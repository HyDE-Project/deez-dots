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
import argparse


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


def check_dependencies(dependency_list: Dict[str, List[str]]) -> bool:
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
                user_input = input(
                    "Do you wish to continue and ignore this error? (y/n): "
                )
                if user_input.lower() != "y":
                    sys.exit(1)


def write_file(act: str, src: str, tgt: str, paths: List[str]) -> None:
    """Write files based on the specified action."""

    def backup_target(src: str, tgt: str, paths: List[str]) -> None:
        # logging.getLogger().setLevel(logging.CRITICAL)
        """Execute backup of target paths."""
        for pth in paths:
            target_path = os.path.join(tgt, pth)
            logging.debug("Processing path: %s", target_path)
            if os.path.exists(target_path):
                logging.info("Backing up target path: %s", target_path)
                # Construct backup_path relative to CFG_BACKUP_DIR
                relative_path = os.path.relpath(target_path, start=tgt)
                backup_path = os.path.join(CFG_BACKUP_DIR, dot_name, src, relative_path)
                logging.debug("Backup path: %s", backup_path)
                if os.path.abspath(target_path) == os.path.abspath(backup_path):
                    logging.warning(
                        "Source and backup paths are the same: %s", target_path
                    )
                    continue
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                if os.path.isdir(target_path):
                    logging.debug(
                        "Copying directory %s to %s", target_path, backup_path
                    )
                    shutil.copytree(
                        target_path, backup_path, symlinks=True, dirs_exist_ok=True
                    )
                else:
                    logging.debug("Copying file %s to %s", target_path, backup_path)
                    shutil.copy2(target_path, backup_path)
            else:
                logging.warning("Target path does not exist: %s", target_path)

    def preserve_target(src: str, tgt: str, paths: List[str]) -> None:
        """Preserve existing target paths."""
        for pth in paths:
            source_path = os.path.join(source_root_path, src, pth)
            target_path = os.path.join(tgt, pth)

            if not os.path.exists(source_path):
                logging.warning("Source path does not exist: %s", source_path)
                continue

            if os.path.exists(target_path):
                logging.info("Preserving target path :  %s", target_path)
                continue
            else:
                logging.info("Populating target path: %s", target_path)
                if os.path.isdir(source_path):
                    shutil.copytree(source_path, target_path, symlinks=True)
                else:
                    shutil.copy2(source_path, target_path)

    def overwrite_target(src: str, tgt: str, paths: List[str]) -> None:
        """Overwrite target paths."""
        for pth in paths:
            source_path = os.path.join(source_root_path, src, pth)
            target_path = os.path.join(tgt, pth)

            if not os.path.exists(source_path):
                logging.warning("Source path does not exist: %s", source_path)
                continue
            logging.info("Overwriting: %s", target_path)
            if os.path.isdir(source_path):
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                shutil.copytree(source_path, target_path, symlinks=True)
            else:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(source_path, target_path)

    def sync_target(src: str, tgt: str, paths: List[str]) -> None:
        """Sync target paths."""
        for pth in paths:
            source_path = os.path.join(source_root_path, src, pth)
            target_path = os.path.join(tgt, pth)

            if not os.path.exists(source_path):
                logging.warning("Source path does not exist: %s", source_path)
                continue

            logging.info("Syncing files from source to target: %s", target_path)
            if os.path.isdir(source_path):
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                shutil.copytree(source_path, target_path, symlinks=True)
            else:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(source_path, target_path)

    # check if the inputs are valid
    if not src or not tgt or not paths:
        logging.warning("Skipping due to missing source, target or paths")
        return

    backup_target(src, tgt, paths)
    if act == "preserve":
        preserve_target(src, tgt, paths)
    elif act == "overwrite":
        overwrite_target(src, tgt, paths)
    elif act == "sync":
        sync_target(src, tgt, paths)
    else:
        logging.warning(f"Skipping due to unknown act: {act}")


def read_toml(file_path: str) -> Dict[str, Any]:
    """Read and parse a TOML file."""
    with open(file_path, "rb") as file:
        data = toml.load(file)
    return data


def filter_deps(
    package_manager: List[str],
    dependency: Dict[str, List[str]],
    filtered_deps: Dict[str, List[str]] = None,
) -> Dict[str, List[str]]:
    """Filter dependencies based on available package managers."""
    if filtered_deps is None:
        filtered_deps = {}

    seen_packages = set()
    for dep_manager, dep_list in dependency.items():
        for manager in package_manager:
            dep_managers = dep_manager.split(",")
            if manager in dep_managers:
                if manager not in filtered_deps:
                    filtered_deps[manager] = []
                for package in dep_list:
                    if package not in seen_packages:
                        filtered_deps[manager].append(package)
                        seen_packages.add(package)
    return {k: v for k, v in filtered_deps.items() if v}


def fetch_all_deps(data: Dict[str, Any]) -> Dict[str, List[str]]:
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


def resolve_package_managers(pac_man):
    if not pac_man or pac_man in ["", "auto", None, [""], ["auto"]]:
        pac_man = available_package_managers

    if not any(manager in available_package_managers for manager in pac_man):
        logging.error("Specified package manager(s) not available: %s", pac_man)
        sys.exit(1)

    if not pac_man:
        logging.error("No package manager available.")
        sys.exit(1)
    return pac_man

    logging.info("Expected package manager: %s", pac_man)


