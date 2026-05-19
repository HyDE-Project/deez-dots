from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..core import RequestedSections, _ALL_SECTIONS_REQUESTED


def normalize_requested_sections(values: object) -> RequestedSections:
    """Return explicit dot names or the special all token used by DeezCLI."""
    if values is None:
        return None
    sections = [str(value).strip() for value in values if str(value).strip()]
    if not sections:
        return None
    if any(section.lower() == "all" for section in sections):
        return _ALL_SECTIONS_REQUESTED
    return sections


@dataclass(frozen=True)
class CommandModule:
    """Importable descriptor for one top-level deez command.

    `description` is the short help string for CLI and interop layers.
    `add_arguments(parser)` adds command-specific args to an argparse parser.
    `normalize_args(args, parser)` fills the normalized namespace consumed by DeezCLI.
    `execute(cli)` runs the command against an initialized DeezCLI instance.
    """

    name: str
    description: str
    loader_message: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    normalize_args: Callable[[argparse.Namespace, argparse.ArgumentParser], bool]
    should_auto_discover_config: Callable[[argparse.Namespace], bool]
    config_error: Callable[[argparse.Namespace, Optional[str]], Optional[str]]
    execute: Callable[[Any], None]