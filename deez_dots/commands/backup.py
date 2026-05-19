"""Parser helpers for `deez backup`.

Import `BACKUP_COMMAND` to reuse the command description, argument shape, and
namespace normalization in other integrations.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from .base import CommandModule
from ..ui import UI


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register backup-operation flags on the provided parser."""
    parser.add_argument("--list", action="store_true", help="List backup snapshots")
    parser.add_argument("--prune", nargs="*", metavar="DOT", help="Prune old backups; optionally limit pruning to one or more dots")
    parser.add_argument("--keep", type=int, default=5, help="Number of newest snapshots to keep when pruning (default: 5)")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would be deleted without removing files")


def normalize_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    """Normalize raw argparse values into the flags consumed by DeezCLI."""
    args.backup_list = bool(getattr(args, "list", False))
    args.backup_prune = getattr(args, "prune", None) is not None
    args.keep = getattr(args, "keep", None)
    args.dry_run = getattr(args, "dry_run", False)
    prune_sections = getattr(args, "prune", None)
    if prune_sections is None:
        args.sections = None
    elif not prune_sections or any(str(section).strip().lower() == "all" for section in prune_sections):
        args.sections = None
    else:
        args.sections = [str(section).strip() for section in prune_sections if str(section).strip()]
    if not any([args.backup_list, args.backup_prune]):
        parser.print_help()
        return False
    return True


def should_auto_discover_config(args: argparse.Namespace) -> bool:
    """Return False because backup operations can run without a config file."""
    return False


def config_error(args: argparse.Namespace, config_file_path: str | None) -> str | None:
    """Return None because backup operations do not require a config file."""
    return None


def execute(cli: Any) -> None:
    """Execute the normalized `deez backup` flow using an initialized CLI instance."""
    if getattr(cli.args, "backup_list", False):
        UI.set_loader_message("Listing backups...")
        cli._backup_list()
        return
    if getattr(cli.args, "backup_prune", False):
        UI.set_loader_message("Pruning backups...")
        code = cli._backup_prune_keep(cli.args.keep, getattr(cli.args, "sections", None), getattr(cli.args, "dry_run", False))
        if code:
            os._exit(code)


BACKUP_COMMAND = CommandModule(
    name="backup",
    description="Backup operations",
    loader_message="Processing backups...",
    add_arguments=add_arguments,
    normalize_args=normalize_args,
    should_auto_discover_config=should_auto_discover_config,
    config_error=config_error,
    execute=execute,
)