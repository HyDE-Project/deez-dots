"""Parser helpers for `deez cache`.

Import `CACHE_COMMAND` to reuse the command description, argument shape, and
namespace normalization in other integrations.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from .base import CommandModule
from ..ui import UI


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register cache-operation flags on the provided parser."""
    parser.add_argument("--list", action="store_true", help="List cached bundle archives")
    parser.add_argument("--prune", action="store_true", help="Prune old cached bundle archives")
    parser.add_argument("--keep", type=int, default=10, help="Number of newest cache entries to keep when pruning (default: 10)")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would be deleted without removing files")


def normalize_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    """Normalize raw argparse values into the flags consumed by DeezCLI."""
    args.cache_list = bool(getattr(args, "list", False))
    args.cache_prune = bool(getattr(args, "prune", False))
    args.cache_keep = getattr(args, "keep", 10)
    args.dry_run = getattr(args, "dry_run", False)
    if not any([args.cache_list, args.cache_prune]):
        args.cache_list = True
    if not any([args.cache_list, args.cache_prune]):
        parser.print_help()
        return False
    return True


def should_auto_discover_config(args: argparse.Namespace) -> bool:
    """Return False because cache operations can run without a config file."""
    return False


def config_error(args: argparse.Namespace, config_file_path: str | None) -> str | None:
    """Return None because cache operations do not require a config file."""
    return None


def execute(cli: Any) -> None:
    """Execute the normalized `deez cache` flow using an initialized CLI instance."""
    if getattr(cli.args, "cache_prune", False):
        UI.set_loader_message("Pruning cache...")
        code = cli._cache_prune_keep(getattr(cli.args, "cache_keep", 10), getattr(cli.args, "dry_run", False))
        if code:
            os._exit(code)
        return
    if getattr(cli.args, "cache_list", False):
        UI.set_loader_message("Listing cache...")
        cli._cache_list()


CACHE_COMMAND = CommandModule(
    name="cache",
    description="Cache operations",
    loader_message="Processing cache...",
    add_arguments=add_arguments,
    normalize_args=normalize_args,
    should_auto_discover_config=should_auto_discover_config,
    config_error=config_error,
    execute=execute,
)