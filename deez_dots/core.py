from __future__ import annotations

import argparse
import hashlib
import itertools
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import tomllib as toml

from .ui import UI

LOG = logging.getLogger("deez-dots")
LOG.setLevel(logging.NOTSET)
CLI_VERSION = "v0.1.0"


class _AllSectionsRequested:
    pass


_ALL_SECTIONS_REQUESTED = _AllSectionsRequested()
RequestedSections = Optional[Union[List[str], _AllSectionsRequested]]
RunResult = Tuple[bool, str, str]


def _normalize_description(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def default_run_command(
    cmd: Union[str, List[str]],
    *,
    shell: bool = False,
    cwd: Optional[Union[str, Path]] = None,
    capture_output: bool = True,
    text: bool = True,
    stream_output: bool = False,
    passthrough_output: bool = False,
    retries: int = 1,
    check: bool = False,
) -> RunResult:
    """Run a subprocess and normalize its output."""
    cwd_path = str(cwd) if cwd is not None else None
    for attempt in range(1, retries + 1):
        try:
            if stream_output and passthrough_output:
                if shell and isinstance(cmd, str):
                    proc = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=cwd_path,
                        capture_output=False,
                        text=text,
                        check=False,
                    )
                else:
                    args = cmd if isinstance(cmd, list) else (cmd.split() if isinstance(cmd, str) else cmd)
                    proc = subprocess.run(
                        args,
                        shell=False,
                        cwd=cwd_path,
                        capture_output=False,
                        text=text,
                        check=False,
                    )
                return_code = proc.returncode
                stdout = ""
                stderr = ""
            elif stream_output:
                if shell and isinstance(cmd, str):
                    proc = subprocess.Popen(
                        cmd,
                        shell=True,
                        cwd=cwd_path,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=text,
                        bufsize=1,
                    )
                else:
                    args = cmd if isinstance(cmd, list) else (cmd.split() if isinstance(cmd, str) else cmd)
                    proc = subprocess.Popen(
                        args,
                        shell=False,
                        cwd=cwd_path,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=text,
                        bufsize=1,
                    )
                combined_chunks: List[Union[str, bytes]] = []
                stream = proc.stdout
                while stream is not None:
                    chunk = stream.read(1)
                    if not chunk:
                        break
                    combined_chunks.append(chunk)
                    if text:
                        sys.stdout.write(chunk)
                    else:
                        output_stream = getattr(sys.stdout, "buffer", sys.stdout)
                        output_stream.write(chunk)
                    sys.stdout.flush()
                if stream is not None:
                    stream.close()
                return_code = proc.wait()
                stdout = "".join(combined_chunks) if text else b"".join(combined_chunks)
                stderr = ""
            elif shell and isinstance(cmd, str):
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=cwd_path,
                    capture_output=capture_output,
                    text=text,
                    check=False,
                )
                return_code = proc.returncode
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
            else:
                args = cmd if isinstance(cmd, list) else (cmd.split() if isinstance(cmd, str) else cmd)
                proc = subprocess.run(
                    args,
                    shell=False,
                    cwd=cwd_path,
                    capture_output=capture_output,
                    text=text,
                    check=False,
                )
                return_code = proc.returncode
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
            if return_code == 0:
                LOG.debug("Command succeeded: %s (cwd=%s)", cmd, cwd_path)
                return True, stdout, stderr
            if check:
                LOG.debug("Command check failed: %s returned %s", cmd, return_code)
            LOG.debug("Command failed (attempt %d/%d): %s", attempt, retries, cmd)
            LOG.debug("stdout: %s", stdout)
            LOG.debug("stderr: %s", stderr)
            if attempt == retries:
                return False, stdout, stderr
            time.sleep(0.1)
        except Exception as e:
            LOG.exception("Unexpected error running command: %s", cmd)
            return False, "", str(e)
    return False, "", "unknown error"


class DeezUtils:
    """Utility helpers for normalizing config values and dot metadata.

    This class contains static helper methods used by the CLI and API layers
    for owner normalization, action normalization, timestamps, and config
    environment expansion.
    """

    @staticmethod
    def normalize_owner(owner: Optional[str]) -> str:
        """Normalize a dot owner string into a canonical lowercase identifier."""
        if not owner:
            return "unknown"
        return owner.strip().lower().replace(" ", "_")

    @staticmethod
    def normalize_action(action: Optional[str]) -> str:
        """Normalize a dot file transfer action name to a supported action."""
        if not action:
            return "preserve"
        normalized = str(action).strip().lower()
        if normalized == "overwrite":
            return "sync"
        if normalized not in ("preserve", "sync"):
            return "preserve"
        return normalized

    @staticmethod
    def get_timestamp() -> str:
        """Return the current timestamp formatted for backup and package metadata."""
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")

    @staticmethod
    def expand_env(val: Any) -> Any:
        """Expand environment variables for a string or recursively for lists."""
        if isinstance(val, str):
            return os.path.expandvars(val)
        if isinstance(val, list):
            return [os.path.expandvars(v) for v in val]
        return val

    @staticmethod
    def expand(val: Any) -> Any:
        """Expand environment variables in strings and lists for config values."""
        return DeezUtils.expand_env(val)

    @staticmethod
    def normalize_dependency_blocks(dep_block: Any) -> List[Dict[str, List[str]]]:
        """Normalize dependency block declarations into a manager-to-packages map."""
        def normalize_packages(packages: Any) -> List[str]:
            if isinstance(packages, str):
                package_name = packages.strip()
                return [package_name] if package_name else []
            if not isinstance(packages, list):
                return []
            normalized: List[str] = []
            seen = set()
            for package in packages:
                package_name = str(package or "").strip()
                if not package_name or package_name in seen:
                    continue
                normalized.append(package_name)
                seen.add(package_name)
            return normalized

        normalized_blocks: List[Dict[str, List[str]]] = []
        if not dep_block:
            return normalized_blocks
        if isinstance(dep_block, dict):
            normalized_block: Dict[str, List[str]] = {}
            for manager, packages in dep_block.items():
                manager_name = str(manager or "").strip()
                normalized_packages = normalize_packages(packages)
                if manager_name and normalized_packages:
                    normalized_block[manager_name] = normalized_packages
            if normalized_block:
                normalized_blocks.append(normalized_block)
            return normalized_blocks
        if isinstance(dep_block, list):
            generic_packages: List[str] = []
            for entry in dep_block:
                if isinstance(entry, dict):
                    normalized_blocks.extend(DeezUtils.normalize_dependency_blocks(entry))
                    continue
                generic_packages.extend(normalize_packages([entry]))
            if generic_packages:
                normalized_blocks.append({"system": generic_packages})
            return normalized_blocks
        if isinstance(dep_block, str):
            package_name = dep_block.strip()
            if package_name:
                return [{"system": [package_name]}]
        return normalized_blocks

    @staticmethod
    def merge_dependency_blocks(*dep_blocks: Any) -> Dict[str, List[str]]:
        """Merge one or more dependency blocks into a unified dependency map."""
        merged: Dict[str, List[str]] = {}
        seen_by_manager: Dict[str, set] = {}
        for dep_block in dep_blocks:
            for block in DeezUtils.normalize_dependency_blocks(dep_block):
                for manager, packages in block.items():
                    merged.setdefault(manager, [])
                    seen_by_manager.setdefault(manager, set())
                    for package in packages:
                        if package in seen_by_manager[manager]:
                            continue
                        merged[manager].append(package)
                        seen_by_manager[manager].add(package)
        return {manager: packages for manager, packages in merged.items() if packages}


class PackageManager:
    """Resolve and manage available package managers and package operations.

    This helper class loads package manager command templates from config,
    filters dependencies, and provides a pluggable runner for package queries,
    installs, uninstalls, and updates.
    """

    config_keys: Tuple[str, ...] = ("package_managers", "pm", "package_manager")
    command_keys: Tuple[str, ...] = ("query", "install", "uninstall", "update")
    package_manager_commands: Dict[str, Dict[str, str]] = {
        "pacman": {
            "query": "pacman -Qs",
            "install": "sudo pacman -S",
            "uninstall": "sudo pacman -R",
            "update": "sudo pacman -Syu",
        },
        "yay": {
            "query": "yay -Qs",
            "install": "yay -S",
            "uninstall": "yay -R",
            "update": "yay -Syu",
        },
        "paru": {
            "query": "paru -Qs",
            "install": "paru -S",
            "uninstall": "paru -R",
            "update": "paru -Syu",
        },
        "dnf": {
            "query": "dnf list installed",
            "install": "sudo dnf install",
            "uninstall": "sudo dnf remove",
            "update": "sudo dnf upgrade --refresh",
        },
        "apt": {
            "query": "apt list --installed",
            "install": "sudo apt install -y",
            "uninstall": "sudo apt remove -y",
            "update": "sudo apt update && sudo apt upgrade -y",
        },
        "flatpak": {
            "query": "flatpak list --app --columns application | grep ",
            "install": "flatpak install -y",
            "uninstall": "flatpak uninstall -y",
            "update": "flatpak update -y",
        },
    }

    @classmethod
    def load_pm(cls, global_config: Any) -> Dict[str, Dict[str, str]]:
        """Load package manager command definitions from global config."""
        parsed: Dict[str, Dict[str, str]] = {}
        if not isinstance(global_config, dict):
            return parsed

        for key in cls.config_keys:
            entries = global_config.get(key)
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name", "")).strip()
                if not name:
                    continue
                commands = {cmd_key: str(value).strip() for cmd_key in cls.command_keys for value in [entry.get(cmd_key)] if value is not None and str(value).strip()}
                if commands:
                    parsed.setdefault(name, {}).update(commands)
        return parsed

    @classmethod
    def package_manager_commands_from_global_config(cls, global_config: Any) -> Dict[str, Dict[str, str]]:
        """Return package manager command templates from global config."""
        return cls.load_pm(global_config)

    def __init__(
        self,
        runner: Callable[..., RunResult] = default_run_command,
        custom_commands: Optional[Dict[str, Dict[str, str]]] = None,
    ):
        """Initialize package manager support with a command runner and overrides."""
        self.runner = runner
        self.package_manager_commands = {name: commands.copy() for name, commands in type(self).package_manager_commands.items()}
        for name, commands in (custom_commands or {}).items():
            self.package_manager_commands.setdefault(name, {}).update(commands)
        self.available: List[str] = self.available_managers()
        self._sudo_validated = False

    @staticmethod
    def _command_requires_sudo(cmd: str) -> bool:
        return any(part.strip().startswith("sudo ") for part in str(cmd).split("&&"))

    @staticmethod
    def _should_stream_output() -> bool:
        stdin = getattr(sys, "stdin", None)
        stdout = getattr(sys, "stdout", None)
        try:
            return bool(stdin and stdout and stdin.isatty() and stdout.isatty())
        except Exception:
            return False

    def _ensure_sudo_session(self, manager: str, action: str, cmd: str) -> bool:
        if self._sudo_validated or not self._command_requires_sudo(cmd) or not self._should_stream_output() or not shutil.which("sudo"):
            return True
        was_paused = UI.pause_loader()
        try:
            UI.info(f"Sudo authentication required for {action} via {manager}.")
            success, out, err = self.runner(
                ["sudo", "-v"],
                capture_output=False,
                stream_output=True,
                passthrough_output=True,
                retries=1,
            )
        finally:
            UI.resume_loader(was_paused)
        if success:
            self._sudo_validated = True
            return True
        UI.error(f"Sudo authentication failed for {manager}: {err or out or 'authentication failed'}")
        return False

    def _run_manager_command(self, manager: str, action: str, cmd: str) -> RunResult:
        live_output = self._should_stream_output()
        if live_output and not self._ensure_sudo_session(manager, action, cmd):
            return False, "", "sudo authentication failed"
        was_paused = UI.pause_loader() if live_output else False
        try:
            success, out, err = self.runner(
                cmd,
                shell=True,
                retries=1,
                capture_output=not live_output,
                stream_output=live_output,
                passthrough_output=live_output,
            )
        finally:
            UI.resume_loader(was_paused)
        if live_output and not success:
            return False, "", err or "command exited with a non-zero status"
        return success, out, err

    def available_managers(self) -> List[str]:
        """Return the list of detected package managers available on the system."""
        found: List[str] = []
        for manager in self.package_manager_commands:
            if shutil.which(manager):
                found.append(manager)
        LOG.debug("Detected package managers: %s", found)
        return found

    def query_installed(self, manager: str, package: str) -> bool:
        """Query whether a package is installed using the configured manager."""
        query_cmd = self.package_manager_commands.get(manager, {}).get("query")
        if not query_cmd:
            return False
        cmd = f"{query_cmd} {package}"
        success, out, _ = self.runner(cmd, shell=True, retries=1)
        return bool(out.strip()) if success else False

    def install(self, manager: str, packages: List[str]) -> bool:
        """Install a list of packages using the configured package manager."""
        install_cmd = self.package_manager_commands.get(manager, {}).get("install")
        if not install_cmd or not packages:
            LOG.debug("No install command or packages for manager=%s", manager)
            return False
        if not shutil.which(manager):
            LOG.debug("Skipping install for %s: manager not present", manager)
            return False
        UI.set_loader_message(f"Installing via {manager}: {', '.join(packages)}")
        cmd = f"{install_cmd} {' '.join(packages)}"
        success, out, err = self._run_manager_command(manager, "install", cmd)
        if not success:
            LOG.error("Package install failed for %s: %s", manager, err or out)
            UI.error(f"Failed to install packages for {manager}: {err or out}")
        else:
            LOG.debug("Installed packages for %s: %s", manager, packages)
            UI.success(f"Installed packages for {manager}: {', '.join(packages)}")
        return success

    def update(self, manager: str) -> bool:
        """Update packages for the specified package manager."""
        update_cmd = self.package_manager_commands.get(manager, {}).get("update")
        if not update_cmd:
            LOG.debug("No update command for manager=%s", manager)
            return False
        UI.set_loader_message(f"Updating via {manager}...")
        success, out, err = self._run_manager_command(manager, "update", update_cmd)
        if not success:
            LOG.error("Update failed for %s: %s", manager, err or out)
            UI.error(f"Update failed for {manager}: {err or out}")
        else:
            LOG.debug("Updated packages via %s", manager)
            UI.success(f"Updated packages via {manager}")
        return success

    def install_packages(self, dependencies: Dict[str, List[str]]) -> bool:
        """Install a mapping of package dependencies across supported managers."""
        success = True
        for manager, packages in dependencies.items():
            if not packages:
                LOG.debug("No packages to install for %s", manager)
                continue
            if manager == "system":
                UI.error(f"Cannot auto-install generic dependencies without a package manager mapping: {', '.join(packages)}")
                success = False
                continue
            if not self.install(manager, packages):
                success = False
        return success

    def filter_deps(
        self,
        package_manager: List[str],
        dependency: Dict[str, List[str]],
        filtered_deps: Optional[Dict[str, List[str]]] = None,
    ) -> Dict[str, List[str]]:
        """Filter dependencies to only those applicable to the chosen managers."""
        if filtered_deps is None:
            filtered_deps = {}
        seen_packages: set = set()
        for dep_manager, dep_list in dependency.items():
            if dep_manager == "system":
                filtered_deps.setdefault("system", [])
                for package in dep_list:
                    if package not in seen_packages:
                        filtered_deps["system"].append(package)
                        seen_packages.add(package)
                continue
            dep_managers = [m.strip() for m in dep_manager.split(",")]
            for manager in package_manager:
                if manager in dep_managers:
                    filtered_deps.setdefault(manager, [])
                    for package in dep_list:
                        if package not in seen_packages:
                            filtered_deps[manager].append(package)
                            seen_packages.add(package)
        return {k: v for k, v in filtered_deps.items() if v}

    def fetch_all_deps(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Collect all dependency declarations from config into a deduplicated map."""
        def add_dep_block(acc: Dict[str, List[str]], dep_block: Any) -> None:
            merged = DeezUtils.merge_dependency_blocks(dep_block)
            for manager, packages in merged.items():
                acc.setdefault(manager, []).extend(packages)

        def iter_file_entries(section_data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
            for file_entry in section_data.get("files", []):
                if isinstance(file_entry, dict):
                    yield file_entry
                elif isinstance(file_entry, list):
                    for nested_entry in file_entry:
                        if isinstance(nested_entry, dict):
                            yield nested_entry

        all_deps: Dict[str, List[str]] = {}
        add_dep_block(all_deps, data.get("dependency") or data.get("depends"))
        global_config = data.get("global", {}) if isinstance(data, dict) else {}
        add_dep_block(all_deps, global_config.get("dependency") or global_config.get("depends"))
        if isinstance(global_config, dict) and global_config.get("dots"):
            dot_sections = [s for s in global_config.get("dots", []) if s in data]
        else:
            dot_sections = [k for k in data.keys() if k != "global"]
        for section in dot_sections:
            section_data = data.get(section, {})
            if isinstance(section_data, dict):
                add_dep_block(all_deps, section_data.get("dependency") or section_data.get("depends"))
                for file_entry in iter_file_entries(section_data):
                    add_dep_block(all_deps, file_entry.get("dependency") or file_entry.get("depends"))
        for manager, pkgs in list(all_deps.items()):
            seen = set()
            unique: List[str] = []
            for pkg in pkgs:
                if pkg in seen:
                    continue
                seen.add(pkg)
                unique.append(pkg)
            all_deps[manager] = unique
        return all_deps

    def collect_dependency_blocks(self, data: Dict[str, Any]) -> List[Dict[str, List[str]]]:
        """Collect normalized dependency blocks from config sections and file entries."""
        def append_blocks(acc: List[Dict[str, List[str]]], dep_block: Any) -> None:
            acc.extend(DeezUtils.normalize_dependency_blocks(dep_block))

        def iter_file_entries(section_data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
            for file_entry in section_data.get("files", []):
                if isinstance(file_entry, dict):
                    yield file_entry
                elif isinstance(file_entry, list):
                    for nested_entry in file_entry:
                        if isinstance(nested_entry, dict):
                            yield nested_entry

        dependency_blocks: List[Dict[str, List[str]]] = []
        append_blocks(dependency_blocks, data.get("dependency") or data.get("depends"))
        global_config = data.get("global", {}) if isinstance(data, dict) else {}
        append_blocks(dependency_blocks, global_config.get("dependency") or global_config.get("depends"))
        if isinstance(global_config, dict) and global_config.get("dots"):
            dot_sections = [s for s in global_config.get("dots", []) if s in data]
        else:
            dot_sections = [k for k in data.keys() if k != "global"]
        for section in dot_sections:
            section_data = data.get(section, {})
            if isinstance(section_data, dict):
                append_blocks(dependency_blocks, section_data.get("dependency") or section_data.get("depends"))
                for file_entry in iter_file_entries(section_data):
                    append_blocks(dependency_blocks, file_entry.get("dependency") or file_entry.get("depends"))
        return dependency_blocks

    def resolve_dependency_blocks(
        self,
        dependency_blocks: List[Dict[str, List[str]]],
        available_managers: List[str],
    ) -> Tuple[Dict[str, List[str]], List[Dict[str, List[str]]]]:
        """Resolve dependency blocks into manager-specific install plans and unresolved blocks."""
        selected: Dict[str, List[str]] = {}
        selected_seen: Dict[str, set] = {}
        unresolved: List[Dict[str, List[str]]] = []

        for block in dependency_blocks:
            chosen_manager: Optional[str] = None
            chosen_packages: List[str] = []
            fallback_system: List[str] = []
            has_manager_specific = False

            for manager_key, packages in block.items():
                if manager_key == "system":
                    fallback_system.extend(packages)
                    continue
                has_manager_specific = True
                manager_names = [name.strip() for name in manager_key.split(",") if name.strip()]
                matched_manager = next((name for name in manager_names if name in available_managers), None)
                if matched_manager:
                    chosen_manager = matched_manager
                    chosen_packages = list(packages)
                    break

            if chosen_manager:
                selected.setdefault(chosen_manager, [])
                selected_seen.setdefault(chosen_manager, set())
                for package in chosen_packages:
                    if package in selected_seen[chosen_manager]:
                        continue
                    selected[chosen_manager].append(package)
                    selected_seen[chosen_manager].add(package)
                continue

            if fallback_system:
                selected.setdefault("system", [])
                selected_seen.setdefault("system", set())
                for package in fallback_system:
                    if package in selected_seen["system"]:
                        continue
                    selected["system"].append(package)
                    selected_seen["system"].add(package)
                continue

            if has_manager_specific:
                unresolved.append(block)

        return {manager: packages for manager, packages in selected.items() if packages}, unresolved


class ReadMeta:
    """Load TOML configuration files and resolve included config references."""

    supported_url_schemes: Tuple[str, ...] = ("http", "https", "file")

    @classmethod
    def is_url(cls, config_location: Union[str, Path]) -> bool:
        """Return True if the config location is a supported URL scheme."""
        parsed = urllib.parse.urlparse(str(config_location or "").strip())
        return parsed.scheme in cls.supported_url_schemes

    def read_url(self, config_url: str) -> Dict[str, Any]:
        """Load a TOML config from a remote URL and return it as a dictionary."""
        with urllib.request.urlopen(config_url) as response:
            payload = response.read().decode("utf-8")
        data = toml.loads(payload)
        LOG.debug("Loaded config from URL %s", config_url)
        return data

    def read_file(self, file_path: Union[str, Path]) -> Dict[str, Any]:
        """Load a TOML config file into a Python dictionary."""
        p = Path(file_path)
        with p.open("rb") as f:
            data = toml.load(f)
        LOG.debug("Loaded config from %s", p)
        return data

    @staticmethod
    def _global_include_entries(data: Dict[str, Any]) -> List[str]:
        global_config = data.get("global", {}) if isinstance(data, dict) else {}
        if not isinstance(global_config, dict):
            return []
        include_value = global_config.get("include")
        if not include_value:
            return []
        if isinstance(include_value, str):
            include_value = [include_value]
        if not isinstance(include_value, list):
            return []
        return [str(entry).strip() for entry in include_value if str(entry).strip()]

    @staticmethod
    def _strip_loader_keys(data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return data
        global_config = data.get("global")
        if not isinstance(global_config, dict) or "include" not in global_config:
            return data
        stripped = dict(data)
        stripped_global = dict(global_config)
        stripped_global.pop("include", None)
        stripped["global"] = stripped_global
        return stripped

    @classmethod
    def _merge_config_values(cls, base_value: Any, override_value: Any) -> Any:
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged = dict(base_value)
            for key, value in override_value.items():
                if key in merged:
                    merged[key] = cls._merge_config_values(merged[key], value)
                else:
                    merged[key] = value
            return merged
        if isinstance(base_value, list) and isinstance(override_value, list):
            return list(base_value) + list(override_value)
        return override_value

    @classmethod
    def merge_configs(cls, base_config: Dict[str, Any], override_config: Dict[str, Any]) -> Dict[str, Any]:
        """Merge two configuration dictionaries, combining nested values and lists."""
        return cls._merge_config_values(base_config, override_config)

    def _normalize_location(self, config_location: Union[str, Path]) -> str:
        location_text = str(config_location).strip()
        if self.is_url(location_text):
            return location_text
        return str(Path(os.path.expandvars(os.path.expanduser(location_text))).resolve())

    def _resolve_include_location(self, include_location: str, base_location: str) -> Union[str, Path]:
        include_text = os.path.expandvars(os.path.expanduser(str(include_location).strip()))
        if not include_text:
            raise ValueError("Config include path cannot be empty.")
        if self.is_url(include_text):
            return include_text
        if self.is_url(base_location):
            return urllib.parse.urljoin(base_location, include_text)
        include_path = Path(include_text)
        if include_path.is_absolute():
            return include_path
        return Path(base_location).parent / include_path

    def read_location(
        self,
        config_location: Union[str, Path],
        _loading_stack: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Load a config from a file path or URL, resolving included configs recursively."""
        normalized_location = self._normalize_location(config_location)
        loading_stack = list(_loading_stack or [])
        if normalized_location in loading_stack:
            cycle = " -> ".join(loading_stack + [normalized_location])
            raise ValueError(f"Config include cycle detected: {cycle}")
        loading_stack.append(normalized_location)

        UI.set_loader_message(f"Loading config {normalized_location}...")
        if self.is_url(normalized_location):
            data = self.read_url(normalized_location)
        else:
            data = self.read_file(normalized_location)

        merged_config: Dict[str, Any] = {}
        for include_location in self._global_include_entries(data):
            resolved_include = self._resolve_include_location(include_location, normalized_location)
            UI.set_loader_message(f"Loading included config {resolved_include} referenced from {normalized_location}...")
            try:
                included_config = self.read_location(resolved_include, loading_stack)
            except Exception as exc:
                raise ValueError(
                    f"Failed to load included config '{resolved_include}' referenced from '{normalized_location}': {exc}"
                ) from exc
            merged_config = self.merge_configs(merged_config, included_config)
            UI.success(f"Loaded included config {resolved_include}")

        current_config = self._strip_loader_keys(data)
        return self.merge_configs(merged_config, current_config)


class ManifestManager:
    """Read and write installed-dot manifests under XDG data storage.

    Each installed dot is tracked as a single TOML file at
    ``XDG_DATA_HOME/deez/dots/<dot>.toml``.
    """

    def __init__(self, base_dir: Optional[Union[str, Path]] = None):
        data_home = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        self.base_dir: Path = Path(base_dir) if base_dir else data_home / "deez" / "dots"

    def _base_dir_path(self) -> Path:
        return Path(self.base_dir)

    def _manifest_path(self, dot: str) -> Path:
        return self._base_dir_path() / f"{dot}.toml"

    @staticmethod
    def _serialize(meta: Dict[str, Any], file_pairs: List[Union[Tuple[str, str], Dict[str, Any]]]) -> bytes:
        def normalize_value(value: Any) -> Any:
            if isinstance(value, Path):
                return str(value)
            return value

        def is_table_array(value: Any) -> bool:
            return isinstance(value, list) and all(isinstance(item, dict) for item in value)

        def format_scalar(value: Any) -> str:
            value = normalize_value(value)
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, int):
                return str(value)
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'

        def append_key_value(lines: List[str], key: str, value: Any) -> None:
            if value is None:
                return
            value = normalize_value(value)
            if isinstance(value, list) and not is_table_array(value):
                items = ", ".join(format_scalar(item) for item in value)
                lines.append(f"{key} = [{items}]")
                return
            lines.append(f"{key} = {format_scalar(value)}")

        def append_table_array(lines: List[str], table_name: str, blocks: List[Dict[str, Any]]) -> None:
            for block in blocks:
                lines.append(f"[[{table_name}]]")
                nested_arrays: List[Tuple[str, List[Dict[str, Any]]]] = []
                for key, value in block.items():
                    if value is None:
                        continue
                    if is_table_array(value):
                        nested_arrays.append((key, value))
                        continue
                    append_key_value(lines, key, value)
                lines.append("")
                for nested_key, nested_blocks in nested_arrays:
                    append_table_array(lines, f"{table_name}.{nested_key}", nested_blocks)

        lines: List[str] = []
        meta_arrays: List[Tuple[str, List[Dict[str, Any]]]] = []
        for key, value in meta.items():
            if value is None:
                continue
            if is_table_array(value):
                meta_arrays.append((key, value))
                continue
            append_key_value(lines, key, value)
        lines.append("")
        for key, blocks in meta_arrays:
            append_table_array(lines, key, blocks)
        serialized_files: List[Dict[str, Any]] = []
        for file_pair in file_pairs:
            if isinstance(file_pair, dict):
                serialized_files.append({k: normalize_value(v) for k, v in file_pair.items() if v is not None})
            else:
                src, dst = file_pair
                serialized_files.append({"src": normalize_value(src), "dst": normalize_value(dst)})
        append_table_array(lines, "files", serialized_files)
        return "\n".join(lines).encode("utf-8")

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        part = path.with_suffix(path.suffix + ".part") if path.suffix else Path(str(path) + ".part")
        path.parent.mkdir(parents=True, exist_ok=True)
        with part.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        part.replace(path)
        LOG.debug("Wrote manifest atomically to %s", path)

    def _load_raw(self, dot: str) -> Dict[str, Any]:
        path = self._manifest_path(dot)
        if not path.exists():
            return {}
        with path.open("rb") as f:
            return toml.load(f)

    def save(self, dot: str, meta: Dict[str, Any], file_pairs: List[Union[Tuple[str, str], Dict[str, Any]]]) -> None:
        """Save a dot manifest for an installed dot to disk."""
        payload = self._serialize(meta, file_pairs)
        self._atomic_write(self._manifest_path(dot), payload)

    def load_desc(self, dot: str) -> Dict[str, Any]:
        """Load metadata for an installed dot without the file entries."""
        raw = self._load_raw(dot)
        raw.pop("files", None)
        return raw

    def get_files(self, dot: str) -> List[str]:
        """Return tracked installed destination file paths for a dot."""
        raw = self._load_raw(dot)
        files = raw.get("files", [])
        return [e["dst"] for e in files if e.get("installed", True)]

    def get_file_entries(self, dot: str) -> List[Dict[str, Any]]:
        """Return the raw file entry definitions for a tracked dot."""
        return self._load_raw(dot).get("files", [])

    def remove_dot(self, dot: str) -> None:
        """Remove the manifest for an installed dot from storage."""
        path = self._manifest_path(dot)
        if path.exists():
            path.unlink()
            LOG.debug("Removed manifest %s", path)

    def mark_removed(self, dot: str) -> None:
        """Mark a dot as removed by deleting its manifest."""
        self.remove_dot(dot)

    def find_owner_of(self, target_path: str) -> Optional[str]:
        """Return the dot that owns a tracked target path, if any."""
        if not self._base_dir_path().is_dir():
            return None
        for dot in self.list_dots():
            if target_path in self.get_files(dot):
                return dot
        return None

    def build_owner_index(self) -> Dict[str, str]:
        """Build a reverse index mapping tracked paths to owning dot names."""
        owner_index: Dict[str, str] = {}
        for dot in self.list_dots():
            for path in self.get_files(dot):
                owner_index.setdefault(path, dot)
        return owner_index

    def list_dots(self) -> List[str]:
        """List all installed dots for which manifests exist."""
        base_dir = self._base_dir_path()
        if not base_dir.is_dir():
            return []
        return [p.stem for p in base_dir.glob("*.toml") if p.is_file()]


