from __future__ import annotations

import argparse
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .commands import COMMAND_MODULES, register_subcommands
from .commands.base import normalize_requested_sections

from .core import (
    CLI_VERSION,
    DeezUtils,
    GitHandler,
    LOG,
    PackageManager,
    ReadMeta,
)
from .ui import UI


def _resolve_deez_cli_class():
    """Resolve the DeezCLI class from the loaded package or fall back to the default."""
    package = sys.modules.get("deez") or sys.modules.get("deez_dots")
    if package is not None and hasattr(package, "DeezCLI"):
        return getattr(package, "DeezCLI")
    from .core import DeezCLI as DefaultDeezCLI

    return DefaultDeezCLI

def _setup_logging(debug: bool = False) -> None:
    """Configure CLI logging mode."""
    level = logging.DEBUG if debug else logging.WARNING
    fmt = "%(asctime)s [%(levelname)s] %(message)s" if debug else "%(levelname)s: %(message)s"
    logging.basicConfig(stream=sys.stderr, level=level, format=fmt, force=True)
    LOG.propagate = True
    LOG.setLevel(level)
    LOG.debug("Debug logging enabled")


GLOBAL_OVERRIDE_ARGUMENTS: Tuple[Tuple[Tuple[str, ...], Dict[str, Any]], ...] = (
    (("-c", "--config"), {"dest": "config", "type": str, "help": "Path or URL to the dots TOML configuration file"}),
    (("--source",), {"dest": "source", "type": str, "help": "Override [global].source for package or deploy"}),
    (("--git",), {"dest": "git", "type": str, "help": "Override [global].git"}),
    (("--branch",), {"dest": "branch", "type": str, "help": "Override [global].branch"}),
    (("--git-branch", "--git_branch"), {"dest": "git_branch", "type": str, "help": "Override [global].git_branch"}),
    (("--home",), {"dest": "home", "type": str, "help": "Override [global].home"}),
    (("--owner",), {"dest": "owner", "type": str, "help": "Override [global].owner"}),
    (("--name",), {"dest": "name", "type": str, "help": "Override [global].name"}),
    (("--description",), {"dest": "description", "type": str, "help": "Override [global].description"}),
    (("--action",), {"dest": "action", "type": str, "help": "Override [global].action"}),
    (("--distribution",), {"dest": "distribution", "type": str, "help": "Override [global].distribution"}),
    (("--pre-command", "--pre_command"), {"dest": "pre_command", "type": str, "help": "Override [global].pre_command"}),
    (("--post-command", "--post_command"), {"dest": "post_command", "type": str, "help": "Override [global].post_command"}),
    (("--build-command", "--build_command"), {"dest": "build_command", "type": str, "help": "Override [global].build_command"}),
    (("--dots",), {"dest": "global_dots", "action": "append", "metavar": "DOT[,DOT...]", "help": "Override [global].dots; repeat or comma-separate values"}),
    (("--config-version",), {"dest": "global_version", "type": str, "help": "Override [global].version"}),
)

GLOBAL_OVERRIDE_DEST_TO_KEY: Dict[str, str] = {
    "source": "source",
    "git": "git",
    "branch": "branch",
    "git_branch": "git_branch",
    "home": "home",
    "owner": "owner",
    "name": "name",
    "description": "description",
    "action": "action",
    "distribution": "distribution",
    "pre_command": "pre_command",
    "post_command": "post_command",
    "build_command": "build_command",
    "global_version": "version",
}


def _add_global_override_arguments(parser: argparse.ArgumentParser) -> None:
    """Add global CLI override arguments to a parser."""
    for flags, kwargs in GLOBAL_OVERRIDE_ARGUMENTS:
        option_kwargs = dict(kwargs)
        option_kwargs.setdefault("default", argparse.SUPPRESS)
        parser.add_argument(*flags, **option_kwargs)


def _parse_dot_override_values(values: Any) -> List[str]:
    """Normalize dot override input values into a list of unique dot names."""
    if values is None:
        return []
    raw_values = values if isinstance(values, list) else [values]
    normalized: List[str] = []
    seen: set = set()
    for raw_value in raw_values:
        for part in str(raw_value).split(","):
            candidate = part.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)
    return normalized


def _normalize_requested_sections(values: Any):
    """Normalize requested dot section values into explicit names or the all token."""
    return normalize_requested_sections(values)


