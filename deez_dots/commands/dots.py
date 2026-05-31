"""Parser helpers for `deez dots`.

Import `DOTS_COMMAND` to reuse the command description, argument shape, and
namespace normalization in other integrations.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import CommandModule, normalize_requested_sections
from ..core import DeezUtils, GitHandler, LOG, WriteDots, compare_versions
from ..ui import UI


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register dotfile-operation flags on the provided parser."""
    parser.add_argument("--package", nargs="*", metavar="DOT", help="Pull from git source and bundle dots into tar.gz artifacts (no live changes); omit names for interactive selection or specify dot names or 'all'")
    parser.add_argument("--export", nargs="*", metavar="DOT", help="Snapshot live dots from $HOME into tar.gz bundles (reverse of --package); omit names for interactive selection or specify dot names or 'all'")
    parser.add_argument("--install", nargs="+", metavar="TARBALL", help="Install from one or more bundle tar.gz files")
    parser.add_argument("--deploy", nargs="*", metavar="DOT", help="Bundle then install in one step (equivalent to --package + --install); omit names for interactive selection or specify dot names or 'all'")
    parser.add_argument("--uninstall", nargs="*", metavar="DOT", help="Uninstall dots; omit names for interactive selector")
    parser.add_argument("--filetree", nargs="?", const="all", metavar="DOT|all", help="Show tracked installed-file trees for one installed dot or for all installed dots when omitted or set to 'all'")
    parser.add_argument("--healthcheck", nargs="?", const="all", metavar="DOT|all", help="Check one installed dot or all installed dots when omitted or set to 'all'; drift checks use the cached bundle when its archive is available")
    parser.add_argument("--restore", nargs="*", metavar="DOT", help="Restore files from a backup snapshot; omit names for interactive selector")
    parser.add_argument("--downgrade", nargs="*", metavar="DOT", help="Re-install a previously cached bundle version; omit names for interactive selector")
    parser.add_argument("--list", action="store_true", help="List all tracked dots and their state")
    parser.add_argument("--no-backup", action="store_true", dest="no_backup", help="Skip backup when deploying or uninstalling")
    parser.add_argument("--no-deps-checks", action="store_true", dest="no_deps_checks", help="Skip dependency checks before install or deploy")
    parser.add_argument("--skip-git", action="store_true", dest="skip_git", help="Skip git refresh operations when preparing source")
    parser.add_argument("--no-deps-install", action="store_true", dest="no_deps_install", help="Check dependencies but do not auto-install missing ones before install or deploy")
    parser.add_argument("--no-compress", action="store_true", dest="no_compress", help="Skip tar.gz packing; leave output as a plain directory (for inspection)")
    parser.add_argument("--rebuild", action="store_true", dest="rebuild", help="Remove cached or existing build output before bundling")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would happen without making live changes")


def normalize_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    """Normalize raw argparse values into the flags consumed by DeezCLI."""
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
        parser.print_help()
        return False
    args.do_package = action_package
    args.do_export = action_export
    args.package_sections = normalize_requested_sections(package_val)
    args.export_sections = normalize_requested_sections(export_val)
    args.do_install = action_install
    args.install_tarballs = [str(Path(t).expanduser().resolve()) for t in install_tarballs]
    args.from_stage = action_install
    args.do_deploy = action_deploy
    args.deploy_sections = normalize_requested_sections(deploy_val)
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
    return True


def should_auto_discover_config(args: argparse.Namespace) -> bool:
    """Return True when dots.toml in the current directory can satisfy the command."""
    return bool(
        getattr(args, "do_package", False)
        or getattr(args, "do_deploy", False)
        or (getattr(args, "do_export", False) and not getattr(args, "export_sections", None))
    )


