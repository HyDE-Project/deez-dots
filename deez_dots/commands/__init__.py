from __future__ import annotations

import argparse
from typing import Any, Dict, Iterable, Sequence

from .backup import BACKUP_COMMAND
from .base import CommandModule, normalize_requested_sections
from .cache import CACHE_COMMAND
from .deps import DEPS_COMMAND
from .dots import DOTS_COMMAND

COMMAND_MODULES: Dict[str, CommandModule] = {
    DOTS_COMMAND.name: DOTS_COMMAND,
    DEPS_COMMAND.name: DEPS_COMMAND,
    BACKUP_COMMAND.name: BACKUP_COMMAND,
    CACHE_COMMAND.name: CACHE_COMMAND,
}


def iter_command_modules() -> Iterable[CommandModule]:
    """Yield command descriptors in CLI registration order."""
    return COMMAND_MODULES.values()


def infer_command_name(args: Any) -> str | None:
    """Infer a command name from normalized args when `args.command` is unset."""
    command_name = getattr(args, "command", None)
    if command_name:
        return str(command_name)
    if getattr(args, "deps_check", False) or getattr(args, "deps_update", False) or getattr(args, "install_deps", False):
        return "deps"
    if getattr(args, "backup_list", False) or getattr(args, "backup_prune", False):
        return "backup"
    if getattr(args, "cache_list", False) or getattr(args, "cache_prune", False):
        return "cache"
    if any(
        getattr(args, attr, False)
        for attr in (
            "do_package",
            "do_export",
            "do_install",
            "do_deploy",
            "do_uninstall",
            "do_filetree",
            "do_healthcheck",
            "do_restore",
            "do_downgrade",
            "list",
        )
    ):
        return "dots"
    return None


def resolve_command_module(name_or_args: Any) -> CommandModule:
    """Resolve a command descriptor from a command name or args namespace."""
    if hasattr(name_or_args, "__dict__"):
        command_name = infer_command_name(name_or_args)
    else:
        command_name = str(name_or_args) if name_or_args is not None else None
    if not command_name or command_name not in COMMAND_MODULES:
        raise ValueError(f"Unknown command module: {command_name!r}")
    return COMMAND_MODULES[command_name]


def execute_command(cli: Any) -> None:
    """Execute the registered runtime handler for the CLI's active command."""
    resolve_command_module(cli.args).execute(cli)


def register_subcommands(
    subparsers: argparse._SubParsersAction,
    *,
    parents: Sequence[argparse.ArgumentParser] | None = None,
) -> Dict[str, argparse.ArgumentParser]:
    """Create argparse subparsers from the importable command registry."""
    registered: Dict[str, argparse.ArgumentParser] = {}
    parser_parents = list(parents or [])
    for module in iter_command_modules():
        parser = subparsers.add_parser(module.name, parents=parser_parents, help=module.description)
        module.add_arguments(parser)
        registered[module.name] = parser
    return registered


__all__ = [
    "CommandModule",
    "COMMAND_MODULES",
    "DOTS_COMMAND",
    "DEPS_COMMAND",
    "BACKUP_COMMAND",
    "CACHE_COMMAND",
    "iter_command_modules",
    "infer_command_name",
    "normalize_requested_sections",
    "resolve_command_module",
    "register_subcommands",
    "execute_command",
]