def _apply_global_cli_overrides(main_config: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """Apply CLI global option overrides onto the parsed main configuration."""
    if not isinstance(main_config, dict):
        main_config = {}
    global_config = main_config.get("global")
    if not isinstance(global_config, dict):
        global_config = {}

    for dest, key in GLOBAL_OVERRIDE_DEST_TO_KEY.items():
        value = getattr(args, dest, None)
        if value is not None:
            global_config[key] = value

    dot_override = _parse_dot_override_values(getattr(args, "global_dots", None))
    if dot_override:
        global_config["dots"] = dot_override

    main_config["global"] = global_config
    return main_config


def _initialize_command_state(args: argparse.Namespace) -> None:
    """Populate the normalized runtime flags expected by DeezCLI."""
    args.install_deps = False
    args.deps_check = False
    args.deps_update = False
    args.deps_managers = []
    args.do_package = False
    args.do_export = False
    args.do_install = False
    args.install_tarballs = []
    args.do_deploy = False
    args.do_uninstall = False
    args.uninstall_dots = []
    args.do_filetree = False
    args.filetree_target = None
    args.do_healthcheck = False
    args.healthcheck_target = None
    args.do_restore = False
    args.restore_dots = []
    args.do_downgrade = False
    args.downgrade_dots = []
    args.from_stage = False
    args.backup_list = False
    args.backup_prune = False
    args.cache_list = False
    args.cache_prune = False
    args.cache_keep = 10
    args.sections = None
    args.no_backup = getattr(args, "no_backup", False)
    args.no_deps_checks = getattr(args, "no_deps_checks", False)
    args.skip_git = getattr(args, "skip_git", False)
    args.no_deps_install = getattr(args, "no_deps_install", False)
    args.no_compress = getattr(args, "no_compress", False)
    args.rebuild = getattr(args, "rebuild", False)
    args.dry_run = getattr(args, "dry_run", False)


def main() -> None:
    """Parse CLI arguments and execute deez-dots commands."""

    # Main parser with custom formatter
    parser = argparse.ArgumentParser(
        prog="deez",
        description="Deez dots manager (deez-dots)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Global options group
    global_group = parser.add_argument_group("Global options")
    _add_global_override_arguments(global_group)
    global_group.add_argument("--debug", action="store_true", help="Enable debug logging for troubleshooting (default: off)")
    global_group.add_argument("--version", action="version", version=f"deez {CLI_VERSION}")

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", title="Commands", metavar="{dots,deps,backup,cache}")
    command_parsers = {}
    for cmd_name, command_module in COMMAND_MODULES.items():
        cmd_parser = subparsers.add_parser(
            cmd_name,
            help=command_module.description,
            description=command_module.description,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )
        # Command options group
        cmd_group = cmd_parser.add_argument_group("Command options")
        command_module.add_arguments(cmd_group)
        # Also add global options to each subcommand for visibility
        _add_global_override_arguments(cmd_parser)
        cmd_parser.add_argument("--version", action="version", version=f"deez {CLI_VERSION}")
        command_parsers[cmd_name] = cmd_parser

    argv = sys.argv[1:]
    if argv and argv[-1] == "--":
        argv = argv[:-1]
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return

    _setup_logging(debug=bool(getattr(args, "debug", False)))
    _initialize_command_state(args)

    cmd = args.command
    command_module = COMMAND_MODULES[cmd]
    if not command_module.normalize_args(args, command_parsers[cmd]):
        return

    config_reader = ReadMeta()
    config_value = getattr(args, "config", None)
    auto_discovered_config = False
    if config_value:
        config_file_path = str(config_value).strip()
        if not config_reader.is_url(config_file_path):
            config_file_path = str(Path(config_file_path).expanduser().resolve())
    else:
        config_file_path = None
        default_config_path = Path.cwd() / "dots.toml"
        should_auto_discover_config = command_module.should_auto_discover_config(args)
        if should_auto_discover_config and default_config_path.is_file():
            config_file_path = str(default_config_path.resolve())
            auto_discovered_config = True
            LOG.debug("Auto-discovered config from current directory: %s", config_file_path)

    config_error = command_module.config_error(args, config_file_path)
    if config_error:
        UI.error(config_error)
        raise SystemExit(1)

    debug = bool(getattr(args, "debug", False))
    loader_started = False
    if UI.can_use_loader(debug=debug):
        loader_started = UI.start_loader("Loading configuration...")

    try:
        if config_file_path:
            if auto_discovered_config:
                UI.info(f"Using auto-discovered config from current directory: {config_file_path}")
            try:
                main_config = config_reader.read_location(config_file_path)
            except Exception as exc:
                UI.error(f"Failed to load config '{config_file_path}': {exc}")
                raise SystemExit(1)
        else:
            main_config = {"global": {}}
        if loader_started:
            UI.set_loader_message(command_module.loader_message)
        main_config = _apply_global_cli_overrides(main_config, args)
        global_config = main_config.get("global", {})
        home = DeezUtils.expand(global_config.get("home", "$HOME"))
        home = os.path.expanduser(home)
        os.environ["HOME"] = str(home)
        os.environ.setdefault("XDG_CONFIG_HOME", str(Path(home) / ".config"))
        os.environ.setdefault("XDG_DATA_HOME", str(Path(home) / ".local" / "share"))
        os.environ.setdefault("XDG_CACHE_HOME", str(Path(home) / ".cache"))
        distribution = global_config.get("distribution", "auto")
        git_url = global_config.get("git")
        owner = global_config.get("owner")
        name = global_config.get("name")
        target_branch = global_config.get("branch") or global_config.get("git_branch", "main")
        source_override = getattr(args, "source", None)
        source_dir = source_override or global_config.get("source")
        explicit_source_path = bool(source_dir)
        has_dot_level_source = any(isinstance(dot_data, dict) and dot_data.get("source") for name, dot_data in main_config.items() if name != "global")
        if not source_dir:
            xdg_cache = os.getenv("XDG_CACHE_HOME", DeezUtils.xdg_cache_home())
            if not owner or not name:
                if git_url:
                    owner, name = GitHandler.get_git_owner_name(git_url)
                else:
                    owner, name = "unknown", "unknown"
            source_dir = GitHandler.source_cache_path(xdg_cache, owner, name, target_branch)
        source_dir = DeezUtils.expand(source_dir)
        skip_global_source_prepare = bool(has_dot_level_source and not source_override and not global_config.get("source") and not git_url)
        need_source = bool(((args.do_package or args.do_deploy) and not skip_global_source_prepare) or (args.do_install and not args.from_stage))

        target_root = home
        try:
            git_handler = GitHandler(global_config, skip_git=args.skip_git)
        except TypeError:
            git_handler = GitHandler(global_config)
            setattr(git_handler, "skip_git", args.skip_git)
        if need_source:
            UI.set_loader_message("Preparing source directory...")
            try:
                prepare_source = git_handler.prepare_source
                if "skip_git" in inspect.signature(prepare_source).parameters:
                    source_dir = prepare_source(
                        source_dir,
                        git_url,
                        target_branch,
                        explicit_source_path=explicit_source_path,
                        skip_git=args.skip_git,
                    )
                else:
                    source_dir = prepare_source(
                        source_dir,
                        git_url,
                        target_branch,
                        explicit_source_path=explicit_source_path,
                    )
            except RuntimeError as exc:
                UI.error(str(exc))
                raise SystemExit(1)
        custom_package_manager_commands = PackageManager.load_pm(global_config)
        package_manager_instance = PackageManager(custom_commands=custom_package_manager_commands)
        available_package_managers = package_manager_instance.available
        version = global_config.get("version")
        if not version:
            if source_dir and Path(source_dir).exists():
                version = GitHandler.get_git_version(source_dir)
            else:
                version = "unknown"
        if args.do_install and not main_config.get("global", {}) and config_file_path is None:
            build_dir = Path.cwd() / "build"
            if build_dir.is_dir():
                for fname in os.listdir(build_dir):
                    if fname.endswith(".tar.gz"):
                        dot = fname[: -len(".tar.gz")].rsplit("-", 1)[0]
                        main_config.setdefault(dot, {})
        DeezCLIClass = _resolve_deez_cli_class()
        cli = DeezCLIClass(
            args,
            main_config,
            source_dir,
            target_root,
            version,
            available_package_managers,
            distribution,
            package_manager_instance=package_manager_instance,
        )
        cli.run()
    finally:
        if loader_started:
            UI.stop_loader()


def run_entrypoint() -> None:
    """Run the CLI entrypoint and handle user cancellation cleanly."""
    try:
        main()
    except KeyboardInterrupt:
        UI.info("Cancelled.")
        raise SystemExit(1)