def config_error(args: argparse.Namespace, config_file_path: str | None) -> str | None:
    """Return a user-facing config error when the invocation cannot proceed."""
    if getattr(args, "do_export", False) and not getattr(args, "export_sections", None) and not config_file_path:
        return "Blank --export requires a config file. Use --config or run it from a directory containing dots.toml."
    if (getattr(args, "do_package", False) or getattr(args, "do_deploy", False)) and not config_file_path:
        return "No config file provided. Use --config or place dots.toml in the current directory."
    return None


@dataclass(frozen=True)
class DotsRuntimeContext:
    """Shared runtime values used by `deez dots` execution branches."""

    global_owner: str
    global_name: str
    global_version: str | None
    global_home: str
    git_url: str | None
    target_branch: str
    global_pre_command: str | None
    global_post_command: str | None
    global_build_command: str | None
    hook_cwd: str | None
    dry_run: bool


def build_runtime_context(cli: Any) -> DotsRuntimeContext:
    """Build the shared values needed by dots command execution."""
    global_config = cli.main_config.get("global", {})
    global_owner = DeezUtils.normalize_owner(global_config.get("owner", "unknown"))
    global_name = global_config.get("name", "unknown")
    git_url = global_config.get("git")
    if (not global_owner or not global_name) and git_url:
        global_owner, global_name = GitHandler.get_git_owner_name(git_url)
        global_owner = DeezUtils.normalize_owner(global_owner)
    return DotsRuntimeContext(
        global_owner=global_owner,
        global_name=global_name,
        global_version=global_config.get("version"),
        global_home=DeezUtils.expand(global_config.get("home", "$HOME")),
        git_url=git_url,
        target_branch=global_config.get("branch") or global_config.get("git_branch", "main"),
        global_pre_command=global_config.get("pre_command"),
        global_post_command=global_config.get("post_command"),
        global_build_command=global_config.get("build_command"),
        hook_cwd=cli._hook_cwd(cli.source_dir),
        dry_run=bool(getattr(cli.args, "dry_run", False)),
    )


def _prepare_global_pre_command(cli: Any, context: DotsRuntimeContext) -> None:
    """Run or announce the global pre-command for a dots action."""
    if context.dry_run:
        cli._announce_dry_run_pre_command(context.global_pre_command, scope_label="global")
        return
    if context.global_pre_command:
        LOG.debug(f"Running global pre_command: {context.global_pre_command}")
    cli._require_pre_command(context.global_pre_command, scope_label="global", cwd=context.hook_cwd)


def _with_loader(message: str, action, *args, **kwargs):
    UI.set_loader_message(message)
    return action(*args, **kwargs)


def _debug_source(context: DotsRuntimeContext) -> None:
    if context.git_url:
        LOG.debug("Source: %s/%s branch=%s", context.global_owner, context.global_name, context.target_branch)


def _run_global_action(cli: Any, context: DotsRuntimeContext, message: str, handler, *args, require_pre_command: bool = False, **kwargs):
    if require_pre_command:
        _prepare_global_pre_command(cli, context)
    return _with_loader(message, handler, *args, **kwargs)


def _resolve_selected_sections(cli: Any, requested: object, action_label: str) -> list[str]:
    return cli._resolve_requested_config_dot_targets(requested, action_label)


def _find_pkg_for_dots(pkg_paths: list[str], sections: list[str]) -> dict[str, str]:
    dot_to_pkg: dict[str, str] = {}
    normalized_paths: dict[str, str] = {}
    for pkg_path in pkg_paths:
        basename = Path(pkg_path).name
        if basename.endswith(".tar.gz"):
            normalized_name = basename[: -len(".tar.gz")]
        elif basename.endswith(".tar"):
            normalized_name = basename[: -len(".tar")]
        else:
            normalized_name = basename
        normalized_paths[normalized_name] = pkg_path

    for dot in sections:
        if dot in normalized_paths:
            dot_to_pkg[dot] = normalized_paths[dot]
            continue
        prefix_key = f"{dot}-"
        matching = [pkg for name, pkg in normalized_paths.items() if name.startswith(prefix_key)]
        if matching:
            dot_to_pkg[dot] = matching[0]
            continue
        for name, pkg in normalized_paths.items():
            if name == dot or name.startswith(prefix_key) or f"-{dot}-" in name or name.endswith(f"-{dot}"):
                dot_to_pkg[dot] = pkg
                break
    return dot_to_pkg


