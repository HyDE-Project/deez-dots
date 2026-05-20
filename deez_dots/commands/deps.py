"""Parser helpers for `deez deps`.

Import `DEPS_COMMAND` to reuse the command description, argument shape, and
namespace normalization in other integrations.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from .base import CommandModule
from ..ui import UI


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register dependency-operation flags on the provided parser."""
    parser.add_argument("--check", action="store_true", help="Check dependency status")
    parser.add_argument("--install", action="store_true", help="Install missing dependencies")
    parser.add_argument("--update", action="store_true", help="Update packages via configured package managers")
    parser.add_argument("--manager", action="append", help="Limit to specific manager(s), e.g. --manager yay")


def normalize_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    """Normalize raw argparse values into the flags consumed by DeezCLI."""
    action_install = bool(getattr(args, "install", False))
    action_check = bool(getattr(args, "check", False))
    action_update = bool(getattr(args, "update", False))
    if not any([action_install, action_check, action_update]):
        parser.print_help()
        return False
    args.install_deps = action_install
    args.deps_check = action_check
    args.deps_update = action_update
    args.deps_managers = getattr(args, "manager", []) or []
    return True


def should_auto_discover_config(args: argparse.Namespace) -> bool:
    """Return True when dots.toml in the current directory can satisfy the command."""
    return bool(getattr(args, "install_deps", False) or getattr(args, "deps_check", False) or getattr(args, "deps_update", False))


def config_error(args: argparse.Namespace, config_file_path: str | None) -> str | None:
    """Return a user-facing config error when the invocation cannot proceed."""
    if (getattr(args, "install_deps", False) or getattr(args, "deps_check", False) or getattr(args, "deps_update", False)) and not config_file_path:
        return "No config file provided. Use --config or place dots.toml in the current directory."
    return None


def execute(cli: Any) -> None:
    """Execute the normalized `deez deps` flow using an initialized CLI instance."""
    UI.start_loader("Resolving dependency managers...")
    selected_managers = cli._resolve_dep_managers()
    if selected_managers is None:
        os._exit(1)
    if getattr(cli.args, "deps_check", False):
        UI.set_loader_message("Checking dependencies...")
        cli._deps_check(selected_managers)
        return
    if getattr(cli.args, "deps_update", False):
        UI.set_loader_message("Updating dependencies...")
        code = cli._deps_update(selected_managers)
        if code:
            os._exit(code)
        return
    if getattr(cli.args, "install_deps", False):
        UI.set_loader_message("Installing missing dependencies...")
        _, missing = cli._collect_missing_dependencies(selected_managers)
        if not missing:
            UI.success("Deps: nothing to install.")
            return
        cli.package_manager_instance.install_packages(missing)


DEPS_COMMAND = CommandModule(
    name="deps",
    description="Dependency operations",
    loader_message="Processing dependencies...",
    add_arguments=add_arguments,
    normalize_args=normalize_args,
    should_auto_discover_config=should_auto_discover_config,
    config_error=config_error,
    execute=execute,
)