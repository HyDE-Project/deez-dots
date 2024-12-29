#!/bin/env python3
# Usage: python3 deez-dots.py
# Description: A script to deploy dotfiles in $HOME directory

import tomllib as toml
import sys
import subprocess
import os
import shutil
from typing import List, Optional, Dict, Any
import logging
import time

CFG_BACKUP_DIR = os.path.join(
    os.getenv("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "deez-dots",
    "backup",
    time.strftime("%Y%m%d%H%M%S"),
)
logging.basicConfig(level=logging.INFO)


def pacman_query(package_managers: List[str], package: str) -> bool:
    """Check if a package is installed using the package manager."""
    query_commands = {
        "pacman": "pacman -Qs",
        "yay": "yay -Qs",
        "paru": "paru -Qs",
        "dnf": "dnf list installed",
        "apt": "apt list --installed",
        "flatpak": "flatpak list --app --columns application | grep ",
    }
    for manager in package_managers:
        query_cmd = query_commands.get(manager)
        if query_cmd:
            try:
                result = subprocess.run(
                    f"{query_cmd} {package}",
                    shell=True,
                    check=True,
                    text=True,
                    capture_output=True,
                )
                if result.stdout:
                    return True
            except subprocess.CalledProcessError:
                continue
    return False


def are_deps_installed(dependency_list: Dict[str, List[str]]) -> bool:
    """Check if all dependencies are installed."""
    all_installed = True
    for manager, packages in dependency_list.items():
        for package in packages:
            if shutil.which(package):
                logging.info(f"Package command '{package}' is available.")
                continue
            if not pacman_query([manager], package):
                logging.warning(f"Package '{package}' is not installed.")
                all_installed = False
    return all_installed


def available_managers() -> List[str]:
    """Get a list of available package managers on the system."""
    package_managers = ["flatpak", "pacman", "yay", "paru", "dnf", "apt"]
    return [manager for manager in package_managers if shutil.which(manager)]


def execute_commands(commands: List[Optional[str]]) -> None:
    """Execute a list of commands."""
    for command in commands:
        if command:
            try:
                result = subprocess.run(
                    command, shell=True, check=True, text=True, capture_output=True
                )
                logging.info(result.stdout.strip())
            except subprocess.CalledProcessError as e:
                logging.error("Error executing command '%s': %s", command, e)
                sys.exit(1)


def backup_target(src: str, tgt: str, paths: List[str]) -> None:
    """Execute backup of target paths."""
    for pth in paths:
        target_path = os.path.join(tgt, pth)
        if os.path.exists(target_path):
            logging.info("Backing up target path: %s", target_path)


def preserve_target(src: str, tgt: str, paths: List[str]) -> None:
    """Preserve existing target paths."""
    logging.info("Preserving: %s", tgt)
    for pth in paths:
        target_path = os.path.join(tgt, pth)
        if os.path.exists(target_path):
            logging.info("Target path already exists: %s", target_path)
            return
        logging.info("Populating: %s", target_path)
        os.makedirs(target_path, exist_ok=True)


def overwrite_target(src: str, tgt: str, paths: List[str]) -> None:
    """Overwrite target paths."""
    logging.info("Overwriting: %s", tgt)
    for pth in paths:
        target_path = os.path.join(tgt, pth)
        if os.path.exists(target_path):
            logging.info("Target path already exists: %s", target_path)


def sync_target(src: str, tgt: str, paths: List[str]) -> None:
    """Sync target paths."""
    logging.info("Syncing: %s", tgt)
    for pth in paths:
        target_path = os.path.join(tgt, pth)
        if os.path.exists(target_path):
            logging.info("Target path already exists: %s", target_path)
            logging.info("Syncing files from source to target")


def write_file(act: str, src: str, tgt: str, paths: List[str]) -> None:
    """Write files based on the specified action."""
    backup_target(src, tgt, paths)
    if act == "preserve":
        preserve_target(src, tgt, paths)
    elif act == "overwrite":
        overwrite_target(src, tgt, paths)
    elif act == "sync":
        sync_target(src, tgt, paths)
    else:
        logging.warning(f"Skipping due to unknown action: {act}")


def read_toml(file_path: str) -> Dict[str, Any]:
    """Read and parse a TOML file."""
    with open(file_path, "rb") as file:
        data = toml.load(file)
    return data


def filter_deps(
    pac_man: List[str], dependency: Dict[str, List[str]]
) -> Dict[str, List[str]]:
    """Filter dependencies based on available package managers."""
    filtered_deps = {}
    seen_packages = set()
    for manager in pac_man:
        for dep_manager, dep_list in dependency.items():
            dep_managers = dep_manager.split(",")
            if manager in dep_managers:
                if manager not in filtered_deps:
                    filtered_deps[manager] = []
                for package in dep_list:
                    if package not in seen_packages:
                        filtered_deps[manager].append(package)
                        seen_packages.add(package)
    return {k: v for k, v in filtered_deps.items() if v}


def get_all_deps(data: Dict[str, Any]) -> Dict[str, List[str]]:
    """Get all dependencies from the provided data."""
    all_deps = data.get("dependency", {})
    dot_files = data.get("dots", [])
    for dot_file in dot_files:
        dot_dependencies = data.get(dot_file, {}).get("dependency", {})
        for manager, packages in dot_dependencies.items():
            if manager in all_deps:
                all_deps[manager].extend(packages)
            else:
                all_deps[manager] = packages
    for manager in all_deps:
        all_deps[manager] = list(set(all_deps[manager]))
    return all_deps


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logging.error("Usage: python3 deez-dots.py <path_to_toml_file>")
        sys.exit(1)

    filePath = os.path.realpath(sys.argv[1])
    rootPath = os.path.dirname(filePath)
    if not os.path.isfile(filePath):
        logging.error("The file '%s' does not exist.", filePath)
        sys.exit(1)

    logging.info("Reading file: %s", filePath)
    sysPacMan = available_managers()
    mainData = read_toml(filePath)
    mainAction = mainData.get("default_action")
    startCmd = mainData.get("start_command")
    PacMan = mainData.get("package_manager", sysPacMan)
    endCmd = mainData.get("end_command")
    dotsArr = mainData.get("dots", [])

    if not PacMan or PacMan in ["", "auto", None, [""]]:
        PacMan = sysPacMan

    Deps = filter_deps(PacMan, get_all_deps(mainData))

    logging.info("Expected package manager: %s", PacMan)
    if not are_deps_installed(Deps):
        logging.error("Missing dependencies: %s", Deps)
        sys.exit(1)

    execute_commands(startCmd)

    if not dotsArr:
        logging.error("No dots declared in the file.")
        sys.exit(1)

    if not os.path.exists(CFG_BACKUP_DIR):
        os.makedirs(CFG_BACKUP_DIR, exist_ok=True)

    for dot in dotsArr:
        logging.info("Deploying %s", dot)
        dotData = mainData.get(dot)
        defPreCmd = dotData.get("pre_command")
        if isinstance(defPreCmd, str):
            defPreCmd = [defPreCmd]

        defPostCmd = dotData.get("post_command")
        if isinstance(defPostCmd, str):
            defPostCmd = [defPostCmd]

        defAction = dotData.get("action", mainAction)
        files = dotData.get("files")

        deps = dotData.get("dependency")
        deps = filter_deps(PacMan, deps)
        if not are_deps_installed(deps):
            logging.warning("Skipping due to missing dependencies: %s", deps)
            continue

        execute_commands(defPreCmd)

        for fileActions in files:
            action = fileActions.get("action", defAction)
            source_root = fileActions.get("source_root")
            if source_root and "$" in source_root:
                source_root = os.path.expandvars(source_root)
            target_root = fileActions.get("target_root")
            if target_root and "$" in target_root:
                target_root = os.path.expandvars(target_root)
            paths = fileActions.get("paths")
            if isinstance(paths, str):
                paths = [paths]
            if paths and any("$" in path for path in paths):
                paths = [os.path.expandvars(path) for path in paths]

            if not source_root:
                logging.warning(
                    "Skipping due to missing source_root for paths: %s", paths
                )
                continue
            if not target_root:
                logging.warning(
                    "Skipping due to missing target_root for paths: %s", paths
                )
                continue

            sourcePath = os.path.join(rootPath, source_root)
            write_file(action, sourcePath, target_root, paths)

        execute_commands(defPostCmd)
        logging.info("____________________________")