def _should_overwrite_installed_dot(cli: Any, dot: str, new_owner: str | None, new_version: str | None) -> bool:
    existing_desc = cli.manifest_manager.load_desc(dot)
    if not existing_desc:
        return True
    old_owner = existing_desc.get("owner")
    old_version = existing_desc.get("version")
    same_owner = bool(old_owner and new_owner and old_owner == new_owner)
    if same_owner and old_version and new_version:
        version_cmp = compare_versions(old_version, new_version)
        if version_cmp <= 0:
            return True
    conflict = bool(
        (old_owner and new_owner and old_owner != new_owner)
        or (old_version and new_version and compare_versions(old_version, new_version) != 0)
    )
    if not conflict:
        return True
    colored_dot = UI.style(dot, UI._CYAN)
    colored_old_version = UI.style(old_version, UI._GREEN)
    colored_new_version = UI.style(new_version, UI._GREEN)
    colored_old_owner = UI.style(old_owner, UI._MAGENTA)
    colored_new_owner = UI.style(new_owner, UI._MAGENTA)
    prompt = (
        f"Dot '{colored_dot}' version {colored_old_version} owned by {colored_old_owner} is installed. "
        f"Overwrite with version {colored_new_version} owned by {colored_new_owner}? [y/N]: "
    )
    answer = UI.read_input(prompt).strip().lower()
    if answer in ("y", "yes"):
        return True
    UI.info("Cancelled.")
    return False


def _deploy_dot(cli: Any, context: DotsRuntimeContext, dot: str, pkg_path: str) -> None:
    if not _should_overwrite_installed_dot(cli, dot, *(_read_dot_manifest(cli, pkg_path) or (None, None))):
        return
    _run_global_action(
        cli,
        context,
        f"Installing new '{dot}'...",
        cli._do_install,
        [pkg_path],
        context.dry_run,
        prechecked_dependencies=True,
        uninstall_existing=True,
    )


def _read_dot_manifest(cli: Any, pkg_path: str) -> tuple[str | None, str | None] | None:
    bundle_manifest = cli._read_bundle_manifest(Path(pkg_path))
    if not bundle_manifest:
        return None
    return bundle_manifest.get("owner"), bundle_manifest.get("version")