def deploy_files(files: List[Dict[str, Any]]):
    for file_action in files:
        global action
        action = file_action.get("action", default_action)
        source_root = file_action.get("source_root")
        if source_root and "$" in source_root:
            source_root = os.path.expandvars(source_root)
        target_root = file_action.get("target_root")
        if target_root and "$" in target_root:
            target_root = os.path.expandvars(target_root)
        paths = file_action.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if paths and any("$" in path for path in paths):
            paths = [os.path.expandvars(path) for path in paths]

        if not source_root:
            logging.warning("Skipping due to missing source_root for paths: %s", paths)
            continue
        if not target_root:
            logging.warning("Skipping due to missing target_root for paths: %s", paths)
            continue
        # full_source_path = os.path.join(source_root_path, source_root)

        write_file(action, source_root, target_root, paths)


def handle_git(url: str):
    """Handle Git repository information."""

    def git_clone(url: str, target_dir: str):
        subprocess.run(["git", "clone", "--depth", "1", url, target_dir], check=True)

    def git_fetch(repo_path: str):
        subprocess.run(["git", "-C", repo_path, "fetch", "--all"], check=True)

    def git_checkout(repo_path: str, branch: str):
        subprocess.run(["git", "-C", repo_path, "checkout", branch], check=True)

    target_branch = main_config.get("git_branch", "main")
    version = main_config.get("version", None)
    clone_dir = os.path.join(
        os.getenv("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
        "deez-dots",
        "clones",
    )

    # Extract repository owner and name from URL
    repo_owner = url.split("/")[-2]
    repo_name = url.split("/")[-1].replace(".git", "")
    global source_root_path
    if version:
        source_root_path = os.path.join(
            clone_dir, f"{repo_owner.lower()}.{repo_name.lower()}.{version}"
        )
    else:
        source_root_path = os.path.join(
            clone_dir, f"{repo_owner.lower()}.{repo_name.lower()}"
        )

    # Clone the repository if it doesn't exist
    if not os.path.exists(source_root_path):
        git_clone(url, source_root_path)
    # Fetch the latest changes
    git_fetch(source_root_path)
    # Checkout the specified branch
    git_checkout(source_root_path, target_branch)

    branches = subprocess.run(
        ["git", "-C", source_root_path, "branch", "-r"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    # Get remote branches
    remote_branches = subprocess.run(
        ["git", "-C", source_root_path, "branch", "-r"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    print(f"Remote Branches: {remote_branches}")

    # Get tags
    tags = subprocess.run(
        ["git", "-C", source_root_path, "tag"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()

    print(f"Repository Owner: {repo_owner}")
    print(f"Repository Name: {repo_name}")
    print(f"Branches: {branches}")
    print(source_root_path)
    print(f"Tags: {tags}")


def main():
    parser = argparse.ArgumentParser(description="Deez Dots Deployment Script")
    parser.add_argument(
        "-c", "--config", type=str, help="Path to the dots TOML configuration file"
    )
    parser.add_argument(
        "-s", "--source", type=str, help="Path to the source root directory"
    )
    args = parser.parse_args()

    if args.config:
        config_file_path = os.path.realpath(os.path.expanduser(args.config))
    else:
        config_file_path = os.path.expanduser("~/.config/hyde/dots.toml")

    global available_package_managers
    global main_config
    global source_root_path

    source_root_path = os.path.dirname(config_file_path)
    if not os.path.isfile(config_file_path):
        logging.error("The file '%s' does not exist.", config_file_path)
        sys.exit(1)

    logging.info("Reading file: %s", config_file_path)
    available_package_managers = available_managers()
    main_config = read_toml(config_file_path)
    git_url = main_config.get("git")
    if git_url:
        handle_git(git_url)
    print(source_root_path)

    mainAction = main_config.get("default_action")
    start_cmd = main_config.get("start_command")
    end_cmd = main_config.get("end_command")
    package_manager = main_config.get("package_manager", available_package_managers)
    declared_dots = main_config.get("dots", [])

    # Dependency resolution
    package_manager = resolve_package_managers(package_manager)

    # Handle all dependencies
    # TODO make func to install all dependenciess
    all_dependencies = filter_deps(package_manager, fetch_all_deps(main_config))
    if not check_dependencies(all_dependencies):
        logging.error("Missing dependencies: %s", all_dependencies)
        sys.exit(1)

    execute_commands(start_cmd)
    logging.info("____________________________")

    # preparation
    if not declared_dots:
        logging.error("No dots declared in the file.")
        sys.exit(1)

    global CFG_BACKUP_DIR
    CFG_BACKUP_DIR = os.path.join(
        os.getenv("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
        "deez-dots",
        "backup",
        time.strftime("%Y%m%d%H%M%S"),
    )
    logging.basicConfig(level=logging.INFO)
    if not os.path.exists(CFG_BACKUP_DIR):
        os.makedirs(CFG_BACKUP_DIR, exist_ok=True)

    # evaluate dotfiles
    global dot_name
    for dot_name in declared_dots:
        logging.info("Deploying %s", dot_name)
        dot_data = main_config.get(dot_name)
        default_pre_cmd = dot_data.get("pre_command")
        if isinstance(default_pre_cmd, str):
            default_pre_cmd = [default_pre_cmd]

        default_post_cmd = dot_data.get("post_command")
        if isinstance(default_post_cmd, str):
            default_post_cmd = [default_post_cmd]

        global default_action
        default_action = dot_data.get("action", mainAction)
        files = dot_data.get("files")

        get_deps = dot_data.get("dependency")
        deps = filter_deps(package_manager, get_deps)
        if not check_dependencies(deps):
            logging.warning("Skipping due to missing dependencies: %s", deps)
            continue

        execute_commands(default_pre_cmd)
        deploy_files(files)

        execute_commands(default_post_cmd)
        logging.info("____________________________")
    execute_commands(end_cmd)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