@dataclass
class CacheEntry:
    """Metadata for a cached dot bundle archive."""

    path: Path
    name: str
    version: str
    githash: str
    builddate: str
    builddate_raw: str
    origin: str
    size: Optional[int]
    mtime: str
    mtime_ts: int
    meta: Dict[str, Any]


class CacheManager:
    """Manage cached dot bundle archives under XDG cache storage."""

    def __init__(self, cache_root: Optional[Union[str, Path]] = None):
        xdg_cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.cache_root: Path = Path(cache_root) if cache_root else xdg_cache / "deez" / "dots"

    def _bundle_paths(self) -> List[Path]:
        if not self.cache_root.is_dir():
            return []
        return [p for p in self.cache_root.iterdir() if p.suffixes and p.name.endswith(".tar.gz")]

    @staticmethod
    def _format_builddate(builddate_raw: Any) -> str:
        if builddate_raw in (None, ""):
            return "?"
        try:
            return datetime.fromtimestamp(int(builddate_raw)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(builddate_raw)

    @staticmethod
    def _parse_builddate(builddate_raw: Any) -> int:
        try:
            return int(builddate_raw)
        except Exception:
            return 0

    def read_bundle_metadata(self, path: Union[str, Path]) -> CacheEntry:
        """Read metadata from a cached bundle archive without loading the full bundle."""
        p = Path(path)
        name = "?"
        version = "?"
        githash = "?"
        builddate_raw = ""
        builddate = "?"
        origin = ""
        meta: Dict[str, Any] = {}
        try:
            with tarfile.open(p, "r:gz") as tar:
                member = tar.extractfile("manifest.toml")
                if member:
                    meta = toml.loads(member.read().decode("utf-8"))
                    name = meta.get("name", name)
                    version = meta.get("version", version)
                    githash = (meta.get("githash") or "")[:8] or githash
                    builddate_raw = meta.get("builddate", "")
                    builddate = self._format_builddate(builddate_raw)
                    origin = meta.get("origin", "")
        except Exception:
            LOG.debug("Failed to read manifest from %s", p)
        try:
            stat = p.stat()
            size = stat.st_size
            mtime_ts = int(stat.st_mtime)
            mtime = datetime.fromtimestamp(mtime_ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            size = None
            mtime = "?"
            mtime_ts = 0
        return CacheEntry(
            path=p,
            name=name,
            version=version,
            githash=githash,
            builddate=builddate,
            builddate_raw=str(builddate_raw),
            origin=origin,
            size=size,
            mtime=mtime,
            mtime_ts=mtime_ts,
            meta=meta,
        )

    def list_entries(self) -> List[CacheEntry]:
        """List all cache bundle entries in the configured cache root."""
        entries = [self.read_bundle_metadata(p) for p in self._bundle_paths()]
        entries.sort(key=lambda e: e.mtime_ts, reverse=True)
        return entries

    def bundles_by_dot(self) -> Dict[str, List[CacheEntry]]:
        """Group cached bundles by their dot name."""
        cached_by_dot: Dict[str, List[CacheEntry]] = {}
        for entry in self.list_entries():
            if not entry.name:
                continue
            cached_by_dot.setdefault(entry.name, []).append(entry)
        for entries in cached_by_dot.values():
            entries.sort(
                key=lambda entry: (self._parse_builddate(entry.builddate_raw), entry.mtime_ts),
                reverse=True,
            )
        return cached_by_dot

    def bundle_path_for_hash(self, bundle_hash: str) -> Optional[Path]:
        """Return the cache path for a bundle identified by its hash."""
        digest = str(bundle_hash or "").strip()
        if not digest:
            return None
        candidate = self.cache_root / f"{digest}.tar.gz"
        return candidate if candidate.is_file() else None

    def prune_keep(self, keep_count: int = 10, dry_run: bool = False) -> int:
        """Prune cached bundles to keep the newest `keep_count` items."""
        if keep_count is None:
            keep_count = 10
        if keep_count < 0:
            LOG.error("--keep must be a non-negative integer when using --cache prune")
            UI.error("--keep must be a non-negative integer")
            return 1
        entries = sorted(self._bundle_paths(), key=lambda p: p.stat().st_mtime, reverse=True)
        if len(entries) <= keep_count:
            LOG.debug("Keeping all cache entries: total=%d", len(entries))
            UI.success(f"Keeping all cache entries: total={len(entries)}")
            return 0
        to_delete = entries[keep_count:]
        LOG.debug("Pruning cache: keep=%d, total=%d, delete=%d", keep_count, len(entries), len(to_delete))
        UI.plain(f"Pruning cache: keep={keep_count}, total={len(entries)}, delete={len(to_delete)}")
        for p in to_delete:
            if dry_run:
                LOG.debug("Would delete: %s", p)
                UI.plain(f"  would delete: {p}")
                continue
            try:
                p.unlink()
                sha = Path(str(p)[: -len(".tar.gz")] + ".sha256")
                if sha.exists():
                    sha.unlink()
                LOG.debug("Deleted: %s", p)
            except Exception as e:
                LOG.error("Failed to delete %s: %s", p, e)
                UI.error(f"Failed to delete {p}: {e}")
        UI.success(f"Pruned cache, kept {keep_count} entries")
        return 0


class WriteDots:
    """Execute dot-related commands and backup operations during CLI workflows."""

    def __init__(self, runner: Callable[..., RunResult] = default_run_command):
        """Initialize a WriteDots instance with an optional command runner."""
        self.runner = runner

    def execute_commands(
        self,
        commands: Iterable[str],
        cwd: Optional[Union[str, Path]] = None,
        soft_fail: bool = True,
    ) -> None:
        """Execute a series of shell commands or scripts with optional failure handling."""
        for cmd in commands:
            if not cmd:
                continue
            cmd = cmd.strip()
            resolved: Optional[Path] = None
            if "/" in cmd or cmd.endswith(".py") or cmd.endswith(".sh"):
                candidate = Path(cmd) if Path(cmd).is_absolute() else Path(cwd or ".") / cmd
                if candidate.exists():
                    resolved = candidate
            try:
                if resolved and os.access(resolved, os.X_OK):
                    LOG.debug("Executing (executable): %s", resolved)
                    success, out, err = self.runner([str(resolved)], cwd=cwd)
                elif resolved and resolved.suffix == ".py":
                    LOG.debug("Executing (python): %s", resolved)
                    success, out, err = self.runner([os.sys.executable, str(resolved)], cwd=cwd)
                else:
                    LOG.debug("Executing (shell): %s", cmd)
                    success, out, err = self.runner(cmd, shell=True, cwd=cwd)
                if success:
                    continue
                message = err or out or "command failed"
                LOG.warning("Command reported failure: %s (%s)", cmd, message)
                if soft_fail:
                    UI.warn(f"Command skipped after failure: {cmd}: {message}")
                    continue
                raise RuntimeError(message)
            except Exception as e:
                LOG.warning("Failed to execute command '%s': %s", cmd, e)
                if soft_fail:
                    UI.warn(f"Command skipped after failure: {cmd}: {e}")
                else:
                    UI.error(f"Command failed: {cmd}: {e}")
                    raise

    @staticmethod
    def _backup_dot_dirname(dot: str, owner: str, version: str) -> str:
        import re

        def safe(value: Any) -> str:
            return re.sub(r"[^a-zA-Z0-9._-]", "-", str(value or "unknown"))

        return f"{safe(dot)}.{safe(owner)}.{safe(version)}"

    def backup_to_tarball(
        self,
        dot: str,
        file_entries: List[Union[str, Dict[str, Any]]],
        manifest_manager: Optional[ManifestManager] = None,
        desc_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a backup tarball for a dot's tracked files and return its path."""
        if desc_data is None:
            desc_data = {}
            if manifest_manager:
                desc_data = manifest_manager.load_desc(dot) or {}
        owner = desc_data.get("owner", "unknown")
        version = desc_data.get("version", "unknown")
        dirname = self._backup_dot_dirname(dot, owner, version)
        xdg_data = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        backup_dir = xdg_data / "deez" / "backup" / "user" / dirname
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = DeezUtils.get_timestamp().replace(":", "-")
        tmp_stage = Path(tempfile.mkdtemp(prefix="deez-backup-"))
        try:
            data_dir = tmp_stage / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            backed_pairs: List[Dict[str, Any]] = []
            for entry in file_entries:
                if isinstance(entry, dict):
                    dst_abs = entry.get("dst")
                    action = entry.get("action", "sync")
                else:
                    dst_abs = entry
                    action = "sync"
                if not dst_abs:
                    continue
                dst_path = Path(dst_abs)
                if not self._path_exists_or_link(dst_path):
                    continue
                rel = dst_path.as_posix().lstrip("/")
                dest_in_stage = data_dir / rel
                try:
                    self._copy_path_to_stage(dst_path, dest_in_stage)
                except Exception as e:
                    LOG.warning("Backup failed for %s: %s", dst_abs, e)
                    continue
                for f in self._expand_files(dest_in_stage):
                    data_rel = Path(f).relative_to(data_dir).as_posix()
                    backed_pairs.append({"src": data_rel, "dst": Path(os.sep) / data_rel, "action": action})
            if not backed_pairs:
                LOG.debug("No files backed up for %s", dot)
                return ""
            backup_meta = {"name": dot, "owner": owner, "version": version, "builddate": str(int(time.time())), "origin": "backup"}
            manifest_bytes = ManifestManager._serialize(backup_meta, backed_pairs)
            (tmp_stage / "manifest.toml").write_bytes(manifest_bytes)
            tarball_path = backup_dir / f"{ts}.tar.gz"
            with tarfile.open(tarball_path, "w:gz") as tar:
                tar.add(tmp_stage / "manifest.toml", arcname="manifest.toml")
                tar.add(data_dir, arcname="data")
            digest = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
            (backup_dir / f"{ts}.sha256").write_text(f"{digest}  {ts}.tar.gz\n")
            LOG.debug("Backup tarball created: %s", tarball_path)
            UI.info(f"Backup saved: {tarball_path}")
            return str(tarball_path)
        finally:
            shutil.rmtree(tmp_stage, ignore_errors=True)

    def _pkg_path(self, dot: str, version: str, out_dir: Optional[Union[str, Path]] = None) -> str:
        safe_ver = (version or "unknown").replace("/", "-")
        base = Path(out_dir) if out_dir else Path.cwd() / "build"
        base.mkdir(parents=True, exist_ok=True)
        return str(base / f"{dot}-{safe_ver}.tar.gz")

    @staticmethod
    def _expand_files(root_path: Union[str, Path]) -> List[str]:
        p = Path(root_path)
        if p.is_symlink():
            return [str(p)]
        if p.is_file():
            return [str(p)]
        result: List[str] = []
        for dirpath, dirnames, filenames in os.walk(p):
            current_dir = Path(dirpath)
            for dirname in dirnames:
                candidate = current_dir / dirname
                if candidate.is_symlink():
                    result.append(str(candidate))
            for fname in filenames:
                result.append(str(current_dir / fname))
        return result

    @staticmethod
    def _path_exists_or_link(candidate_path: Union[str, Path]) -> bool:
        candidate = Path(candidate_path)
        try:
            return candidate.exists() or candidate.is_symlink()
        except OSError:
            return candidate.is_symlink()

    @staticmethod
    def _remove_existing_path(candidate_path: Union[str, Path]) -> None:
        candidate = Path(candidate_path)
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
            return
        if candidate.exists():
            shutil.rmtree(candidate)

    @staticmethod
    def _copy_symlink(source_path: Union[str, Path], staged_path: Union[str, Path]) -> None:
        source = Path(source_path)
        staged = Path(staged_path)
        if staged.exists() or staged.is_symlink():
            WriteDots._remove_existing_path(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.readlink(source), staged)

    @staticmethod
    def _copy_path_to_stage(source_path: Union[str, Path], staged_path: Union[str, Path]) -> bool:
        source = Path(source_path)
        staged = Path(staged_path)
        if not WriteDots._path_exists_or_link(source):
            return False
        if source.is_symlink():
            WriteDots._copy_symlink(source, staged)
            return True
        staged.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if staged.exists() or staged.is_symlink():
                WriteDots._remove_existing_path(staged)
            shutil.copytree(source, staged, symlinks=True)
        else:
            shutil.copy2(source, staged)
        return True

    @staticmethod
    def _expand_relative_paths(root_path: Union[str, Path], rel_paths: Union[str, List[str]]) -> List[Tuple[str, str]]:
        root = Path(root_path)
        raw_paths = rel_paths if isinstance(rel_paths, list) else [rel_paths]
        matches: List[Tuple[str, str]] = []
        seen: set = set()
        for raw_path in raw_paths:
            rel_path = str(raw_path or "").strip()
            if not rel_path:
                continue
            if rel_path in (".", "./"):
                if "." not in seen:
                    seen.add(".")
                    matches.append((rel_path, "."))
                continue
            if any(ch in rel_path for ch in "*?["):
                for matched_path in sorted(root.glob(rel_path)):
                    relative_match = matched_path.relative_to(root).as_posix()
                    if relative_match in seen:
                        continue
                    seen.add(relative_match)
                    matches.append((rel_path, relative_match))
                continue
            candidate = root / rel_path
            if WriteDots._path_exists_or_link(candidate) and rel_path not in seen:
                seen.add(rel_path)
                matches.append((rel_path, rel_path))
        return matches

    def _append_stage_manifest_entries(
        self,
        file_pairs: List[Dict[str, Any]],
        staged_path: Union[str, Path],
        data_dir: Union[str, Path],
        matched_rel_path: str,
        target_root: Union[str, Path],
        action: str,
        clean_target: bool = False,
        entry_metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        staged = Path(staged_path)
        matched_rel = PurePosixPath(str(matched_rel_path or ".").strip())
        appended_count = 0
        for staged_file in self._expand_files(staged):
            data_rel = Path(staged_file).relative_to(data_dir).as_posix()
            if staged.is_dir():
                staged_suffix = Path(staged_file).relative_to(staged).as_posix()
                dst_rel = matched_rel / PurePosixPath(staged_suffix)
            else:
                dst_rel = matched_rel
            manifest_entry = {
                "src": data_rel,
                "dst": str(Path(target_root) / str(dst_rel)),
                "action": action,
            }
            if entry_metadata:
                for key, value in entry_metadata.items():
                    if value is None:
                        continue
                    manifest_entry[key] = value
            if clean_target:
                manifest_entry["clean_target"] = True
            file_pairs.append(manifest_entry)
            appended_count += 1
        return appended_count

    @staticmethod
    def _filter_manifest_meta(meta: Optional[Dict[str, Any]], *, origin: str, builddate: str) -> Dict[str, Any]:
        desc_data: Dict[str, Any] = {}
        allowed_keys = {
            "name",
            "owner",
            "version",
            "source",
            "branch",
            "dependency",
            "depends",
            "conflicts",
            "pre_command",
            "post_command",
            "build_command",
            "githash",
            "clean_target",
        }
        for key, value in (meta or {}).items():
            if key in allowed_keys and value not in (None, "", []):
                if key == "depends":
                    desc_data["dependency"] = value
                else:
                    desc_data[key] = value
        desc_data["origin"] = origin
        desc_data["builddate"] = builddate
        return desc_data

    @staticmethod
    def _normalize_archive_root(archive_root: Optional[Union[str, Path]]) -> str:
        if archive_root is None:
            return ""
        root_text = str(archive_root).strip().replace("\\", "/")
        if not root_text or root_text in (".", "./") or os.path.isabs(root_text):
            return ""
        normalized = PurePosixPath(root_text).as_posix().strip("/")
        return "" if normalized == "." else normalized

    @staticmethod
    def _matches_ignored_path(relative_path: str, ignored_paths: List[str]) -> bool:
        relative_posix = relative_path.strip("/") or "."
        relative = PurePosixPath(relative_posix)
        for pattern in ignored_paths:
            normalized_pattern = str(pattern or "").strip().strip("/")
            if not normalized_pattern:
                continue
            candidate_patterns = [normalized_pattern]
            if normalized_pattern.startswith("**/"):
                candidate_patterns.append(normalized_pattern[3:])
            for candidate_pattern in candidate_patterns:
                if fnmatchcase(relative_posix, candidate_pattern) or relative.match(candidate_pattern) or relative.match(f"{candidate_pattern}/**"):
                    return True
        return False

    def _prune_ignored_staged_paths(
        self,
        staged_path: Union[str, Path],
        data_dir: Union[str, Path],
        ignored_paths: List[str],
    ) -> None:
        if not ignored_paths:
            return
        staged = Path(staged_path)
        data_root = Path(data_dir)
        if not staged.exists():
            return
        candidates = list(staged.rglob("*"))
        if staged != data_root:
            candidates.append(staged)
        for candidate in sorted(candidates, key=lambda path: len(path.parts), reverse=True):
            if not candidate.exists():
                continue
            relative = candidate.relative_to(data_root).as_posix()
            if not self._matches_ignored_path(relative, ignored_paths):
                continue
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
            elif candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
        for candidate in sorted(staged.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if candidate.is_dir() and not candidate.is_symlink() and not any(candidate.iterdir()):
                candidate.rmdir()
        if staged != data_root and staged.exists() and staged.is_dir() and not staged.is_symlink() and not any(staged.iterdir()):
            staged.rmdir()

    @staticmethod
    def _prune_empty_parents(candidate_path: Union[str, Path], stop_at: Union[str, Path]) -> None:
        stop_path = Path(stop_at)
        current = Path(candidate_path)
        if not current.exists():
            current = current.parent
        while current.exists() and current != stop_path:
            if not current.is_dir() or any(current.iterdir()):
                break
            parent = current.parent
            current.rmdir()
            current = parent

    @staticmethod
    def _copy_with_action(src_path: Union[str, Path], tgt_path: Union[str, Path], action: str, clean_target: bool = False) -> bool:
        action = DeezUtils.normalize_action(action)
        src = Path(src_path)
        tgt = Path(tgt_path)
        if src.is_symlink():
            if action == "preserve" and (tgt.exists() or tgt.is_symlink()):
                return False
            if tgt.exists() or tgt.is_symlink():
                if tgt.is_dir() and not tgt.is_symlink() and clean_target:
                    backup_parent = tgt.parent
                    backup_name = tgt.name + ".old"
                    backup_path = backup_parent / backup_name
                    counter = 1
                    while backup_path.exists():
                        backup_path = backup_parent / f"{backup_name}.{counter}"
                        counter += 1
                    shutil.move(str(tgt), str(backup_path))
                    LOG.debug("Clean build: moved existing %s -> %s", tgt, backup_path)
                else:
                    WriteDots._remove_existing_path(tgt)
            tgt.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(src), tgt)
            return True
        if src.is_dir():
            if action == "sync":
                if tgt.exists() and clean_target:
                    backup_parent = tgt.parent
                    backup_name = tgt.name + ".old"
                    backup_path = backup_parent / backup_name
                    counter = 1
                    while backup_path.exists():
                        backup_path = backup_parent / f"{backup_name}.{counter}"
                        counter += 1
                    shutil.move(str(tgt), str(backup_path))
                    LOG.debug("Clean build: moved existing %s -> %s", tgt, backup_path)
                if not tgt.exists():
                    shutil.copytree(src, tgt)
                    return True
                for root, _dirs, files in os.walk(src):
                    rel = os.path.relpath(root, src)
                    dest_root = tgt if rel == "." else tgt / rel
                    dest_root.mkdir(parents=True, exist_ok=True)
                    for fname in files:
                        shutil.copy2(Path(root) / fname, dest_root / fname)
                return True
            if action == "preserve":
                if not tgt.exists():
                    shutil.copytree(src, tgt)
                    return True
                for root, _dirs, files in os.walk(src):
                    rel = os.path.relpath(root, src)
                    dest_root = tgt if rel == "." else tgt / rel
                    dest_root.mkdir(parents=True, exist_ok=True)
                    for fname in files:
                        dst_file = dest_root / fname
                        if not dst_file.exists():
                            shutil.copy2(Path(root) / fname, dst_file)
                return True
            return False
        if action == "preserve" and tgt.exists():
            return False
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, tgt)
        return True

    def _write_bundle(
        self,
        stage_dir: Union[str, Path],
        pkg_path: str,
        desc_data: Dict[str, Any],
        file_pairs: List[Dict[str, Any]],
        compress: bool = True,
        overwrite_existing: bool = False,
    ) -> str:
        stage = Path(stage_dir)
        manifest_bytes = ManifestManager._serialize(desc_data, file_pairs)
        (stage / "manifest.toml").write_bytes(manifest_bytes)
        if not compress:
            out_dir = Path(pkg_path[: -len(".tar.gz")]) if pkg_path.endswith(".tar.gz") else Path(pkg_path)
            out_dir.parent.mkdir(parents=True, exist_ok=True)
            if out_dir.exists():
                shutil.rmtree(out_dir)
            shutil.copytree(stage, out_dir)
            shutil.rmtree(stage)
            LOG.debug("Wrote uncompressed bundle to %s", out_dir)
            return str(out_dir)
        extracted_dir = Path(pkg_path[: -len(".tar.gz")]) if pkg_path.endswith(".tar.gz") else None
        if extracted_dir and extracted_dir.exists():
            if overwrite_existing:
                if extracted_dir.is_dir():
                    shutil.rmtree(extracted_dir)
                else:
                    extracted_dir.unlink()
                UI.warn(f"Removed existing extracted build directory: {extracted_dir}")
            else:
                UI.warn(f"Existing extracted build directory may be stale: {extracted_dir}. Use --force to remove it.")
        Path(pkg_path).parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(pkg_path, "w:gz") as tar:
            tar.add(stage / "manifest.toml", arcname="manifest.toml")
            tar.add(stage / "data", arcname="data")
        shutil.rmtree(stage)
        LOG.debug("Wrote compressed bundle to %s", pkg_path)
        return pkg_path

    def _stage_file_entries(
        self,
        file_entries: List[Dict[str, Any]],
        stage_dir: Union[str, Path],
        warn_on_missing: bool = False,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        data_dir = Path(stage_dir) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        all_file_pairs: List[Dict[str, Any]] = []
        any_staged = False
        for entry in file_entries:
            src_root = Path(entry["src_root"])
            tgt_root = entry["tgt_root"]
            rel_paths = entry["rel_paths"]
            ignored_paths = entry.get("ignored_paths") or []
            archive_root = self._normalize_archive_root(entry.get("archive_root"))
            action = DeezUtils.normalize_action(entry.get("action"))
            clean_target = bool(entry.get("clean_target", False))
            entry_metadata = dict(entry.get("entry_metadata") or {})
            entry_stage_root = data_dir / archive_root if archive_root else data_dir
            if not self._path_exists_or_link(src_root):
                LOG.debug("Stage: source root missing %s", src_root)
                if warn_on_missing:
                    UI.warn(f"Source root missing: {src_root}")
                continue
            expanded_paths = self._expand_relative_paths(src_root, rel_paths)
            matched_patterns = {rel_pattern for rel_pattern, _rel_path in expanded_paths}
            if warn_on_missing:
                raw_paths = rel_paths if isinstance(rel_paths, list) else [rel_paths]
                for raw_path in raw_paths:
                    rel_pattern = str(raw_path or "").strip()
                    if not rel_pattern or rel_pattern in matched_patterns:
                        continue
                    if rel_pattern in (".", "./"):
                        UI.warn(f"Source path missing: {src_root}")
                    elif any(ch in rel_pattern for ch in "*?["):
                        UI.warn(f"No source files matched: {src_root / rel_pattern}")
                    else:
                        UI.warn(f"Source path missing: {src_root / rel_pattern}")
            for rel_pattern, rel_path in expanded_paths:
                src_path = src_root / rel_path
                dest_path = entry_stage_root / rel_path
                source_file_count = len(self._expand_files(src_path)) if self._path_exists_or_link(src_path) else 0
                if not self._path_exists_or_link(src_path):
                    LOG.debug("Stage: source missing %s", src_path)
                    if warn_on_missing:
                        UI.warn(f"Source path missing: {src_path}")
                    continue
                self._copy_path_to_stage(src_path, dest_path)
                self._prune_ignored_staged_paths(dest_path, entry_stage_root, ignored_paths)
                appended_count = self._append_stage_manifest_entries(
                    all_file_pairs,
                    dest_path,
                    data_dir,
                    rel_path,
                    tgt_root,
                    action,
                    clean_target=clean_target,
                    entry_metadata=entry_metadata,
                )
                if not appended_count:
                    self._prune_empty_parents(dest_path, data_dir)
                    if warn_on_missing and ignored_paths and source_file_count:
                        UI.warn(f"Ignored all matched files: {src_path}")
                    continue
                any_staged = True
                LOG.debug("Staged %s -> %s (pattern=%s)", src_path, dest_path, rel_pattern)
        return all_file_pairs, any_staged

    def stage(
        self,
        file_entries: List[Dict[str, Any]],
        dot: str,
        owner: str,
        version: str,
        githash: str,
        source_url: str = "",
        branch: str = "",
        dependency: Optional[Any] = None,
        conflicts: Optional[List[str]] = None,
        compress: bool = True,
        out_dir: Optional[str] = None,
        pre_command: Optional[str] = None,
        post_command: Optional[str] = None,
        build_command: Optional[str] = None,
        overwrite_existing: bool = False,
    ) -> str:
        """Stage dot files and metadata into a bundle archive, returning the bundle path."""
        xdg_cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
        stage_dir = xdg_cache / "deez" / "stage" / dot
        shutil.rmtree(stage_dir, ignore_errors=True)
        try:
            all_file_pairs, any_staged = self._stage_file_entries(file_entries, stage_dir, warn_on_missing=True)
            if not any_staged:
                LOG.debug("Nothing staged for %s", dot)
                return ""
            desc_data = {
                "name": dot,
                "owner": owner,
                "version": version or "unknown",
                "githash": githash,
                "builddate": str(int(time.time())),
                "origin": "package",
            }
            if source_url:
                desc_data["source"] = source_url
            if branch:
                desc_data["branch"] = branch
            normalized_dependency = DeezUtils.normalize_dependency_blocks(dependency)
            if normalized_dependency:
                desc_data["dependency"] = normalized_dependency
            if conflicts:
                desc_data["conflicts"] = conflicts
            if pre_command:
                desc_data["pre_command"] = pre_command
            if post_command:
                desc_data["post_command"] = post_command
            if build_command:
                desc_data["build_command"] = build_command
            pkg_path = self._pkg_path(dot, version, out_dir=out_dir)
            out_path = self._write_bundle(stage_dir, pkg_path, desc_data, all_file_pairs, compress=compress, overwrite_existing=overwrite_existing)
            LOG.debug("Bundled %s -> %s", dot, out_path)
            UI.success(f"Bundled {dot} -> {out_path}")
            return out_path
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def export_entries(
        self,
        file_entries: List[Dict[str, Any]],
        dot: str,
        owner: str,
        version: str,
        manifest_meta: Optional[Dict[str, Any]] = None,
        compress: bool = True,
        out_dir: Optional[str] = None,
        overwrite_existing: bool = False,
    ) -> str:
        """Export staged entries to a bundle with optional manifest metadata."""
        xdg_cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
        ts = str(int(time.time()))
        stage_dir = xdg_cache / "deez" / "stage" / f"{dot}-export-{ts}"
        shutil.rmtree(stage_dir, ignore_errors=True)
        try:
            file_pairs, any_staged = self._stage_file_entries(file_entries, stage_dir)
            if not any_staged:
                LOG.debug("No files exported for %s", dot)
                return ""
            desc_data = self._filter_manifest_meta(manifest_meta, origin="export", builddate=ts)
            desc_data.setdefault("name", dot)
            desc_data.setdefault("owner", owner)
            desc_data.setdefault("version", version or "unknown")
            if any(bool(entry.get("clean_target", False)) for entry in file_entries):
                desc_data["clean_target"] = True
            pkg_path = self._pkg_path(dot, version, out_dir=out_dir)
            out_path = self._write_bundle(stage_dir, pkg_path, desc_data, file_pairs, compress=compress, overwrite_existing=overwrite_existing)
            LOG.debug("Exported %s -> %s", dot, out_path)
            UI.success(f"Exported {dot} -> {out_path}")
            return out_path
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def export(
        self,
        rel_paths: Union[str, List[str]],
        tgt_root: str,
        dot: str,
        owner: str,
        version: str,
        action_map: Optional[Dict[str, str]] = None,
        ignored_paths: Optional[List[str]] = None,
        clean_target: bool = False,
        manifest_meta: Optional[Dict[str, Any]] = None,
        compress: bool = True,
        overwrite_existing: bool = False,
    ) -> str:
        """Export live files from a target root into a bundle archive."""
        xdg_cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
        ts = str(int(time.time()))
        stage_dir = xdg_cache / "deez" / "stage" / f"{dot}-export-{ts}"
        try:
            data_dir = stage_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            captured_rel_paths: List[str] = []
            file_pairs: List[Dict[str, Any]] = []
            ignored = ignored_paths or []
            for rel_pattern, rel_path in self._expand_relative_paths(tgt_root, rel_paths):
                src_path = Path(tgt_root) / rel_path
                dest_path = data_dir / rel_path
                if not src_path.exists():
                    LOG.debug("Export: live file missing %s", src_path)
                    continue
                self._copy_path_to_stage(src_path, dest_path)
                self._prune_ignored_staged_paths(dest_path, data_dir, ignored)
                entry_action = DeezUtils.normalize_action(action_map.get(rel_pattern) if action_map else None)
                appended_count = self._append_stage_manifest_entries(
                    file_pairs,
                    dest_path,
                    data_dir,
                    rel_path,
                    tgt_root,
                    entry_action,
                )
                if not appended_count:
                    continue
                captured_rel_paths.append(rel_path)
                LOG.debug("Exported %s -> %s (pattern=%s)", src_path, dest_path, rel_pattern)
            if not captured_rel_paths:
                LOG.debug("No files exported for %s", dot)
                return ""
            pkg_path = self._pkg_path(dot, version)
            desc_data = self._filter_manifest_meta(manifest_meta, origin="export", builddate=ts)
            desc_data.setdefault("name", dot)
            desc_data.setdefault("owner", owner)
            desc_data.setdefault("version", version or "unknown")
            if clean_target:
                desc_data["clean_target"] = True
            out_path = self._write_bundle(stage_dir, pkg_path, desc_data, file_pairs, compress=compress, overwrite_existing=overwrite_existing)
            LOG.debug("Exported %s -> %s", dot, out_path)
            UI.success(f"Exported {dot} -> {out_path}")
            return out_path
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def remove(self, dot: str, manifest_manager: ManifestManager) -> None:
        """Remove an installed dot by backing up and deleting tracked files."""
        tracked = manifest_manager.get_files(dot)
        if not tracked:
            LOG.debug("No tracked files for %s", dot)
            UI.info(f"No tracked files for {dot}")
            return
        file_entries = manifest_manager.get_file_entries(dot) or [{"dst": p, "action": "sync"} for p in tracked]
        self.backup_to_tarball(dot, file_entries, manifest_manager)
        for tgt_path in tracked:
            p = Path(tgt_path)
            if not p.exists():
                LOG.debug("Remove: already gone %s", tgt_path)
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                LOG.debug("Removed %s", tgt_path)
            except Exception as e:
                LOG.warning("Failed to remove %s: %s", tgt_path, e)
                UI.error(f"Failed to remove {tgt_path}: {e}")
        manifest_manager.mark_removed(dot)
        UI.success(f"Uninstalled {dot}")


class GitHandler:
    """Git helper used to resolve dot source paths, canonical URLs, and repo metadata."""

    def __init__(self, main_config: Dict[str, Any], runner: Callable[..., RunResult] = default_run_command):
        """Initialize git source handling with config and a command runner."""
        self.main_config = main_config
        self.runner = runner
        self.source_cache_dir = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "deez" / "source"

    @staticmethod
    def sanitize_branch(branch: Optional[str]) -> str:
        """Convert a git branch name into a filesystem-safe identifier."""
        if not branch:
            return "main"
        return branch.replace("/", "-").replace(" ", "-")

    @staticmethod
    def source_cache_path(cache_root: Union[str, Path], owner: str, name: str, branch: str) -> str:
        """Compute a cache path for a git source based on owner/name/branch."""
        safe_branch = GitHandler.sanitize_branch(branch)
        owner_part = (owner or "unknown").lower()
        name_part = (name or "unknown").lower()
        return str(Path(cache_root) / "deez" / "source" / f"{owner_part}-{name_part}-{safe_branch}")

    @staticmethod
    def normalize_git_url(git_url: Optional[str]) -> str:
        """Normalize git URLs into a consistent lower-case repository path."""
        normalized = str(git_url or "").strip()
        if not normalized:
            return ""
        if "://" in normalized:
            normalized = normalized.split("://", 1)[1]
        if normalized.startswith("git@"):
            normalized = normalized[4:]
        if ":" in normalized and "/" not in normalized.split(":", 1)[0]:
            host, path = normalized.split(":", 1)
            normalized = f"{host}/{path}"
        normalized = normalized.rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        return normalized.lower()

    def is_git_repo(self, repo_path: Union[str, Path]) -> bool:
        """Return True if the given path is a git repository."""
        success, out, _ = self.runner(["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"])
        return success and out.strip() == "true"

    def get_remote_url(self, repo_path: Union[str, Path], remote: str = "origin") -> str:
        """Return the configured remote URL for a git repository."""
        success, out, _ = self.runner(["git", "-C", str(repo_path), "config", "--get", f"remote.{remote}.url"])
        return out.strip() if success else ""

    @staticmethod
    def is_source_url(source: Optional[Union[str, Path]]) -> bool:
        """Return True if the source string is a URL pointing to source content."""
        parsed = urllib.parse.urlparse(str(source or "").strip())
        return parsed.scheme in ("http", "https", "file")

    @staticmethod
    def is_release(url: str) -> bool:
        """Return True if the URL points to an archive release artifact."""
        lowered = str(url or "").strip().lower()
        return lowered.endswith(".tar.gz") or lowered.endswith(".tgz") or lowered.endswith(".tar") or lowered.endswith(".zip")

    @staticmethod
    def file_url_to_path(url: str) -> Path:
        """Convert a file:// URL into a local filesystem path."""
        parsed = urllib.parse.urlparse(url)
        path = urllib.request.url2pathname(parsed.path or "")
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        return Path(path).expanduser()

    @staticmethod
    def archive_cache_key(source: Union[str, Path]) -> str:
        """Compute a stable cache key for an archive source path or URL."""
        source_text = str(source).strip()
        if not source_text:
            return "source"
        parsed = urllib.parse.urlparse(source_text)
        if parsed.scheme == "file":
            local_path = GitHandler.file_url_to_path(source_text)
            if local_path.exists():
                stat = local_path.stat()
                source_text = f"file:{local_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        else:
            candidate = Path(os.path.expandvars(os.path.expanduser(source_text)))
            if candidate.exists() and candidate.is_file():
                stat = candidate.stat()
                source_text = f"file:{candidate.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        return digest[:16]

    @staticmethod
    def _archive_filename(source: Union[str, Path]) -> str:
        source_text = str(source).strip()
        parsed = urllib.parse.urlparse(source_text)
        if parsed.scheme:
            name = Path(parsed.path).name
        else:
            name = Path(source_text).name
        return name or "source.tar.gz"

    @staticmethod
    def _discover_extracted_root(extract_root: Union[str, Path]) -> Path:
        root = Path(extract_root)
        entries = [entry for entry in root.iterdir() if entry.name != "__MACOSX"] if root.exists() else []
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return root

    @staticmethod
    def _extract_archive(archive_path: Union[str, Path], extract_root: Union[str, Path]) -> None:
        archive = Path(archive_path)
        destination = Path(extract_root)
        destination.mkdir(parents=True, exist_ok=True)
        archive_name = archive.name.lower()
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(destination)
            return
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(path=destination)

    def prepare_archive_source(self, source: Union[str, Path]) -> str:
        """Download or copy and extract a remote/local archive source into cache."""
        source_text = str(source).strip()
        cache_key = self.archive_cache_key(source_text)
        cache_root = self.source_cache_dir / "archives" / cache_key
        extract_root = cache_root / "content"
        archive_name = self._archive_filename(source_text)
        archive_path = cache_root / archive_name

        if extract_root.exists() and any(extract_root.iterdir()):
            return str(self._discover_extracted_root(extract_root))

        shutil.rmtree(cache_root, ignore_errors=True)
        cache_root.mkdir(parents=True, exist_ok=True)

        if self.is_source_url(source_text):
            parsed = urllib.parse.urlparse(source_text)
            if parsed.scheme == "file":
                local_archive = self.file_url_to_path(source_text)
                if not local_archive.is_file():
                    raise RuntimeError(f"Source archive '{local_archive}' does not exist.")
                shutil.copy2(local_archive, archive_path)
            else:
                urllib.request.urlretrieve(source_text, str(archive_path))
        else:
            local_archive = Path(os.path.expandvars(os.path.expanduser(source_text)))
            if not local_archive.is_file():
                raise RuntimeError(f"Source archive '{local_archive}' does not exist.")
            shutil.copy2(local_archive, archive_path)

        self._extract_archive(archive_path, extract_root)
        return str(self._discover_extracted_root(extract_root))

    def prepare_git_source(self, git_url: str, target_branch: str) -> str:
        """Clone or refresh a git source repository into the local cache."""
        repo_owner, repo_name = self.get_git_owner_name(git_url)
        cache_root = os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        source_root_path = Path(self.source_cache_path(cache_root, repo_owner, repo_name, target_branch))
        if not source_root_path.exists():
            self.git_clone(git_url, source_root_path, target_branch)
        self.git_fetch(source_root_path, target_branch)
        self.git_pull(source_root_path, target_branch)
        self.git_checkout(source_root_path, target_branch)
        return str(source_root_path)

    def prepare_source(
        self,
        source_dir: Union[str, Path],
        git_url: Optional[str],
        target_branch: str,
        *,
        explicit_source_path: bool = False,
    ) -> str:
        """Resolve a source path or git URL to a local source directory."""
        source_text = os.path.expandvars(os.path.expanduser(str(source_dir or "").strip()))
        if explicit_source_path and source_text:
            if self.is_source_url(source_text):
                if self.is_release(source_text):
                    return self.prepare_archive_source(source_text)
                if urllib.parse.urlparse(source_text).scheme == "file":
                    local_path = self.file_url_to_path(source_text)
                    if local_path.is_dir():
                        source_text = str(local_path)
                    elif local_path.is_file() and self.is_release(str(local_path)):
                        return self.prepare_archive_source(local_path)
                    else:
                        raise RuntimeError(f"Source path '{local_path}' is not a directory or supported archive.")
                else:
                    return self.prepare_git_source(source_text, target_branch)
            source_path = Path(source_text).expanduser()
            if source_path.exists() and source_path.is_file():
                if self.is_release(str(source_path)):
                    return self.prepare_archive_source(source_path)
                raise RuntimeError(f"Source path '{source_path}' is not a directory or supported archive.")
        else:
            source_path = Path(source_text).expanduser()
        if source_path.exists():
            if not source_path.is_dir():
                raise RuntimeError(f"Source path '{source_path}' is not a directory.")
            if not explicit_source_path:
                if git_url:
                    if self.is_release(git_url):
                        return self.prepare_archive_source(git_url)
                    return self.prepare_git_source(git_url, target_branch)
                return str(source_path)
            if not self.is_git_repo(source_path):
                LOG.debug("Using explicit non-git source path: %s", source_path)
                return str(source_path)
            if not git_url:
                LOG.debug("Using explicit git source path without configured remote: %s", source_path)
                return str(source_path)
            source_remote = self.normalize_git_url(self.get_remote_url(source_path))
            configured_remote = self.normalize_git_url(git_url)
            if not source_remote or source_remote != configured_remote:
                LOG.debug(
                    "Skipping git refresh for %s: remote=%s configured=%s",
                    source_path,
                    source_remote or "(missing)",
                    configured_remote or "(missing)",
                )
                return str(source_path)
            self.git_fetch(source_path, target_branch)
            self.git_pull(source_path, target_branch)
            self.git_checkout(source_path, target_branch)
            return str(source_path)
        if not git_url:
            raise RuntimeError(f"Source directory '{source_path}' does not exist and no git URL provided.")
        if explicit_source_path:
            self.git_clone(git_url, source_path, target_branch)
            return str(source_path)
        if self.is_release(git_url):
            return self.prepare_archive_source(git_url)
        return self.prepare_git_source(git_url, target_branch)

    @staticmethod
    def get_git_owner_name(git_url: str) -> Tuple[str, str]:
        """Extract owner and repository name from a git URL."""
        parts = git_url.rstrip(".git").split("/")
        owner = parts[-2] if len(parts) > 1 else "unknown"
        name = parts[-1].replace(".git", "") if len(parts) > 0 else "unknown"
        return owner, name

    @staticmethod
    def get_git_version(repo_path: Union[str, Path]) -> str:
        """Return the latest git tag or commit hash for a repository."""
        try:
            res = subprocess.run(["git", "-C", str(repo_path), "describe", "--tags", "--abbrev=0"], check=True, capture_output=True, text=True)
            tag = res.stdout.strip()
            if tag:
                LOG.debug("Git tag found: %s", tag)
                return tag
        except Exception:
            LOG.debug("No git tag found for %s", repo_path)
        try:
            res = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
            LOG.debug("Git rev-parse returned for %s", repo_path)
            return res.stdout.strip()
        except Exception:
            LOG.debug("Failed to get git version for %s", repo_path)
            return "unknown"

    def git_clone(self, url: str, target_dir: Union[str, Path], branch: Optional[str] = None) -> None:
        """Clone a git repository to a target directory."""
        if not url.startswith("http"):
            url = "https://" + url
        LOG.debug("[GIT] clone %s -> %s", url, target_dir)
        cmd = ["git", "clone", "--progress", "--depth", "1", "--single-branch", "--no-tags"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([url, str(target_dir)])
        success, out, err = self.runner(cmd)
        if not success:
            raise RuntimeError(f"git clone failed: {err or out}")

    def git_fetch(self, repo_path: Union[str, Path], target_branch: Optional[str] = None) -> None:
        """Fetch updates for a local git repository."""
        LOG.debug("[GIT] fetch --all (%s)", repo_path)
        if target_branch:
            cmd = ["git", "-C", str(repo_path), "fetch", "origin", target_branch, "--depth", "1", "--progress"]
        else:
            cmd = ["git", "-C", str(repo_path), "fetch", "--all", "--progress"]
        success, out, err = self.runner(cmd)
        if not success:
            raise RuntimeError(f"git fetch failed: {err or out}")

    def git_pull(self, repo_path: Union[str, Path], target_branch: str) -> None:
        """Pull the latest changes for a specific branch."""
        LOG.debug("[GIT] pull origin %s (%s)", target_branch, repo_path)
        cmd = ["git", "-C", str(repo_path), "pull", "--rebase", "origin", target_branch, "--progress"]
        success, out, err = self.runner(cmd)
        if not success:
            raise RuntimeError(f"git pull failed: {err or out}")

    def git_checkout(self, repo_path: Union[str, Path], branch: str) -> None:
        """Checkout or recreate a branch in a local git repository."""
        LOG.debug("[GIT] checkout %s (%s)", branch, repo_path)
        try:
            success, out, err = self.runner(["git", "-C", str(repo_path), "checkout", branch])
            if not success:
                success2, out2, err2 = self.runner(["git", "-C", str(repo_path), "checkout", "-B", branch, f"origin/{branch}"])
                if not success2:
                    raise RuntimeError(err2 or out2)
        except Exception:
            raise

    def handle(self, url: str) -> None:
        """Download and prepare a URL or git repo source based on its form."""
        if url.startswith("http"):
            url = url.split("://", 1)[1]
        if self.is_release(url):
            version = url.split("/")[-1].split(".tar.gz")[0] if ".tar.gz" in url else url.split("/")[-1].split(".zip")[0]
            repo_owner = url.split("/")[1]
            repo_name = url.split("/")[2]
            LOG.debug("Repository Owner: %s", repo_owner)
            LOG.debug("Repository Name: %s", repo_name)
            LOG.debug("Version: %s", version)
            source_root_path = self.source_cache_dir / f"{repo_owner.lower()}-{repo_name.lower()}-{version}"
            if not source_root_path.exists():
                source_root_path.mkdir(parents=True, exist_ok=True)
                tarball_path = source_root_path / f"{version}.tar.gz"
                urllib.request.urlretrieve(f"https://{url}", str(tarball_path))
                with tarfile.open(tarball_path, "r:gz") as tar:
                    tar.extractall(path=source_root_path)
        else:
            repo_owner = url.split("/")[1]
            repo_name = url.split("/")[2].replace(".git", "")
            target_branch = self.main_config.get("branch") or self.main_config.get("git_branch", "main")
            safe_branch = self.sanitize_branch(target_branch)
            LOG.debug("Repository Owner: %s", repo_owner)
            LOG.debug("Repository Name: %s", repo_name)
            LOG.debug("Branch: %s", target_branch)
            source_root_path = Path(self.source_cache_path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache")), repo_owner, repo_name, safe_branch))
            if not source_root_path.exists():
                self.git_clone(url, source_root_path, target_branch)
            self.git_fetch(source_root_path, target_branch)
            self.git_pull(source_root_path, target_branch)
            self.git_checkout(source_root_path, target_branch)

    def get_githash(self, repo_path: Union[str, Path]) -> str:
        """Return the current git HEAD hash for the given repository."""
        try:
            success, out, err = self.runner(["git", "-C", str(repo_path), "rev-parse", "HEAD"])
            if success:
                LOG.debug("Got git hash for %s", repo_path)
                return out.strip()
        except Exception as e:
            LOG.warning("Could not get git hash for %s: %s", repo_path, e)
        return ""


class InteractiveMenu:
    """Terminal prompt helpers for interactive dot selection and confirmation."""

    _RESET = "\033[0m"
    _BOLD = "\033[1m"
    _DIM = "\033[2m"
    _CYAN = "\033[0;36m"
    _YELLOW = "\033[0;33m"

    @classmethod
    def _print_options(cls, options: List[Any], labels: Optional[List[str]] = None, extra: str = "") -> None:
        for i, opt in enumerate(options, 1):
            display = labels[i - 1] if labels else str(opt)
            UI.plain(f"  {cls._CYAN}[{i}]{cls._RESET} {display}")
        if extra:
            UI.plain(extra)

    @classmethod
    def confirm(cls, prompt: str, default: bool = False) -> bool:
        """Prompt the user for a yes/no confirmation."""
        hint = f"{cls._BOLD}Y{cls._RESET}/n" if default else f"y/{cls._BOLD}N{cls._RESET}"
        raw = UI.read_input(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")

    @classmethod
    def choose_one(cls, prompt: str, options: List[Any], labels: Optional[List[str]] = None, allow_cancel: bool = True) -> Optional[Any]:
        """Prompt the user to choose a single option from a list."""
        if not options:
            return None
        was_paused = UI.pause_loader()
        try:
            cls._print_options(options, labels, extra=f"  {cls._DIM}[0] Cancel{cls._RESET}" if allow_cancel else "")
            while True:
                raw = input(f"{prompt}: ").strip()
                if allow_cancel and raw == "0":
                    return None
                try:
                    idx = int(raw) - 1
                    if 0 <= idx < len(options):
                        return options[idx]
                except ValueError:
                    pass
                UI.plain(f"  Enter 1–{len(options)}" + (" or 0 to cancel." if allow_cancel else "."))
        finally:
            UI.resume_loader(was_paused)

    @classmethod
    def choose_many(cls, prompt: str, options: List[Any], labels: Optional[List[str]] = None, allow_all: bool = True) -> List[Any]:
        """Prompt the user to choose multiple options or ranges from a list."""
        if not options:
            return []
        was_paused = UI.pause_loader()
        try:
            cls._print_options(options, labels)
            hint = f"numbers or ranges like 1,3,5 or 1-2,4-6{', ' + cls._BOLD + 'all' + cls._RESET if allow_all else ''}; Enter to cancel"
            while True:
                raw = input(f"{prompt} ({hint}): ").strip()
                if not raw:
                    return []
                if raw == "0":
                    return []
                if allow_all and raw.lower() == "all":
                    return list(options)
                chosen_indices: List[int] = []
                seen = set()
                valid = True
                for part in raw.split(","):
                    token = part.strip()
                    if not token:
                        valid = False
                        break
                    if "-" in token:
                        start_text, end_text = token.split("-", 1)
                        if not start_text.strip() or not end_text.strip():
                            valid = False
                            break
                        try:
                            start_idx = int(start_text) - 1
                            end_idx = int(end_text) - 1
                        except ValueError:
                            valid = False
                            break
                        if start_idx < 0 or end_idx < 0 or start_idx > end_idx or end_idx >= len(options):
                            valid = False
                            break
                        for idx in range(start_idx, end_idx + 1):
                            if idx not in seen:
                                chosen_indices.append(idx)
                                seen.add(idx)
                        continue
                    try:
                        idx = int(token) - 1
                    except ValueError:
                        valid = False
                        break
                    if idx < 0 or idx >= len(options):
                        valid = False
                        break
                    if idx not in seen:
                        chosen_indices.append(idx)
                        seen.add(idx)
                if valid:
                    return [options[idx] for idx in chosen_indices]
                UI.plain(f"  Enter 1–{len(options)}, comma-separated, or ranges like 1-3." + (" Type all to select everything." if allow_all else ""))
        finally:
            UI.resume_loader(was_paused)


class DeezCLI:
    """Programmatic entrypoint exposing deez-dots query and management APIs."""

    def __init__(
        self,
        args: argparse.Namespace,
        main_config: Dict[str, Any],
        source_dir: str,
        target_root: str,
        version: str,
        available_package_managers: List[str],
        distribution: str,
        package_manager_instance: Optional[PackageManager] = None,
        manifest_manager: Optional[ManifestManager] = None,
        cache_manager: Optional[CacheManager] = None,
    ):
        self.args = args
        self.main_config = main_config
        self.source_dir = source_dir
        self.target_root = target_root
        self.version = version
        self.available_package_managers = available_package_managers
        self.distribution = distribution
        global_config = main_config.get("global", {})
        self.global_config = global_config if isinstance(global_config, dict) else {}
        self._has_explicit_dot_selection = "dots" in self.global_config
        global_dots = self.global_config.get("dots")
        if self._has_explicit_dot_selection:
            self.dotfile_sections = [dot for dot in (global_dots or []) if dot in main_config and isinstance(main_config.get(dot), dict)]
        else:
            self.dotfile_sections = [key for key, value in main_config.items() if key != "global" and isinstance(value, dict)]
        self.package_manager_instance = package_manager_instance if package_manager_instance is not None else PackageManager()
        self.manifest_manager = manifest_manager if manifest_manager is not None else ManifestManager()
        self.cache_manager = cache_manager if cache_manager is not None else CacheManager()
        self._manifest_dots = self.manifest_manager.list_dots()

    @staticmethod
    def _can_prompt_for_selection() -> bool:
        stdin = getattr(os.sys, "stdin", None)
        stdout = getattr(os.sys, "stdout", None)
        try:
            return bool(stdin and stdout and stdin.isatty() and stdout.isatty())
        except Exception:
            return False

    def _dot_description(self, dot: str) -> str:
        dot_data = self.main_config.get(dot, {})
        if not isinstance(dot_data, dict):
            return ""
        return _normalize_description(dot_data.get("description"))

    @staticmethod
    def _hook_cwd(source_dir: Optional[Union[str, Path]]) -> Optional[str]:
        if not source_dir:
            return None
        path = Path(source_dir).expanduser()
        return str(path) if path.exists() else None

    @staticmethod
    def _file_entry_label(dot: str, file_entry: Dict[str, Any]) -> str:
        label_source = file_entry.get("src") or file_entry.get("paths") or file_entry.get("dst") or file_entry.get("source_root") or "file entry"
        if isinstance(label_source, list):
            parts = [str(item).strip() for item in label_source if str(item).strip()]
            preview = ", ".join(parts[:2])
            if len(parts) > 2:
                preview = f"{preview}, ..."
        else:
            preview = str(label_source).strip()
        return f"file entry in '{dot}' ({preview or 'file entry'})"

    def _run_scoped_pre_command(
        self,
        command: Optional[str],
        *,
        scope_label: str,
        cwd: Optional[Union[str, Path]] = None,
    ) -> Optional[str]:
        if not command:
            return None
        writer = WriteDots()
        try:
            writer.execute_commands([command], cwd=cwd, soft_fail=False)
        except Exception as exc:
            message = str(exc).strip() or "command failed"
            LOG.warning("[PRE] %s failed: %s", scope_label, message)
            return message
        LOG.debug("[PRE] %s passed", scope_label)
        return None

    def _require_pre_command(
        self,
        command: Optional[str],
        *,
        scope_label: str,
        cwd: Optional[Union[str, Path]] = None,
    ) -> None:
        failure = self._run_scoped_pre_command(command, scope_label=scope_label, cwd=cwd)
        if failure is None:
            return
        UI.error(f"{scope_label} pre_command failed: {command}: {failure}")
        raise SystemExit(1)

    def _skip_on_failed_pre_command(
        self,
        command: Optional[str],
        *,
        scope_label: str,
        cwd: Optional[Union[str, Path]] = None,
    ) -> bool:
        failure = self._run_scoped_pre_command(command, scope_label=scope_label, cwd=cwd)
        if failure is None:
            return False
        UI.warn(f"Skipping {scope_label}: pre_command failed: {command}: {failure}")
        return True

    @staticmethod
    def _announce_dry_run_pre_command(command: Optional[str], *, scope_label: str) -> None:
        if not command:
            return
        UI.plain(f"[DRY RUN] Would run {scope_label} pre_command: {command} (assuming success)")

    @staticmethod
    def _normalize_conflict_names(value: Any) -> List[str]:
        if not value:
            return []
        values = value if isinstance(value, list) else [value]
        normalized: List[str] = []
        for item in values:
            name = str(item or "").strip()
            if name and name not in normalized:
                normalized.append(name)
        return normalized

    @staticmethod
    def _normalize_conflicted_paths(value: Any) -> List[str]:
        if not value:
            return []
        expanded = DeezUtils.expand(value)
        values = expanded if isinstance(expanded, list) else [expanded]
        normalized: List[str] = []
        for item in values:
            path_text = str(item or "").strip()
            if path_text and path_text not in normalized:
                normalized.append(path_text)
        return normalized

    def _dot_owner(self, dot: str) -> str:
        dot_data = self.main_config.get(dot, {})
        if not isinstance(dot_data, dict):
            dot_data = {}
        return str(dot_data.get("owner") or self.global_config.get("owner") or "?")

    def _dot_selection_header(self, action_label: str) -> str:
        global_description = _normalize_description(self.global_config.get("description"))
        owner_label = str(self.global_config.get("owner") or "?")
        raw = global_description if global_description else f"{action_label} dots from {owner_label}"
        return self._selection_color(raw, UI._MAGENTA, bold=True)

    def _selection_color(self, text: str, color: str, *, bold: bool = False) -> str:
        if not UI._colors_enabled():
            return text
        prefix = f"{InteractiveMenu._BOLD}{color}" if bold else color
        return f"{prefix}{text}{InteractiveMenu._RESET}"

    def _dot_selection_label(self, dot: str) -> str:
        dot_data = self.main_config.get(dot, {})
        if not isinstance(dot_data, dict):
            dot_data = {}
        file_count = len(list(self._iter_raw_file_entries(dot_data)))
        if not file_count and dot_data.get("paths"):
            file_count = 1
        owner = self._dot_owner(dot)
        colored_dot = self._selection_color(dot, InteractiveMenu._CYAN, bold=True)
        colored_owner = self._selection_color(owner, InteractiveMenu._YELLOW)
        colored_count = self._selection_color(str(file_count), UI._GREEN, bold=True)
        label = f"{colored_dot} by {colored_owner} with {colored_count} entries"
        description = self._dot_description(dot)
        if description:
            colored_description = self._selection_color(f"({description})", UI._MAGENTA)
            label = f"{colored_dot} {colored_description} by {colored_owner} with {colored_count} entries"
        return label

    def _installed_dot_selection_label(self, dot: str) -> str:
        desc = self.manifest_manager.load_desc(dot)
        tracked_files = self.manifest_manager.get_files(dot)
        owner = self._selection_color(str(desc.get("owner", "?")), InteractiveMenu._YELLOW)
        colored_dot = self._selection_color(dot, InteractiveMenu._CYAN, bold=True)
        colored_count = self._selection_color(str(len(tracked_files)), UI._GREEN, bold=True)
        install_ts = self._selection_color(str(desc.get("installdate", "?")), UI._MAGENTA)
        missing = sum(1 for path in tracked_files if not self._path_exists_or_link(Path(path)))
        label = f"{colored_dot} by {owner} with {colored_count} tracked files installed {install_ts}"
        if missing:
            colored_missing = self._selection_color(str(missing), UI._RED, bold=True)
            label = f"{label} ({colored_missing} missing)"
        return label

    def _restorable_dot_selection_label(self, dot: str, snapshot_count: int) -> str:
        colored_dot = self._selection_color(dot, InteractiveMenu._CYAN, bold=True)
        colored_count = self._selection_color(str(snapshot_count), UI._GREEN, bold=True)
        return f"{colored_dot} with {colored_count} snapshots"

    def _cache_dot_selection_label(self, dot: str, version_count: int) -> str:
        colored_dot = self._selection_color(dot, InteractiveMenu._CYAN, bold=True)
        colored_count = self._selection_color(str(version_count), UI._GREEN, bold=True)
        return f"{colored_dot} with {colored_count} cached versions"

    def _cache_version_selection_label(self, entry: CacheEntry) -> str:
        version = self._selection_color(entry.version, UI._GREEN, bold=True)
        githash = self._selection_color(entry.githash, InteractiveMenu._YELLOW)
        builddate = self._selection_color(entry.builddate, UI._MAGENTA)
        origin = f" [{entry.origin}]" if entry.origin else ""
        return f"{version:<10} {githash:<8} {builddate:<16}{origin}"

    @staticmethod
    def _path_exists_or_link(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    @staticmethod
    def _path_content_hash(path: Path) -> Optional[str]:
        try:
            if path.is_symlink():
                target = os.readlink(path)
                return hashlib.sha256(f"symlink:{target}".encode("utf-8")).hexdigest()
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                return digest.hexdigest()
            if path.is_dir():
                digest = hashlib.sha256()
                for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
                    rel = child.relative_to(path).as_posix()
                    if child.is_symlink():
                        digest.update(f"link:{rel}:{os.readlink(child)}".encode("utf-8"))
                        continue
                    if child.is_dir():
                        digest.update(f"dir:{rel}".encode("utf-8"))
                        continue
                    digest.update(f"file:{rel}".encode("utf-8"))
                    with child.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                return digest.hexdigest()
        except OSError:
            return None
        return None

    @staticmethod
    def _bundle_entry_hash(bundle_path: Path, source_path: str) -> Optional[str]:
        normalized = str(source_path or "").strip().replace("\\", "/")
        if not normalized:
            return None
        member_name = f"data/{normalized}"
        try:
            with tarfile.open(bundle_path, "r:gz") as tar:
                try:
                    member = tar.getmember(member_name)
                except KeyError:
                    return None
                if member.isdir():
                    return hashlib.sha256(f"dir:{normalized}".encode("utf-8")).hexdigest()
                if member.issym() or member.islnk():
                    return hashlib.sha256(f"symlink:{member.linkname or ''}".encode("utf-8")).hexdigest()
                extracted = tar.extractfile(member)
                if extracted is None:
                    return None
                digest = hashlib.sha256()
                for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                    digest.update(chunk)
                return digest.hexdigest()
        except (OSError, tarfile.TarError):
            return None

    @staticmethod
    def _display_path_parts(path_text: str, home: Optional[Path] = None) -> List[str]:
        path = Path(path_text).expanduser()
        home = home or Path.home()
        try:
            relative = path.relative_to(home)
            return ["~", *relative.parts] or ["~"]
        except ValueError:
            parts = list(path.parts)
            if parts and parts[0] == os.sep:
                return ["/", *parts[1:]]
            return parts or [str(path)]

    @staticmethod
    def _insert_tree_path(tree: Dict[str, Any], parts: List[str], *, owner: Optional[str] = None, exists: bool = True) -> None:
        node = tree
        for index, part in enumerate(parts):
            children = node.setdefault("children", {})
            child = children.setdefault(part, {"children": {}, "owners": [], "exists": True})
            child["exists"] = child.get("exists", True) and exists
            if index == len(parts) - 1 and owner:
                owners = child.setdefault("owners", [])
                if owner not in owners:
                    owners.append(owner)
            node = child

    def _tree_node_label(self, name: str, *, is_dir: bool, exists: bool, owners: Optional[List[str]] = None) -> str:
        owners = owners or []
        color = UI._BLUE if is_dir else (UI._GREEN if exists else UI._RED)
        label = self._selection_color(name, color, bold=is_dir or not exists)
        if owners:
            owner_text = ", ".join(sorted(owners))
            label = f"{label} {self._selection_color(f'[{owner_text}]', InteractiveMenu._YELLOW)}"
        return label

    def _render_tree_lines(self, node: Dict[str, Any], prefix: str = "") -> List[str]:
        def collapse_chain(name: str, child: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            current_name = name
            current_child = child
            while True:
                children = current_child.get("children", {})
                if len(children) != 1 or current_child.get("owners"):
                    break
                next_name, next_child = next(iter(children.items()))
                current_name = f"{current_name}/{next_name}"
                current_child = next_child
            return current_name, current_child

        children = sorted(node.get("children", {}).items())
        lines: List[str] = []
        for index, (name, child) in enumerate(children):
            is_last = index == len(children) - 1
            branch = "`-- " if is_last else "|-- "
            extension = "    " if is_last else "|   "
            collapsed_name, collapsed_child = collapse_chain(name, child)
            label = self._tree_node_label(
                collapsed_name,
                is_dir=bool(collapsed_child.get("children")),
                exists=bool(collapsed_child.get("exists", True)),
                owners=collapsed_child.get("owners", []),
            )
            lines.append(f"{prefix}{branch}{label}")
            lines.extend(self._render_tree_lines(collapsed_child, prefix + extension))
        return lines

    def _resolve_installed_dot_targets(self, requested: Optional[str]) -> List[str]:
        dots = sorted(self.manifest_manager.list_dots())
        if not dots:
            UI.plain("No dots found in the manifest.")
            return []
        target = str(requested or "").strip()
        if not target or target.lower() == "all":
            return dots
        if target not in dots:
            UI.error(f"Dot '{target}' not found in installed manifest.")
            return []
        return [target]

    def _render_filetree_for_dot(self, dot: str) -> None:
        entries = [entry for entry in self.manifest_manager.get_file_entries(dot) if entry.get("installed", True) and entry.get("dst")]
        desc = self.manifest_manager.load_desc(dot)
        owner = self._selection_color(str(desc.get("owner", "?")), InteractiveMenu._YELLOW)
        colored_dot = self._selection_color(dot, InteractiveMenu._CYAN, bold=True)
        UI.plain(f"\n{colored_dot} by {owner}")
        if not entries:
            UI.plain("  (no tracked files)")
            return
        tree: Dict[str, Any] = {"children": {}}
        for entry in entries:
            dst = str(entry.get("dst") or "").strip()
            if not dst:
                continue
            self._insert_tree_path(tree, self._display_path_parts(dst, home=Path(self.target_root)), exists=self._path_exists_or_link(Path(dst)))
        for line in self._render_tree_lines(tree):
            UI.plain(f"  {line}")

    def _do_filetree(self, target: Optional[str] = None) -> None:
        dots = self._resolve_installed_dot_targets(target)
        if not dots:
            return
        if len(dots) > 1:
            UI.plain("Tracked file tree:")
            tree: Dict[str, Any] = {"children": {}}
            for dot in dots:
                for entry in self.manifest_manager.get_file_entries(dot):
                    if not entry.get("installed", True):
                        continue
                    dst = str(entry.get("dst") or "").strip()
                    if not dst:
                        continue
                    self._insert_tree_path(tree, self._display_path_parts(dst, home=Path(self.target_root)), owner=dot, exists=self._path_exists_or_link(Path(dst)))
            for line in self._render_tree_lines(tree):
                UI.plain(line)
            return
        self._render_filetree_for_dot(dots[0])

    def _healthcheck_dot(self, dot: str) -> Dict[str, Any]:
        desc = self.manifest_manager.load_desc(dot)
        tracked_entries = [entry for entry in self.manifest_manager.get_file_entries(dot) if entry.get("installed", True) and entry.get("dst")]
        expected_bundle_hash = str(desc.get("hash") or "").strip()
        bundle_path = self.cache_manager.bundle_path_for_hash(expected_bundle_hash)
        bundle_available = False
        if bundle_path is not None:
            actual_bundle_hash = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
            bundle_available = actual_bundle_hash == expected_bundle_hash
        status = {
            "dot": dot,
            "owner": desc.get("owner", "?"),
            "tracked": len(tracked_entries),
            "ok": [],
            "missing": [],
            "changed": [],
            "unverified": [],
            "bundle_available": bundle_available,
        }
        for entry in tracked_entries:
            dst = str(entry.get("dst") or "").strip()
            if not dst:
                continue
            path = Path(dst)
            if not self._path_exists_or_link(path):
                status["missing"].append(dst)
                continue
            expected_hash = self._bundle_entry_hash(bundle_path, entry.get("src")) if bundle_available and bundle_path else None
            if expected_hash is not None:
                actual_hash = self._path_content_hash(path)
                if actual_hash != expected_hash:
                    status["changed"].append(dst)
                    continue
            else:
                status["unverified"].append(dst)
            status["ok"].append(dst)
        return status

    def _do_healthcheck(self, target: Optional[str] = None) -> None:
        dots = self._resolve_installed_dot_targets(target)
        if not dots:
            return
        total_missing = 0
        total_changed = 0
        total_unverified = 0
        UI.plain("Healthcheck:")
        for dot in dots:
            result = self._healthcheck_dot(dot)
            total_missing += len(result["missing"])
            total_changed += len(result["changed"])
            total_unverified += len(result["unverified"])
            owner = self._selection_color(str(result["owner"]), InteractiveMenu._YELLOW)
            colored_dot = self._selection_color(result["dot"], InteractiveMenu._CYAN, bold=True)
            tracked = self._selection_color(str(result["tracked"]), UI._GREEN, bold=True)
            UI.plain(f"  {colored_dot} by {owner} with {tracked} tracked entries")
            for missing in result["missing"]:
                UI.plain(f"    [MISSING] {missing}")
            for changed in result["changed"]:
                UI.plain(f"    [CHANGED] {changed}")
            if not result["missing"] and not result["changed"]:
                UI.plain("    [OK] manifest matches the current filesystem state")
            elif result["ok"]:
                UI.plain(f"    [OK] {len(result['ok'])} tracked paths still match")
            if result["unverified"]:
                if result["bundle_available"]:
                    UI.plain(f"    [warn] {len(result['unverified'])} tracked paths could not be matched in the cached bundle; only presence was checked")
                else:
                    UI.plain(f"    [warn] {len(result['unverified'])} tracked paths could not be verified because the cached bundle is unavailable; only presence was checked")
        UI.plain(f"Summary: missing={total_missing} changed={total_changed} unverified={total_unverified}")

    def _resolve_config_dot_targets(self, action_label: str) -> List[str]:
        available = list(self.dotfile_sections)
        if not available:
            UI.error("No dot sections found in the config.")
            return []
        if self._has_explicit_dot_selection or len(available) <= 1 or not self._can_prompt_for_selection():
            return available
        labels = [self._dot_selection_label(dot) for dot in available]
        UI.plain("\nDiscovered dots:")
        UI.plain(f"  {self._dot_selection_header(action_label)}")
        selected = InteractiveMenu.choose_many(f"Select dots to {action_label}", available, labels=labels)
        if not selected:
            UI.info("Cancelled.")
            return []
        return selected

    def _resolve_requested_config_dot_targets(self, requested: RequestedSections, action_label: str) -> List[str]:
        allow_installed_fallback = action_label == "export"
        if requested is None:
            return self._resolve_config_dot_targets(action_label)
        available = list(self.dotfile_sections)
        installed = self.manifest_manager.list_dots() if allow_installed_fallback else []
        if requested is _ALL_SECTIONS_REQUESTED:
            if available:
                return available
            if installed:
                return installed
            UI.error("No dot sections found in the config.")
            return []
        if not available and not installed:
            UI.error("No dot sections found in the config.")
            return []
        eligible = set(available)
        if allow_installed_fallback:
            eligible.update(installed)
        invalid = [dot for dot in requested if dot not in eligible]
        for dot in invalid:
            if allow_installed_fallback:
                UI.error(f"Dot '{dot}' not found in config or installed manifest.")
            else:
                UI.error(f"Dot '{dot}' not found in config.")
        return [dot for dot in requested if dot in eligible]

    def query_selectable_config_dots(self, action_label: str = "bundle") -> List[Dict[str, str]]:
        """Return selectable config dot metadata for interactive selection."""
        sections = sorted(self.dotfile_sections)
        return [
            {"dot": dot, "label": self._dot_selection_label(dot), "action": action_label}
            for dot in sections
        ]

    def query_selectable_installed_dots(self) -> List[Dict[str, str]]:
        """Return selectable installed dots for interactive restore or uninstall flows."""
        dots = sorted(self.manifest_manager.list_dots())
        return [
            {"dot": dot, "label": self._installed_dot_selection_label(dot)}
            for dot in dots
        ]

    def query_selectable_backup_dots(self) -> List[Dict[str, str]]:
        """Return selectable backup dot options for restore workflows."""
        dots = self._backup_dots()
        return [
            {"dot": dot, "label": self._restorable_dot_selection_label(dot, len(self._list_snapshots(dot)))}
            for dot in dots
        ]

    def query_selectable_cache_dots(self) -> List[Dict[str, str]]:
        """Return selectable cached dots and counts for downgrade or restore selection."""
        cached_by_dot = self.cache_manager.bundles_by_dot()
        return [
            {"dot": dot, "label": self._cache_dot_selection_label(dot, len(entries))}
            for dot, entries in sorted(cached_by_dot.items())
        ]

    def query_selectable_cache_versions(self, dot: str) -> List[Dict[str, str]]:
        """Return selectable cached bundle versions for a specific dot."""
        cached_by_dot = self.cache_manager.bundles_by_dot()
        entries = cached_by_dot.get(dot, [])
        return [
            {"path": str(entry.path), "label": self._cache_version_selection_label(entry)}
            for entry in entries
        ]

    def _backup_user_base(self) -> Path:
        xdg_data = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return xdg_data / "deez" / "backup" / "user"

    def _list_snapshots(self, dot: str) -> List[str]:
        base = self._backup_user_base()
        if not base.is_dir():
            return []
        prefix = f"{dot}."
        results: List[str] = []
        for dirname in os.listdir(base):
            if dirname != dot and not dirname.startswith(prefix):
                continue
            dirpath = base / dirname
            if not dirpath.is_dir():
                continue
            for fname in os.listdir(dirpath):
                if fname.endswith(".tar.gz"):
                    results.append(str(dirpath / fname))
        results.sort(reverse=True)
        return results

    def _backup_dots(self) -> List[str]:
        dots = set(self.dotfile_sections)
        base = self._backup_user_base()
        if base.is_dir():
            for dirname in os.listdir(base):
                if (base / dirname).is_dir():
                    dots.add(dirname.split(".")[0])
        return sorted(dots)

    def _backup_list(self) -> None:
        base = self._backup_user_base()
        UI.info("User backups (live file snapshots):")
        any_output = False
        for dot in self._backup_dots():
            snapshots = self._list_snapshots(dot)
            UI.plain(f"  {dot}")
            if not snapshots:
                UI.plain("    (none)")
                continue
            any_output = True
            for snap_path in snapshots:
                rel = os.path.relpath(snap_path, base) if base.is_dir() else snap_path
                UI.plain(f"    {rel}")
        if not any_output:
            UI.plain("  No backup snapshots found.")

    def _backup_prune_keep(self, keep_count: int, dots: Optional[List[str]] = None, dry_run: bool = False) -> int:
        if keep_count is None:
            keep_count = 5
        if keep_count < 0:
            UI.error("--keep must be a non-negative integer")
            return 1
        all_dots = self._backup_dots()
        if not all_dots:
            UI.info("No backup snapshots found.")
            return 0
        if dots:
            requested: List[str] = []
            for item in dots:
                for part in str(item).split(","):
                    if part.strip():
                        requested.append(part.strip())
            invalid = [s for s in requested if s not in all_dots]
            if invalid:
                UI.error(f"Unknown dot(s): {invalid}")
                return 1
            target_dots = requested
        else:
            target_dots = all_dots
        for dot in target_dots:
            snapshots = self._list_snapshots(dot)
            if len(snapshots) <= keep_count:
                LOG.debug("Keeping all backups for %s: total=%d", dot, len(snapshots))
                UI.info(f"Keeping all backups for {dot}: total={len(snapshots)}")
                continue
            to_delete = snapshots[keep_count:]
            LOG.debug("Pruning backups for %s: keep=%d, total=%d, delete=%d", dot, keep_count, len(snapshots), len(to_delete))
            UI.plain(f"Pruning backups for {dot}: keep={keep_count}, total={len(snapshots)}, delete={len(to_delete)}")
            for snap_path in to_delete:
                if dry_run:
                    UI.plain(f"  would delete: {snap_path}")
                    continue
                try:
                    Path(snap_path).unlink()
                    sha_path = Path(snap_path[: -len(".tar.gz")] + ".sha256")
                    if sha_path.exists():
                        sha_path.unlink()
                    LOG.debug("Deleted: %s", snap_path)
                except Exception as e:
                    UI.error(f"Failed to delete {snap_path}: {e}")
        UI.success("Backup prune complete")
        return 0

    def _cache_list(self) -> None:
        entries = self.cache_manager.list_entries()
        if not entries:
            UI.info("No cache entries found.")
            return
        for entry in entries:
            origin_label = f"[{entry.origin}]" if entry.origin else ""
            size_label = f"{entry.size}B" if entry.size is not None else "?B"
            UI.plain(f"  {entry.name:<14} {entry.version:<8} {entry.githash:<8} {entry.builddate:<16} {origin_label:<10} {size_label:>6}  {entry.path.name}")

    def _cache_prune_keep(self, keep_count: int = 10, dry_run: bool = False) -> int:
        return self.cache_manager.prune_keep(keep_count=keep_count, dry_run=dry_run)

    def _resolve_dep_managers(self) -> Optional[List[str]]:
        requested = getattr(self.args, "deps_managers", None) or []
        if not requested:
            return self.available_package_managers
        parsed: List[str] = []
        for item in requested:
            for part in str(item).split(","):
                manager = part.strip()
                if manager and manager not in parsed:
                    parsed.append(manager)
        invalid = [m for m in parsed if m not in self.available_package_managers]
        if invalid:
            UI.error(f"Unsupported or unavailable manager(s): {invalid}")
            return None
        return parsed

    def _check_dependency_status(self, dependency_map: Dict[str, List[str]]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        satisfied: Dict[str, List[str]] = {}
        missing: Dict[str, List[str]] = {}

        UI.set_loader_message("Checking dependency status...")
        for manager, packages in dependency_map.items():
            UI.set_loader_message(f"Checking {manager} dependencies...")
            for package in packages:
                installed = False
                if manager == "system":
                    installed = bool(shutil.which(package))
                else:
                    installed = self.package_manager_instance.query_installed(manager, package)

                if installed:
                    satisfied.setdefault(manager, []).append(package)
                    UI.success(f"{manager}: {package}")
                else:
                    missing.setdefault(manager, []).append(package)
                    UI.warn(f"{manager}: {package} missing")

        LOG.debug("Dependency status satisfied: %s", satisfied)
        LOG.debug("Dependency status missing: %s", missing)
        return satisfied, missing

    def _collect_missing_dependencies(self, selected_managers: Optional[List[str]] = None) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        managers = selected_managers if selected_managers is not None else self.available_package_managers
        UI.set_loader_message("Collecting dependency definitions...")
        all_deps = self.package_manager_instance.fetch_all_deps(self.main_config)
        UI.set_loader_message("Filtering dependencies for selected package managers...")
        filtered = self.package_manager_instance.filter_deps(managers, all_deps)
        if not filtered:
            return filtered, {}
        _, missing = self._check_dependency_status(filtered)
        LOG.debug("Filtered deps: %s", filtered)
        LOG.debug("Missing deps: %s", missing)
        return filtered, missing

    def _bundle_dependency_map(self, bundle: Dict[str, Any], file_entries: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[str]]:
        dep_blocks: List[Any] = [bundle.get("dependency"), bundle.get("depends")]
        for file_entry in file_entries or []:
            dep_blocks.append(file_entry.get("dependency"))
            dep_blocks.append(file_entry.get("depends"))
        return DeezUtils.merge_dependency_blocks(*dep_blocks)

    def _bundle_dependency_blocks(self, bundle: Dict[str, Any], file_entries: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, List[str]]]:
        dependency_blocks: List[Dict[str, List[str]]] = []
        dependency_blocks.extend(DeezUtils.normalize_dependency_blocks(bundle.get("dependency") or bundle.get("depends")))
        for file_entry in file_entries or []:
            dependency_blocks.extend(DeezUtils.normalize_dependency_blocks(file_entry.get("dependency") or file_entry.get("depends")))
        return dependency_blocks

    def _resolve_dependency_blocks(
        self,
        dependency_blocks: List[Dict[str, List[str]]],
        target_dots: List[str],
        *,
        install_intro: str,
    ) -> None:
        if getattr(self.args, "no_deps_checks", False):
            return
        if not dependency_blocks:
            return

        dependency_map, unresolved = self.package_manager_instance.resolve_dependency_blocks(
            dependency_blocks,
            self.available_package_managers,
        )
        if not dependency_map and not unresolved:
            return

        if self.available_package_managers:
            UI.progress(f"Detected package managers: {', '.join(self.available_package_managers)}")
        else:
            UI.warn("No supported package managers detected.")

        UI.progress(f"Dependency plan for: {', '.join(target_dots)}")
        for manager, packages in dependency_map.items():
            UI.plain(f"  {manager}: {', '.join(packages)}")

        satisfied, missing = self._check_dependency_status(dependency_map)

        if unresolved:
            UI.error(f"Dependency managers unavailable for: {', '.join(target_dots)}")
            for block in unresolved:
                for manager, packages in block.items():
                    UI.error(f"  {manager}: {', '.join(packages)}")
            raise SystemExit(1)

        generic_missing = missing.get("system", [])
        if generic_missing:
            UI.error("Missing unmanaged dependencies. Use manager-specific [[dot.dependency]] or install them manually:")
            UI.error(f"  system: {', '.join(generic_missing)}")
            raise SystemExit(1)

        install_missing = {manager: packages for manager, packages in missing.items() if manager != "system" and packages}
        if not install_missing:
            if missing.get("system") is None:
                UI.progress("Dependency checks complete")
            return
        if getattr(self.args, "no_deps_install", False):
            UI.warn(f"Missing dependencies for: {', '.join(target_dots)}")
            for manager, packages in install_missing.items():
                UI.warn(f"  {manager}: {', '.join(packages)}")
            UI.warn("Skipping dependency installation because --no-deps-install was provided.")
            return

        UI.progress(f"{install_intro}: {', '.join(target_dots)}")
        for manager, packages in install_missing.items():
            UI.progress(f"Installing via {manager}: {', '.join(packages)}")
        if not self.package_manager_instance.install_packages(install_missing):
            raise SystemExit(1)

    def _resolve_config_dependencies(self, selected_sections: List[str]) -> None:
        subset_config: Dict[str, Any] = {"global": dict(self.main_config.get("global", {}))}
        for section in selected_sections:
            if section in self.main_config:
                subset_config[section] = self.main_config.get(section, {})
        dependency_blocks = self.package_manager_instance.collect_dependency_blocks(subset_config)
        self._resolve_dependency_blocks(
            dependency_blocks,
            selected_sections,
            install_intro="Installing dependencies before packaging and file transfer",
        )

    def _resolve_bundle_dependencies(
        self,
        planned_installs: List[Tuple[Path, Dict[str, Any], Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[str, str]]]]],
    ) -> None:
        dependency_blocks: List[Dict[str, List[str]]] = []
        target_dots: List[str] = []
        for _bundle_path, bundle, prepared_bundle in planned_installs:
            dot, _bundle_entries, filtered_entries, _kept_pairs = prepared_bundle
            target_dots.append(dot)
            dependency_blocks.extend(self._bundle_dependency_blocks(bundle, filtered_entries))
        self._resolve_dependency_blocks(
            dependency_blocks,
            target_dots,
            install_intro="Installing dependencies before transferring files",
        )

    def _deps_check(self, selected_managers: Optional[List[str]] = None) -> int:
        filtered, missing = self._collect_missing_dependencies(selected_managers)
        if not filtered:
            UI.info("Deps: none configured for available package managers.")
            return 0
        if not missing:
            UI.success("Deps: all satisfied.")
            return 0
        UI.error("Deps: missing")
        for manager, pkgs in missing.items():
            UI.error(f"  {manager}: {', '.join(pkgs)}")
        return 1

    def _deps_update(self, selected_managers: Optional[List[str]] = None) -> int:
        managers = selected_managers if selected_managers is not None else self.available_package_managers
        all_deps = self.package_manager_instance.fetch_all_deps(self.main_config)
        filtered = self.package_manager_instance.filter_deps(managers, all_deps)
        if not filtered:
            UI.info("Deps: none configured for available package managers.")
            return 0
        failed = False
        for manager in sorted(filtered.keys()):
            try:
                LOG.debug("Updating packages via %s...", manager)
                UI.progress(f"Updating via {manager}...")
                self.package_manager_instance.update(manager)
            except Exception as e:
                failed = True
                UI.error(f"Failed to update via {manager}: {e}")
        if failed:
            return 1
        UI.success("Deps updated")
        return 0

    def _iter_raw_file_entries(self, dot_data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        for file_entry in dot_data.get("files", []):
            if isinstance(file_entry, dict):
                yield file_entry
            elif isinstance(file_entry, list):
                for nested_entry in file_entry:
                    if isinstance(nested_entry, dict):
                        yield nested_entry

    def _resolve_dot_source(
        self,
        dot_data: Dict[str, Any],
        git_handler: "GitHandler",
        default_source_url: str,
        target_branch: str,
    ) -> Tuple[str, str, str]:
        dot_source = dot_data.get("source")
        if not dot_source:
            return self.source_dir, default_source_url, target_branch
        source_text = DeezUtils.expand(dot_source)
        dot_branch = dot_data.get("branch") or dot_data.get("git_branch") or target_branch
        source_dir = git_handler.prepare_source(source_text, None, dot_branch, explicit_source_path=True)
        return source_dir, source_text, dot_branch

    def _resolve_file_entry(
        self,
        dot_data: Dict[str, Any],
        file_entry: Dict[str, Any],
        default_target_root: str,
        source_dir: Optional[str] = None,
    ) -> Tuple[str, str, List[str], List[str], str, bool, List[str]]:
        resolved_source_dir = source_dir or self.source_dir
        source_root_value = file_entry.get("source_root")
        if source_root_value:
            expanded_source_root = DeezUtils.expand(source_root_value)
            source_root = expanded_source_root if os.path.isabs(expanded_source_root) else os.path.join(resolved_source_dir, expanded_source_root)
        else:
            source_root = resolved_source_dir
        target_root = DeezUtils.expand(file_entry.get("target_root") or default_target_root)
        relative_paths = file_entry.get("paths")
        relative_paths = DeezUtils.expand(relative_paths) if relative_paths else []
        if isinstance(relative_paths, str):
            relative_paths = [relative_paths]
        ignored_paths = file_entry.get("ignored_paths")
        ignored_paths = DeezUtils.expand(ignored_paths) if ignored_paths else []
        if isinstance(ignored_paths, str):
            ignored_paths = [ignored_paths]
        action = DeezUtils.normalize_action(file_entry.get("action") or dot_data.get("action") or self.main_config.get("global", {}).get("action"))
        clean_target = bool(file_entry.get("clean_target", dot_data.get("clean_target", False)))
        conflicted_paths = self._normalize_conflicted_paths(file_entry.get("conflicted_paths"))
        return source_root, target_root, relative_paths, ignored_paths, action, clean_target, conflicted_paths

    def _iter_file_entries(self, dot_section: str, global_owner: str, global_home: str, global_version: str, source_dir: Optional[str] = None):
        dot_data = self.main_config.get(dot_section, {})
        section_owner = DeezUtils.normalize_owner(dot_data.get("owner", global_owner))
        section_version = dot_data.get("version", global_version)
        section_home = os.path.expandvars(dot_data.get("home", global_home))

        dot_files = dot_data.get("files", [])
        if not dot_files and dot_data.get("paths"):
            legacy_entry = {
                "source_root": dot_data.get("source_root"),
                "target_root": dot_data.get("target_root"),
                "paths": dot_data.get("paths"),
                "action": dot_data.get("action"),
                "clean_target": dot_data.get("clean_target", False),
            }
            yield self._resolve_file_entry(dot_data, legacy_entry, section_home, source_dir) + (section_owner, section_version)
            return

        for file_entry in self._iter_raw_file_entries(dot_data):
            yield self._resolve_file_entry(dot_data, file_entry, section_home, source_dir) + (section_owner, section_version)

    def _check_conflicts_from_bundle(
        self,
        dot: str,
        new_owner: str,
        bundle_entries: List[Dict[str, Any]],
        bundle_conflicts: Optional[List[str]] = None,
    ) -> Tuple[bool, List[Tuple[str, str]]]:
        existing_desc = self.manifest_manager.load_desc(dot)
        if existing_desc:
            existing_owner = existing_desc.get("owner", "unknown")
            if existing_owner != new_owner:
                UI.error(f"Conflict: dot '{dot}' is already installed by '{existing_owner}'.")
                UI.info("Uninstall the dot first, then re-run install.")
                return False, []
        installed_dots = set(self.manifest_manager.list_dots())
        for conflicting_dot in self._normalize_conflict_names(bundle_conflicts):
            if conflicting_dot in installed_dots and conflicting_dot != dot:
                UI.error(f"Dot conflict: '{dot}' conflicts with installed dot '{conflicting_dot}'.")
                return False, []
        owner_index = self.manifest_manager.build_owner_index()
        kept_pairs: List[Tuple[str, str]] = []
        for bundle_entry in bundle_entries:
            src_rel = bundle_entry.get("src")
            dst_abs = bundle_entry.get("dst")
            if not src_rel or not dst_abs:
                continue
            owner_dot = owner_index.get(dst_abs)
            if owner_dot and owner_dot != dot:
                UI.error(f"File conflict: {dst_abs} (owned by {owner_dot})")
                continue
            conflict_hit = False
            for conflict_path in self._normalize_conflicted_paths(bundle_entry.get("conflicted_paths")):
                owner_dot = owner_index.get(conflict_path)
                if owner_dot and owner_dot != dot:
                    UI.error(f"File conflict: {conflict_path} (owned by {owner_dot})")
                    conflict_hit = True
            if conflict_hit:
                continue
            kept_pairs.append((src_rel, dst_abs))
        return True, kept_pairs

    def _collect_installed_export_paths(self, dot: str, home_dir: str) -> Tuple[List[str], Dict[str, str]]:
        relative_paths: List[str] = []
        action_by_path: Dict[str, str] = {}
        for file_entry in self.manifest_manager.get_file_entries(dot):
            destination_path = os.path.abspath(file_entry.get("dst", ""))
            if destination_path.startswith(home_dir + os.sep) or destination_path == home_dir:
                relative_path = os.path.relpath(destination_path, home_dir)
                relative_paths.append(relative_path)
                if "action" in file_entry:
                    action_by_path[relative_path] = DeezUtils.normalize_action(file_entry["action"])
            else:
                LOG.debug("[EXPORT] Skipping %s: not under home directory", destination_path)
        return relative_paths, action_by_path

    def _read_bundle_manifest(self, bundle_path: Path) -> Optional[Dict[str, Any]]:
        try:
            with tarfile.open(bundle_path, "r:gz") as tar:
                manifest_member = tar.extractfile("manifest.toml")
                if manifest_member is None:
                    UI.error(f"{bundle_path}: missing manifest.toml")
                    return None
                return toml.loads(manifest_member.read().decode("utf-8"))
        except Exception as exc:
            UI.error(f"{bundle_path}: failed to inspect bundle: {exc}")
            return None

    def _prepare_bundle_install(
        self,
        bundle_path: Path,
        bundle: Dict[str, Any],
    ) -> Optional[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[str, str]]]]:
        dot = bundle.get("name")
        if not dot:
            UI.error(f"{bundle_path}: manifest.toml has no 'name' field")
            return None
        bundle_entries = bundle.get("files", [])
        file_pairs = [(entry["src"], entry["dst"]) for entry in bundle_entries]
        if not file_pairs:
            UI.error(f"{bundle_path}: manifest.toml has no [[files]]")
            return None
        ok, kept_pairs = self._check_conflicts_from_bundle(
            dot,
            bundle.get("owner", ""),
            bundle_entries,
            bundle.get("conflicts"),
        )
        if not ok or not kept_pairs:
            UI.info(f"'{dot}' skipped due to conflict.")
            return None
        remaining_pairs = list(kept_pairs)
        filtered_entries: List[Dict[str, Any]] = []
        for bundle_entry in bundle_entries:
            entry_key = (bundle_entry.get("src"), bundle_entry.get("dst"))
            if entry_key in remaining_pairs:
                filtered_entries.append(bundle_entry)
                remaining_pairs.remove(entry_key)
        return dot, bundle_entries, filtered_entries, kept_pairs

    def _do_package(self, global_owner: str, global_home: str, global_version: str, git_url: str = "", target_branch: str = "", compress: bool = True, out_dir: Optional[str] = None, overwrite_existing: bool = False, sections: Optional[List[str]] = None, dry_run: bool = False) -> List[str]:
        writer = WriteDots()
        git_handler = GitHandler(self.main_config)
        pkg_paths: List[str] = []
        selected_sections = sections if sections is not None else self.dotfile_sections
        default_source_url = self.global_config.get("source") or git_url
        for dot_section in selected_sections:
            UI.set_loader_message(f"Bundling {dot_section}...")
            dot_data = self.main_config.get(dot_section, {})
            dot_source_dir, dot_source_url, dot_target_branch = self._resolve_dot_source(dot_data, git_handler, default_source_url, target_branch)
            githash = git_handler.get_githash(dot_source_dir)
            hook_cwd = self._hook_cwd(dot_source_dir)
            section_owner = dot_data.get("owner", global_owner)
            section_version = dot_data.get("version", global_version)
            dependency = DeezUtils.normalize_dependency_blocks(dot_data.get("dependency") or dot_data.get("depends"))
            conflicts = self._normalize_conflict_names(dot_data.get("conflicts"))
            LOG.debug("[PACKAGE] Packaging dot: %s", dot_section)
            pre_command = dot_data.get("pre_command")
            post_command = dot_data.get("post_command")
            build_command = dot_data.get("build_command")
            if dry_run:
                self._announce_dry_run_pre_command(pre_command, scope_label=f"dot '{dot_section}'")
            elif self._skip_on_failed_pre_command(pre_command, scope_label=f"dot '{dot_section}'", cwd=hook_cwd):
                continue
            if build_command:
                writer.execute_commands([build_command], cwd=dot_source_dir)
            section_home = os.path.expandvars(dot_data.get("home", global_home))

            file_entries: List[Dict[str, Any]] = []
            raw_file_entries = list(self._iter_raw_file_entries(dot_data))
            if not raw_file_entries and dot_data.get("paths"):
                raw_file_entries = [
                    {
                        "source_root": dot_data.get("source_root"),
                        "target_root": dot_data.get("target_root"),
                        "paths": dot_data.get("paths"),
                        "ignored_paths": dot_data.get("ignored_paths"),
                        "action": dot_data.get("action"),
                        "clean_target": dot_data.get("clean_target", False),
                    }
                ]
            for file_entry in raw_file_entries:
                entry_pre_command = file_entry.get("pre_command")
                if dry_run:
                    self._announce_dry_run_pre_command(entry_pre_command, scope_label=self._file_entry_label(dot_section, file_entry))
                elif self._skip_on_failed_pre_command(entry_pre_command, scope_label=self._file_entry_label(dot_section, file_entry), cwd=hook_cwd):
                    continue
                entry_build_command = file_entry.get("build_command")
                if entry_build_command:
                    writer.execute_commands([entry_build_command], cwd=dot_source_dir)
                source_root, target_root, relative_paths, ignored_paths, action, clean_target, conflicted_paths = self._resolve_file_entry(
                    dot_data,
                    file_entry,
                    section_home,
                    dot_source_dir,
                )
                file_entries.append(
                    {
                        "src_root": source_root,
                        "tgt_root": target_root,
                        "rel_paths": relative_paths,
                        "ignored_paths": ignored_paths,
                        "archive_root": file_entry.get("source_root") or "",
                        "entry_metadata": {
                            "source_root": file_entry.get("source_root") or "",
                            "dependency": DeezUtils.normalize_dependency_blocks(file_entry.get("dependency") or file_entry.get("depends")) or None,
                            "conflicted_paths": [str(Path(target_root) / path) for path in conflicted_paths] or None,
                            "pre_command": entry_pre_command,
                        },
                        "action": action,
                        "clean_target": clean_target,
                    }
                )
            pkg_path = writer.stage(
                file_entries=file_entries,
                dot=dot_section,
                owner=section_owner,
                version=section_version,
                githash=githash,
                source_url=dot_source_url,
                branch=dot_target_branch,
                dependency=dependency,
                conflicts=conflicts,
                compress=compress,
                out_dir=out_dir,
                pre_command=pre_command,
                post_command=post_command,
                build_command=build_command,
                overwrite_existing=overwrite_existing,
            )
            if pkg_path:
                pkg_paths.append(pkg_path)
            else:
                UI.warn(f"Skipped bundling dot '{dot_section}': no source files were staged.")
        if not pkg_paths:
            UI.warn("No bundles were created.")
            UI.info("Bundling complete")
        return pkg_paths

    def _do_export(self, global_owner: str, global_home: str, global_version: str, sections: Optional[List[str]] = None, compress: bool = True, overwrite_existing: bool = False, dry_run: bool = False) -> None:
        writer = WriteDots()
        available_sections = self.dotfile_sections or self.manifest_manager.list_dots()
        selected_sections = available_sections if sections is None else sections
        hook_cwd = self._hook_cwd(self.source_dir)
        for dot_section in selected_sections:
            UI.set_loader_message(f"Exporting {dot_section}...")
            if dot_section in self.main_config:
                dot_data = self.main_config.get(dot_section, {})
                section_owner = dot_data.get("owner", global_owner)
                section_version = dot_data.get("version", global_version)
                section_home = os.path.expandvars(dot_data.get("home", global_home))
                if dry_run:
                    self._announce_dry_run_pre_command(dot_data.get("pre_command"), scope_label=f"dot '{dot_section}'")
                elif self._skip_on_failed_pre_command(dot_data.get("pre_command"), scope_label=f"dot '{dot_section}'", cwd=hook_cwd):
                    continue
                manifest_meta = {
                    "name": dot_section,
                    "owner": section_owner,
                    "version": section_version,
                    "source": dot_data.get("git") or self.main_config.get("global", {}).get("git"),
                    "branch": dot_data.get("branch") or dot_data.get("git_branch") or self.main_config.get("global", {}).get("branch") or self.main_config.get("global", {}).get("git_branch"),
                    "dependency": DeezUtils.normalize_dependency_blocks(dot_data.get("dependency") or dot_data.get("depends")) or None,
                    "conflicts": self._normalize_conflict_names(dot_data.get("conflicts")) or None,
                    "pre_command": dot_data.get("pre_command"),
                    "post_command": dot_data.get("post_command"),
                    "build_command": dot_data.get("build_command"),
                }
                UI.plain(f"[EXPORT] Capturing dot: {dot_section}")
                file_entries: List[Dict[str, Any]] = []
                dot_files = dot_data.get("files", [])
                if not dot_files and dot_data.get("paths"):
                    legacy_entry = {
                        "source_root": dot_data.get("source_root"),
                        "target_root": dot_data.get("target_root"),
                        "paths": dot_data.get("paths"),
                        "action": dot_data.get("action"),
                        "clean_target": dot_data.get("clean_target", False),
                    }
                    _source_root, tgt_root, rel_paths, ignored_paths, action, clean_target, conflicted_paths = self._resolve_file_entry(
                        dot_data,
                        legacy_entry,
                        section_home,
                    )
                    file_entries.append(
                        {
                            "src_root": tgt_root,
                            "tgt_root": tgt_root,
                            "rel_paths": rel_paths,
                            "ignored_paths": ignored_paths,
                            "archive_root": legacy_entry.get("source_root") or "",
                            "entry_metadata": {
                                "source_root": legacy_entry.get("source_root") or "",
                                "dependency": DeezUtils.normalize_dependency_blocks(legacy_entry.get("dependency") or legacy_entry.get("depends")) or None,
                                "conflicted_paths": [str(Path(tgt_root) / path) for path in conflicted_paths] or None,
                            },
                            "action": action,
                            "clean_target": clean_target,
                        }
                    )
                else:
                    for raw_file_entry in self._iter_raw_file_entries(dot_data):
                        entry_pre_command = raw_file_entry.get("pre_command")
                        if dry_run:
                            self._announce_dry_run_pre_command(entry_pre_command, scope_label=self._file_entry_label(dot_section, raw_file_entry))
                        elif self._skip_on_failed_pre_command(entry_pre_command, scope_label=self._file_entry_label(dot_section, raw_file_entry), cwd=hook_cwd):
                            continue
                        _source_root, tgt_root, rel_paths, ignored_paths, action, clean_target, conflicted_paths = self._resolve_file_entry(
                            dot_data,
                            raw_file_entry,
                            section_home,
                        )
                        file_entries.append(
                            {
                                "src_root": tgt_root,
                                "tgt_root": tgt_root,
                                "rel_paths": rel_paths,
                                "ignored_paths": ignored_paths,
                                "archive_root": raw_file_entry.get("source_root") or "",
                                "entry_metadata": {
                                    "source_root": raw_file_entry.get("source_root") or "",
                                    "dependency": DeezUtils.normalize_dependency_blocks(raw_file_entry.get("dependency") or raw_file_entry.get("depends")) or None,
                                    "conflicted_paths": [str(Path(tgt_root) / path) for path in conflicted_paths] or None,
                                    "pre_command": entry_pre_command,
                                },
                                "action": action,
                                "clean_target": clean_target,
                            }
                        )
                writer.export_entries(
                    file_entries=file_entries,
                    dot=dot_section,
                    owner=section_owner,
                    version=section_version,
                    manifest_meta=manifest_meta,
                    compress=compress,
                    overwrite_existing=overwrite_existing,
                )
            else:
                installed_sections = self.manifest_manager.list_dots()
                if dot_section not in installed_sections:
                    UI.error(f"Dot '{dot_section}' not found in config or installed manifest.")
                    continue
                UI.plain(f"[EXPORT] Capturing installed dot: {dot_section}")
                desc_data = self.manifest_manager.load_desc(dot_section)
                section_owner = desc_data.get("owner", global_owner)
                section_version = desc_data.get("version", global_version)
                home_dir = os.path.expanduser("~")
                relative_paths, action_by_path = self._collect_installed_export_paths(dot_section, home_dir)
                if not relative_paths:
                    UI.info(f"No home-relative files found for '{dot_section}'.")
                    continue
                writer.export(
                    rel_paths=relative_paths,
                    tgt_root=home_dir,
                    dot=dot_section,
                    owner=section_owner,
                    version=section_version,
                    action_map=action_by_path if action_by_path else None,
                    clean_target=desc_data.get("clean_target", False),
                    manifest_meta=desc_data,
                    compress=compress,
                    overwrite_existing=overwrite_existing,
                )
        UI.info("Export complete")

    def _do_install(self, tarballs: List[str], dry_run: bool = False, prechecked_dependencies: bool = False) -> None:
        writer = WriteDots()
        no_backup = getattr(self.args, "no_backup", False)
        planned_installs: List[Tuple[Path, Dict[str, Any], Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[str, str]]]]] = []
        if not dry_run and not prechecked_dependencies:
            for pkg_path in tarballs:
                bundle_path = Path(pkg_path)
                if not bundle_path.is_file():
                    UI.error(f"Not found: {pkg_path}")
                    continue
                bundle = self._read_bundle_manifest(bundle_path)
                if bundle is None:
                    continue
                prepared_bundle = self._prepare_bundle_install(bundle_path, bundle)
                if prepared_bundle is None:
                    continue
                planned_installs.append((bundle_path, bundle, prepared_bundle))
            self._resolve_bundle_dependencies(planned_installs)
        for pkg_path in tarballs:
            bundle_path = Path(pkg_path)
            UI.set_loader_message(f"Preparing {bundle_path.name}...")
            UI.set_loader_message(f"Installing {bundle_path.name}...")
            if not bundle_path.is_file():
                UI.error(f"Not found: {pkg_path}")
                continue
            if dry_run:
                bundle = self._read_bundle_manifest(bundle_path)
                if bundle is None:
                    continue
                prepared_bundle = self._prepare_bundle_install(bundle_path, bundle)
                if prepared_bundle is None:
                    continue
                dot, _bundle_entries, filtered_entries, kept_pairs = prepared_bundle
                self._announce_dry_run_pre_command(bundle.get("pre_command"), scope_label=f"dot '{dot}'")
                for file_entry in filtered_entries:
                    self._announce_dry_run_pre_command(file_entry.get("pre_command"), scope_label=self._file_entry_label(dot, file_entry))
                UI.plain(f"[DRY RUN] [INSTALL] '{dot}' would be installed ({len(kept_pairs)} files).")
                continue
            temp_install_dir = Path(tempfile.mkdtemp(prefix="deez-install-"))
            try:
                with tarfile.open(bundle_path, "r:gz") as tar:
                    tar.extractall(temp_install_dir)
                manifest_path = temp_install_dir / "manifest.toml"
                if not manifest_path.exists():
                    UI.error(f"{pkg_path}: missing manifest.toml")
                    continue
                with manifest_path.open("rb") as f:
                    bundle = toml.load(f)
                prepared_bundle = self._prepare_bundle_install(bundle_path, bundle)
                if prepared_bundle is None:
                    continue
                dot, bundle_entries, filtered_entries, _kept_pairs = prepared_bundle
                if self._skip_on_failed_pre_command(bundle.get("pre_command"), scope_label=f"dot '{dot}'"):
                    continue
                install_entries: List[Dict[str, Any]] = []
                for file_entry in filtered_entries:
                    if self._skip_on_failed_pre_command(file_entry.get("pre_command"), scope_label=self._file_entry_label(dot, file_entry)):
                        continue
                    install_entries.append(file_entry)
                filtered_entries = install_entries
                if not filtered_entries:
                    UI.info(f"'{dot}' skipped — all file entries failed pre_command.")
                    continue
                if not no_backup:
                    backup_desc = self.manifest_manager.load_desc(dot) or {k: v for k, v in bundle.items() if k != "files"}
                    writer.backup_to_tarball(dot, filtered_entries, desc_data=backup_desc)
                deployed_pairs: List[Dict[str, Any]] = []
                adopted_pairs: List[Dict[str, Any]] = []
                bundle_data_dir = temp_install_dir / "data"
                clean_target = bundle.get("clean_target", False)
                for file_entry in filtered_entries:
                    source_rel_path = file_entry.get("src")
                    destination_path = file_entry.get("dst")
                    entry_action = DeezUtils.normalize_action(file_entry.get("action"))
                    source_path = bundle_data_dir / source_rel_path
                    if not writer._path_exists_or_link(source_path):
                        LOG.debug("Missing in bundle: %s", source_rel_path)
                        continue
                    if writer._copy_with_action(source_path, destination_path, entry_action, clean_target=clean_target):
                        deployed_pairs.append({"src": source_rel_path, "dst": destination_path, "action": entry_action})
                    elif entry_action == "preserve" and Path(destination_path).exists():
                        adopted_pairs.append({"src": source_rel_path, "dst": destination_path, "action": entry_action})
                if not deployed_pairs and not adopted_pairs:
                    UI.info(f"'{dot}' skipped — no files installed.")
                    continue
                dot_post = bundle.get("post_command")
                if dot_post:
                    writer.execute_commands([dot_post])
                copied_count = len(deployed_pairs)
                adopted_count = len(adopted_pairs)
                deployed_pairs.extend(adopted_pairs)
                digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
                xdg_cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
                cache_dir = xdg_cache / "deez" / "dots"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached_pkg = cache_dir / f"{digest}.tar.gz"
                if not cached_pkg.exists():
                    shutil.copy2(bundle_path, cached_pkg)
                deployed_keys = {(e["src"], e["dst"]) for e in deployed_pairs}
                all_entries: List[Dict[str, Any]] = []
                for file_entry in bundle_entries:
                    entry_key = (file_entry.get("src"), file_entry.get("dst"))
                    manifest_entry = {k: v for k, v in file_entry.items() if k != "installed"}
                    manifest_entry["src"] = file_entry.get("src")
                    manifest_entry["dst"] = file_entry.get("dst")
                    manifest_entry["action"] = DeezUtils.normalize_action(file_entry.get("action"))
                    manifest_entry["installed"] = entry_key in deployed_keys
                    all_entries.append(manifest_entry)
                meta = {k: v for k, v in bundle.items() if k != "files"}
                meta["hash"] = digest
                meta["installdate"] = str(int(time.time()))
                meta.pop("removeddate", None)
                self.manifest_manager.save(dot, meta, all_entries)
                skipped = len(bundle_entries) - len(deployed_pairs)
                parts = []
                if copied_count:
                    parts.append(f"{copied_count} copied")
                if adopted_count:
                    parts.append(f"{adopted_count} adopted")
                if skipped:
                    parts.append(f"{skipped} skipped")
                UI.success(f"Installed '{dot}': {', '.join(parts)}")
            finally:
                shutil.rmtree(temp_install_dir, ignore_errors=True)

    def _resolve_uninstall_targets(self, dots: Optional[List[str]], installed: List[str]) -> List[str]:
        if dots:
            invalid = [dot for dot in dots if dot not in installed]
            for dot in invalid:
                UI.plain(f"[UNINSTALL] '{dot}' is not installed — skipping.")
            return [dot for dot in dots if dot in installed]
        if not installed:
            UI.info("No installed dots found.")
            return []
        labels = [self._installed_dot_selection_label(dot) for dot in installed]
        UI.plain("\nInstalled dots:")
        return InteractiveMenu.choose_many("Select dots to uninstall", installed, labels=labels)

    def _print_uninstall_dry_run(self, dot: str) -> None:
        tracked_paths = self.manifest_manager.get_files(dot)
        UI.plain(f"[DRY RUN] [UNINSTALL] Would remove dot '{dot}' with {len(tracked_paths)} tracked files.")
        for target_path in tracked_paths:
            UI.plain(f"  would remove: {target_path}")

    def _resolve_restore_targets(self, dots: Optional[List[str]], restorable: List[str]) -> List[str]:
        if dots:
            invalid = [dot for dot in dots if dot not in restorable]
            for dot in invalid:
                UI.error(f"No backups found for '{dot}' — skipping.")
            return [dot for dot in dots if dot in restorable]
        snapshot_counts = {dot: len(self._list_snapshots(dot)) for dot in restorable}
        labels = [self._restorable_dot_selection_label(dot, snapshot_counts[dot]) for dot in restorable]
        UI.plain("\nDots with backups:")
        return InteractiveMenu.choose_many("Select dots to restore", restorable, labels=labels)

    def _restore_snapshot(self, dot: str, snapshot_path: str, backup_base: Path, dry_run: bool) -> None:
        UI.plain(f"[RESTORE] Restoring '{dot}' from {os.path.relpath(snapshot_path, backup_base)}...")
        original_no_backup = getattr(self.args, "no_backup", False)
        self.args.no_backup = True
        try:
            self._do_install([snapshot_path], dry_run=dry_run)
        finally:
            self.args.no_backup = original_no_backup

    def _do_uninstall(self, dots: Optional[List[str]] = None, dry_run: bool = False) -> None:
        installed = self.manifest_manager.list_dots()
        if dots:
            selected_dots = self._resolve_uninstall_targets(dots, installed)
            if not selected_dots:
                return
        elif not installed:
            UI.info("No installed dots found.")
            return
        else:
            selected_dots = self._resolve_uninstall_targets(None, installed)
            if not selected_dots:
                UI.info("Cancelled.")
                return
        if not dry_run and not InteractiveMenu.confirm("Confirm uninstall?", default=False):
            UI.info("Cancelled.")
            return
        writer = WriteDots()
        for dot in selected_dots:
            if dry_run:
                self._print_uninstall_dry_run(dot)
                continue
            writer.remove(dot, self.manifest_manager)

    def _do_restore(self, dots: Optional[List[str]] = None, dry_run: bool = False) -> None:
        all_backup_dots = self._backup_dots()
        restorable = [s for s in all_backup_dots if self._list_snapshots(s)]
        if not restorable:
            UI.info("No backup snapshots found.")
            return
        target_dots = self._resolve_restore_targets(dots, restorable)
        if not target_dots:
            UI.info("Cancelled.")
            return
        backup_base = self._backup_user_base()
        for dot in target_dots:
            snapshots = self._list_snapshots(dot)
            labels = [os.path.relpath(snapshot, backup_base) for snapshot in snapshots]
            UI.plain(f"\nDot '{dot}':")
            snapshot_path = InteractiveMenu.choose_one("Select snapshot to restore", snapshots, labels=labels, allow_cancel=True)
            if snapshot_path is None:
                UI.info(f"Skipping '{dot}'.")
                continue
            self._restore_snapshot(dot, snapshot_path, backup_base, dry_run)

    def _do_downgrade(self, dots: Optional[List[str]] = None, dry_run: bool = False) -> None:
        cached_by_dot = self.cache_manager.bundles_by_dot()
        if not cached_by_dot:
            UI.info("No cached bundles found.")
            return
        if dots:
            invalid = [s for s in dots if s not in cached_by_dot]
            for s in invalid:
                UI.error(f"No cached bundles found for '{s}'.")
            target_dots = [s for s in dots if s in cached_by_dot]
        else:
            available = sorted(cached_by_dot.keys())
            labels = [self._cache_dot_selection_label(s, len(cached_by_dot[s])) for s in available]
            UI.plain("\nDots with cached bundles:")
            target_dots = InteractiveMenu.choose_many("Select dots to downgrade", available, labels=labels)
        if not target_dots:
            UI.info("Cancelled.")
            return
        to_install: List[str] = []
        for dot in target_dots:
            entries = cached_by_dot[dot]
            labels = [self._cache_version_selection_label(entry) for entry in entries]
            UI.plain(f"\nDot '{dot}':")
            chosen = InteractiveMenu.choose_one("Select version to install", [str(entry.path) for entry in entries], labels=labels, allow_cancel=True)
            if chosen is None:
                UI.info(f"Skipping '{dot}'.")
                continue
            to_install.append(chosen)
        if not to_install:
            UI.info("Nothing selected.")
            return
        if dry_run:
            for p in to_install:
                UI.info(f"[DRY RUN] Would install: {p}")
            return
        self._do_install(to_install)

    def _serialize_tree_node(self, node: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "exists": bool(node.get("exists", True)),
            "owners": sorted(node.get("owners", [])),
            "children": {
                name: self._serialize_tree_node(child)
                for name, child in sorted(node.get("children", {}).items())
            },
        }

    def _resolve_installed_dot_targets_for_query(self, requested: Optional[str]) -> List[str]:
        dots = sorted(self.manifest_manager.list_dots())
        if not dots:
            return []
        target = str(requested or "").strip()
        if not target or target.lower() == "all":
            return dots
        if target not in dots:
            return []
        return [target]

    def query_installed_dots(self) -> List[Dict[str, Any]]:
        """Return a structured list of installed dot manifests from the local manifest store."""
        dots = self.manifest_manager.list_dots()
        results: List[Dict[str, Any]] = []
        for dot in sorted(dots):
            desc = self.manifest_manager.load_desc(dot)
            files = list(self.manifest_manager.get_files(dot))
            missing = [p for p in files if not Path(p).exists()]
            results.append(
                {
                    "dot": dot,
                    "owner": desc.get("owner", "?"),
                    "version": desc.get("version"),
                    "install_date": desc.get("installdate"),
                    "files": files,
                    "missing": missing,
                    "file_count": len(files),
                    "status": "missing" if missing else "ok",
                }
            )
        return results

    def query_filetree(self, target: Optional[str] = None) -> Dict[str, Any]:
        """Return a serialized tracked file tree for installed dots."""
        dots = self._resolve_installed_dot_targets_for_query(target)
        tree: Dict[str, Any] = {"children": {}}
        for dot in dots:
            for entry in self.manifest_manager.get_file_entries(dot):
                if not entry.get("installed", True):
                    continue
                dst = str(entry.get("dst") or "").strip()
                if not dst:
                    continue
                self._insert_tree_path(tree, self._display_path_parts(dst, home=Path(self.target_root)), owner=dot, exists=self._path_exists_or_link(Path(dst)))
        return {
            "target": target or "all",
            "dots": dots,
            "tree": self._serialize_tree_node(tree),
        }

    def query_healthcheck(self, target: Optional[str] = None) -> Dict[str, Any]:
        """Return a health summary for installed dots, including missing and changed files."""
        dots = self._resolve_installed_dot_targets_for_query(target)
        results: Dict[str, Any] = {}
        for dot in dots:
            results[dot] = self._healthcheck_dot(dot)
        return {
            "target": target or "all",
            "dots": dots,
            "status": results,
        }

    def query_config_sections(self) -> Dict[str, Any]:
        """Return metadata about available configuration dot sections."""
        return {
            "global": dict(self.global_config),
            "sections": sorted(self.dotfile_sections),
            "explicit_selection": self._has_explicit_dot_selection,
        }

    @staticmethod
    def _serialize_cache_entry(entry: CacheEntry) -> Dict[str, Any]:
        return {
            "path": str(entry.path),
            "name": entry.name,
            "version": entry.version,
            "githash": entry.githash,
            "builddate": entry.builddate,
            "origin": entry.origin,
            "size": entry.size,
            "mtime": entry.mtime,
            "mtime_ts": entry.mtime_ts,
            "meta": entry.meta,
        }

    def query_cache_entries(self) -> List[Dict[str, Any]]:
        """Return metadata for all cached bundle entries."""
        return [self._serialize_cache_entry(entry) for entry in self.cache_manager.list_entries()]

    def query_cached_bundles_by_dot(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return cached bundle metadata grouped by dot name."""
        return {
            dot: [self._serialize_cache_entry(entry) for entry in entries]
            for dot, entries in self.cache_manager.bundles_by_dot().items()
        }

    def query_available_package_managers(self) -> Dict[str, Any]:
        """Return available package managers and their configured command templates."""
        return {
            "available": list(self.available_package_managers),
            "commands": dict(self.package_manager_instance.package_manager_commands),
        }

    def query_dependency_blocks(self, sections: Optional[List[str]] = None) -> Dict[str, Any]:
        """Return normalized dependency blocks and merged dependencies for selected sections."""
        selected_sections = sections if sections is not None else self.dotfile_sections
        subset_config: Dict[str, Any] = {"global": dict(self.main_config.get("global", {}))}
        for section in selected_sections:
            if section in self.main_config:
                subset_config[section] = self.main_config.get(section, {})
        blocks = self.package_manager_instance.collect_dependency_blocks(subset_config)
        merged = self.package_manager_instance.fetch_all_deps(subset_config)
        return {
            "selected_sections": selected_sections,
            "dependency_blocks": blocks,
            "merged_dependencies": merged,
        }

    def query_missing_dependencies(self, selected_managers: Optional[List[str]] = None) -> Dict[str, Any]:
        """Return missing dependencies validated against available package managers."""
        filtered, missing = self._collect_missing_dependencies(selected_managers)
        return {
            "available_managers": self.available_package_managers,
            "filtered": filtered,
            "missing": missing,
        }

    def _do_list(self) -> None:
        dots = self.manifest_manager.list_dots()
        if not dots:
            UI.plain("No dots found in the manifest.")
            return
        UI.plain(f"{'Dot':<20} {'Owner':<30} {'Files':<6} {'Installed'}")
        UI.plain("-" * 80)
        for dot in sorted(dots):
            desc = self.manifest_manager.load_desc(dot)
            files = self.manifest_manager.get_files(dot)
            owner = desc.get("owner", "?")
            install_ts = desc.get("installdate", "?")
            missing = [p for p in files if not Path(p).exists()]
            UI.plain(f"  {dot:<18} {owner:<30} {len(files):<6} {install_ts}")
            if missing:
                for p in missing:
                    UI.plain(f"      [MISSING] {p}")

    def run(self) -> None:
        """Run the CLI command flow based on parsed args and config state."""
        from .commands import execute_command

        execute_command(self)


def create_deez_cli(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> DeezCLI:
    """Create a configured DeezCLI instance."""
    args = argparse.Namespace()
    return DeezCLI(
        args,
        main_config or {},
        source_dir or "",
        target_root or os.path.expanduser("~"),
        version or "unknown",
        available_package_managers if available_package_managers is not None else [],
        distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    )


def query_installed_dots(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> List[Dict[str, Any]]:
    """Return installed dot metadata."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_installed_dots()


def query_filetree(
    main_config: Dict[str, Any],
    target: Optional[str] = None,
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> Dict[str, Any]:
    """Return the installed dot file tree."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_filetree(target=target)


def query_healthcheck(
    main_config: Dict[str, Any],
    target: Optional[str] = None,
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> Dict[str, Any]:
    """Return the installed dot health summary."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_healthcheck(target=target)


def query_config_sections(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> Dict[str, Any]:
    """Return dot section metadata."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_config_sections()


def query_cache_entries(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> List[Dict[str, Any]]:
    """Return cached bundle metadata."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_cache_entries()


def query_cached_bundles_by_dot(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return cached bundle lists by dot."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_cached_bundles_by_dot()


def query_available_package_managers(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> Dict[str, Any]:
    """Return available package manager info."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_available_package_managers()


def query_dependency_blocks(
    main_config: Dict[str, Any],
    sections: Optional[List[str]] = None,
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> Dict[str, Any]:
    """Return normalized dependency blocks."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_dependency_blocks(sections=sections)


def query_missing_dependencies(
    main_config: Dict[str, Any],
    selected_managers: Optional[List[str]] = None,
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> Dict[str, Any]:
    """Return missing dependencies."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_missing_dependencies(selected_managers=selected_managers)


def query_selectable_config_dots(
    main_config: Dict[str, Any],
    action_label: str = "bundle",
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> List[Dict[str, str]]:
    """Return config dot selection labels."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_selectable_config_dots(action_label=action_label)


def query_selectable_installed_dots(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> List[Dict[str, str]]:
    """Return installed dot selection labels."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_selectable_installed_dots()


def query_selectable_backup_dots(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> List[Dict[str, str]]:
    """Return backup dot selection labels."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_selectable_backup_dots()


def query_selectable_cache_dots(
    main_config: Dict[str, Any],
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> List[Dict[str, str]]:
    """Return cache dot selection labels."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_selectable_cache_dots()


def query_selectable_cache_versions(
    main_config: Dict[str, Any],
    dot: str,
    source_dir: Optional[str] = None,
    target_root: Optional[str] = None,
    version: Optional[str] = None,
    available_package_managers: Optional[List[str]] = None,
    distribution: str = "auto",
    package_manager_instance: Optional[PackageManager] = None,
    manifest_manager: Optional[ManifestManager] = None,
    cache_manager: Optional[CacheManager] = None,
) -> List[Dict[str, str]]:
    """Return cached version selection labels."""
    return create_deez_cli(
        main_config,
        source_dir=source_dir,
        target_root=target_root,
        version=version,
        available_package_managers=available_package_managers,
        distribution=distribution,
        package_manager_instance=package_manager_instance,
        manifest_manager=manifest_manager,
        cache_manager=cache_manager,
    ).query_selectable_cache_versions(dot)