def execute(cli: Any) -> None:
    """Execute the normalized `deez dots` flow using an initialized CLI instance."""
    context = build_runtime_context(cli)
    args = cli.args
    rebuild = bool(getattr(args, "rebuild", False))
    dry_run = bool(getattr(args, "dry_run", False))
    compress = not getattr(args, "no_compress", False)
    install_tarballs = getattr(args, "install_tarballs", []) or []

    if args.do_package:
        _debug_source(context)
        selected_sections = _resolve_selected_sections(cli, getattr(args, "package_sections", None), "bundle")
        if not selected_sections:
            return
        if context.global_build_command:
            LOG.debug(f"Running global build_command: {context.global_build_command}")
            WriteDots().execute_commands([context.global_build_command], cwd=cli.source_dir)
        _run_global_action(
            cli,
            context,
            "Bundling selected dots...",
            cli._do_package,
            context.global_owner,
            context.global_home,
            context.global_version,
            git_url=context.git_url,
            target_branch=context.target_branch,
            compress=compress,
            out_dir=None,
            overwrite_existing=rebuild,
            rebuild=rebuild,
            sections=selected_sections,
            dry_run=dry_run,
            require_pre_command=True,
        )
        return
    if args.do_export:
        selected_sections = _resolve_selected_sections(cli, getattr(args, "export_sections", None), "export")
        if not selected_sections:
            return
        _run_global_action(
            cli,
            context,
            "Exporting dots...",
            cli._do_export,
            context.global_owner,
            context.global_home,
            context.global_version,
            selected_sections,
            compress=compress,
            overwrite_existing=rebuild,
            dry_run=dry_run,
            require_pre_command=True,
        )
        return
    if args.do_install:
        _run_global_action(
            cli,
            context,
            "Installing bundles...",
            cli._do_install,
            install_tarballs,
            dry_run,
            require_pre_command=True,
        )
        return
    if args.do_deploy:
        _debug_source(context)
        selected_sections = _resolve_selected_sections(cli, getattr(args, "deploy_sections", None), "deploy")
        if not selected_sections:
            return
        UI.set_loader_message("Resolving dependencies...")
        cli._resolve_config_dependencies(selected_sections)
        hook_runner = WriteDots() if context.global_post_command else None
        cache_build_dir = Path(DeezUtils.xdg_cache_home()) / "deez" / "dots" / "build"
        cache_build_dir.mkdir(parents=True, exist_ok=True)
        pkg_paths = _run_global_action(cli, context, "Bundling selected dots...", cli._do_package,
            context.global_owner,
            context.global_home,
            context.global_version,
            git_url=context.git_url,
            target_branch=context.target_branch,
            out_dir=str(cache_build_dir),
            sections=selected_sections,
            compress=compress,
            rebuild=rebuild,
            dry_run=dry_run,
            require_pre_command=True,
        )
        if not pkg_paths:
            UI.error("Deploy failed: bundling produced no bundles.")
            raise SystemExit(1)
        dot_to_pkg = _find_pkg_for_dots(pkg_paths, selected_sections)
        missing_dots = [dot for dot in selected_sections if dot not in dot_to_pkg]
        if missing_dots:
            UI.error(f"Deploy failed: bundling failed for selected dots: {', '.join(missing_dots)}.")
            raise SystemExit(1)
        for dot in selected_sections:
            pkg_path = dot_to_pkg.get(dot)
            _deploy_dot(cli, context, dot, pkg_path)
        if hook_runner is not None:
            LOG.debug(f"Running global post_command: {context.global_post_command}")
            hook_runner.execute_commands([context.global_post_command], cwd=cli.source_dir)
        UI.success("Deploy complete")
        return
    if args.do_uninstall:
        _run_global_action(
            cli,
            context,
            "Uninstalling dots...",
            cli._do_uninstall,
            getattr(args, "uninstall_dots", []) or None,
            dry_run,
        )
        return
    if args.do_filetree:
        _run_global_action(
            cli,
            context,
            "Rendering tracked file tree...",
            cli._do_filetree,
            getattr(args, "filetree_target", "all"),
        )
        return
    if args.do_healthcheck:
        _run_global_action(
            cli,
            context,
            "Checking tracked dot health...",
            cli._do_healthcheck,
            getattr(args, "healthcheck_target", "all"),
        )
        return
    if args.do_restore:
        _run_global_action(
            cli,
            context,
            "Restoring dots...",
            cli._do_restore,
            getattr(args, "restore_dots", []) or None,
            dry_run,
        )
        return
    if args.do_downgrade:
        _run_global_action(
            cli,
            context,
            "Downgrading dots...",
            cli._do_downgrade,
            getattr(args, "downgrade_dots", []) or None,
            dry_run,
        )
        return
    if args.list:
        _run_global_action(cli, context, "Listing installed dots...", cli._do_list)


DOTS_COMMAND = CommandModule(
    name="dots",
    description="Dotfile deployment operations",
    loader_message="Processing dots...",
    add_arguments=add_arguments,
    normalize_args=normalize_args,
    should_auto_discover_config=should_auto_discover_config,
    config_error=config_error,
    execute=execute,
)