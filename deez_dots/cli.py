from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from .core import (
    CLI_VERSION,
    GitHandler,
    LOG,
    PackageManager,
    ReadMeta,
    _ALL_SECTIONS_REQUESTED,
    RequestedSections,
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
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    if debug:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler.setFormatter(formatter)
        root.setLevel(logging.DEBUG)
    else:
        formatter = logging.Formatter("%(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        root.setLevel(logging.WARNING)
    root.addHandler(handler)
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


def _normalize_requested_sections(values: Any) -> RequestedSections:
    """Normalize requested dot section values into explicit section names or the special all token."""
    if values is None:
        return None
    sections = [str(value).strip() for value in values if str(value).strip()]
    if not sections:
        return None
    if any(section.lower() == "all" for section in sections):
        return _ALL_SECTIONS_REQUESTED
    return sections


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


def main() -> None:
    """Parse CLI arguments and execute deez-dots commands."""
    global_override_parser = argparse.ArgumentParser(add_help=False)
    _add_global_override_arguments(global_override_parser)

    parser = argparse.ArgumentParser(prog="deez", description="Deez dots manager (deez-dots)", parents=[global_override_parser])
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for troubleshooting (default: off)")
    parser.add_argument("--version", action="version", version=f"deez {CLI_VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    dots_parser = subparsers.add_parser("dots", parents=[global_override_parser], help="Dotfile deployment operations")
    dots_parser.add_argument("--package", nargs="*", metavar="DOT", help="Pull from git source and bundle dots into tar.gz artifacts (no live changes); omit names for interactive selection or specify dot names or 'all'")
    dots_parser.add_argument("--export", nargs="*", metavar="DOT", help="Snapshot live dots from $HOME into tar.gz bundles (reverse of --package); omit names for interactive selection or specify dot names or 'all'")
    dots_parser.add_argument("--install", nargs="+", metavar="TARBALL", help="Install from one or more bundle tar.gz files")
    dots_parser.add_argument("--deploy", nargs="*", metavar="DOT", help="Bundle then install in one step (equivalent to --package + --install); omit names for interactive selection or specify dot names or 'all'")
    dots_parser.add_argument("--uninstall", nargs="*", metavar="DOT", help="Uninstall dots; omit names for interactive selector")
    dots_parser.add_argument("--filetree", nargs="?", const="all", metavar="DOT|all", help="Show tracked installed-file trees for one installed dot or for all installed dots when omitted or set to 'all'")
    dots_parser.add_argument("--healthcheck", nargs="?", const="all", metavar="DOT|all", help="Check one installed dot or all installed dots when omitted or set to 'all'; drift checks use the cached bundle when its archive is available")
    dots_parser.add_argument("--restore", nargs="*", metavar="DOT", help="Restore files from a backup snapshot; omit names for interactive selector")
    dots_parser.add_argument("--downgrade", nargs="*", metavar="DOT", help="Re-install a previously cached bundle version; omit names for interactive selector")
    dots_parser.add_argument("--list", action="store_true", help="List all tracked dots and their state")
    dots_parser.add_argument("--no-backup", action="store_true", dest="no_backup", help="Skip backup when deploying or uninstalling")
    dots_parser.add_argument("--no-deps-checks", action="store_true", dest="no_deps_checks", help="Skip dependency checks before install or deploy")
    dots_parser.add_argument("--no-deps-install", action="store_true", dest="no_deps_install", help="Check dependencies but do not auto-install missing ones before install or deploy")
    dots_parser.add_argument("--no-compress", action="store_true", dest="no_compress", help="Skip tar.gz packing; leave output as a plain directory (for inspection)")
    dots_parser.add_argument("--force", action="store_true", dest="force", help="Remove an existing extracted build directory beside the output tarball before writing")
    dots_parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would happen without making live changes")

    deps_parser = subparsers.add_parser("deps", parents=[global_override_parser], help="Dependency operations")
    deps_parser.add_argument("--check", action="store_true", help="Check dependency status")
    deps_parser.add_argument("--install", action="store_true", help="Install missing dependencies")
    deps_parser.add_argument("--update", action="store_true", help="Update packages via configured package managers")
    deps_parser.add_argument("--manager", action="append", help="Limit to specific manager(s), e.g. --manager yay")

    backup_parser = subparsers.add_parser("backup", parents=[global_override_parser], help="Backup operations")
    backup_parser.add_argument("--list", action="store_true", help="List backup snapshots")
    backup_parser.add_argument("--prune", nargs="*", metavar="DOT", help="Prune old backups; optionally limit pruning to one or more dots")
    backup_parser.add_argument("--keep", type=int, default=5, help="Number of newest snapshots to keep when pruning (default: 5)")
    backup_parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would be deleted without removing files")

    cache_parser = subparsers.add_parser("cache", parents=[global_override_parser], help="Cache operations")
    cache_parser.add_argument("--list", action="store_true", help="List cached bundle archives")
    cache_parser.add_argument("--prune", action="store_true", help="Prune old cached bundle archives")
    cache_parser.add_argument("--keep", type=int, default=10, help="Number of newest cache entries to keep when pruning (default: 10)")
    cache_parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would be deleted without removing files")

    argv = sys.argv[1:]
    if argv and argv[-1] == "--":
        argv = argv[:-1]
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return

    _setup_logging(debug=bool(getattr(args, "debug", False)))

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
    args.no_deps_install = getattr(args, "no_deps_install", False)
    args.no_compress = getattr(args, "no_compress", False)
    args.force = getattr(args, "force", False)
    args.dry_run = getattr(args, "dry_run", False)

    cmd = args.command
    if cmd == "dots":
        package_val = getattr(args, "package", None)
        action_package = package_val is not None
        export_val = getattr(args, "export", None)
        action_export = export_val is not None
        install_tarballs = getattr(args, "install", None) or []
        action_install = bool(install_tarballs)
        deploy_val = getattr(args, "deploy", None)
        action_deploy = deploy_val is not None
        uninstall_val = getattr(args, "uninstall", None)
        action_uninstall = uninstall_val is not None
        filetree_val = getattr(args, "filetree", None)
        action_filetree = filetree_val is not None
        healthcheck_val = getattr(args, "healthcheck", None)
        action_healthcheck = healthcheck_val is not None
        restore_val = getattr(args, "restore", None)
        action_restore = restore_val is not None
        downgrade_val = getattr(args, "downgrade", None)
        action_downgrade = downgrade_val is not None
        action_list = bool(getattr(args, "list", False))
        if not any([action_package, action_export, action_install, action_deploy, action_uninstall, action_filetree, action_healthcheck, action_restore, action_downgrade, action_list]):
            dots_parser.print_help()
            return
        args.do_package = action_package
        args.do_export = action_export
        args.package_sections = _normalize_requested_sections(package_val)
        args.export_sections = _normalize_requested_sections(export_val)
        args.do_install = action_install
        args.install_tarballs = [str(Path(t).expanduser().resolve()) for t in install_tarballs]
        args.from_stage = action_install
        args.do_deploy = action_deploy
        args.deploy_sections = _normalize_requested_sections(deploy_val)
        args.do_uninstall = action_uninstall
        args.uninstall_dots = list(uninstall_val) if uninstall_val else []
        args.do_filetree = action_filetree
        args.filetree_target = filetree_val or "all"
        args.do_healthcheck = action_healthcheck
        args.healthcheck_target = healthcheck_val or "all"
        args.do_restore = action_restore
        args.restore_dots = list(restore_val) if restore_val else []
        args.do_downgrade = action_downgrade
        args.downgrade_dots = list(downgrade_val) if downgrade_val else []
        args.list = action_list
    elif cmd == "deps":
        action_install = bool(getattr(args, "install", False))
        action_check = bool(getattr(args, "check", False))
        action_update = bool(getattr(args, "update", False))
        if not any([action_install, action_check, action_update]):
            deps_parser.print_help()
            return
        args.install_deps = action_install
        args.deps_check = action_check
        args.deps_update = action_update
        args.deps_managers = getattr(args, "manager", []) or []
    elif cmd == "backup":
        args.backup_list = bool(getattr(args, "list", False))
        args.backup_prune = getattr(args, "prune", None) is not None
        args.keep = getattr(args, "keep", None)
        args.dry_run = getattr(args, "dry_run", False)
        prune_sections = getattr(args, "prune", None)
        if prune_sections is None:
            args.sections = None
        elif not prune_sections or any(str(s).strip().lower() == "all" for s in prune_sections):
            args.sections = None
        else:
            args.sections = [str(s).strip() for s in prune_sections if str(s).strip()]
        if not any([args.backup_list, args.backup_prune]):
            backup_parser.print_help()
            return
    elif cmd == "cache":
        args.cache_list = bool(getattr(args, "list", False))
        args.cache_prune = bool(getattr(args, "prune", False))
        args.cache_keep = getattr(args, "keep", 10)
        args.dry_run = getattr(args, "dry_run", False)
        if not any([args.cache_list, args.cache_prune]):
            args.cache_list = True
        if not any([args.cache_list, args.cache_prune]):
            cache_parser.print_help()
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
        should_auto_discover_config = bool(
            (cmd == "dots" and (getattr(args, "do_package", False) or getattr(args, "do_deploy", False)))
            or (cmd == "dots" and getattr(args, "do_export", False) and not getattr(args, "export_sections", None))
            or (cmd == "deps" and (getattr(args, "install_deps", False) or getattr(args, "deps_check", False) or getattr(args, "deps_update", False)))
        )
        if should_auto_discover_config and default_config_path.is_file():
            config_file_path = str(default_config_path.resolve())
            auto_discovered_config = True
            LOG.debug("Auto-discovered config from current directory: %s", config_file_path)

    config_error = None
    if cmd == "dots" and getattr(args, "do_export", False) and not getattr(args, "export_sections", None) and not config_file_path:
        config_error = "Blank --export requires a config file. Use --config or run it from a directory containing dots.toml."
    elif (cmd == "dots" and (getattr(args, "do_package", False) or getattr(args, "do_deploy", False))) and not config_file_path:
        config_error = "No config file provided. Use --config or place dots.toml in the current directory."
    elif (cmd == "deps" and (getattr(args, "install_deps", False) or getattr(args, "deps_check", False) or getattr(args, "deps_update", False))) and not config_file_path:
        config_error = "No config file provided. Use --config or place dots.toml in the current directory."
    if config_error:
        UI.error(config_error)
        raise SystemExit(1)

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
    main_config = _apply_global_cli_overrides(main_config, args)
    global_config = main_config.get("global", {})
    home = os.path.expandvars(global_config.get("home", "$HOME"))
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
        xdg_cache = os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        if not owner or not name:
            if git_url:
                owner, name = GitHandler.get_git_owner_name(git_url)
            else:
                owner, name = "unknown", "unknown"
        source_dir = GitHandler.source_cache_path(xdg_cache, owner, name, target_branch)
    source_dir = os.path.expandvars(os.path.expanduser(source_dir))
    skip_global_source_prepare = bool(has_dot_level_source and not source_override and not global_config.get("source") and not git_url)
    need_source = bool(((args.do_package or args.do_deploy) and not skip_global_source_prepare) or (args.do_install and not args.from_stage))

    loader_started = False
    if UI.can_use_loader(debug=bool(getattr(args, "debug", False))):
        initial_message = {
            "dots": "Processing dots...",
            "deps": "Processing dependencies...",
            "backup": "Processing backups...",
            "cache": "Processing cache...",
        }.get(cmd, "Working...")
        loader_started = UI.start_loader(initial_message)

    target_root = home
    try:
        git_handler = GitHandler(global_config)
        if need_source:
            UI.set_loader_message("Preparing source directory...")
            try:
                source_dir = git_handler.prepare_source(
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


