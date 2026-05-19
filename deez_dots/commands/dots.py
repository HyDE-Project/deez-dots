"""Parser helpers for `deez dots`.

Import `DOTS_COMMAND` to reuse the command description, argument shape, and
namespace normalization in other integrations.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import CommandModule, normalize_requested_sections
from ..core import DeezUtils, GitHandler, LOG, WriteDots
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
    parser.add_argument("--no-deps-install", action="store_true", dest="no_deps_install", help="Check dependencies but do not auto-install missing ones before install or deploy")
    parser.add_argument("--no-compress", action="store_true", dest="no_compress", help="Skip tar.gz packing; leave output as a plain directory (for inspection)")
    parser.add_argument("--force", action="store_true", dest="force", help="Remove an existing extracted build directory beside the output tarball before writing")
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
        global_home=global_config.get("home", os.path.expandvars("$HOME")),
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
    cli._require_pre_command(context.global_pre_command, scope_label="global", cwd=context.hook_cwd)


def execute(cli: Any) -> None:
    """Execute the normalized `deez dots` flow using an initialized CLI instance."""
    context = build_runtime_context(cli)
    hook_runner = WriteDots()
    if getattr(cli.args, "do_package", False):
        if context.git_url:
            LOG.debug("Source: %s/%s branch=%s", context.global_owner, context.global_name, context.target_branch)
        compress = not getattr(cli.args, "no_compress", False)
        selected_sections = cli._resolve_requested_config_dot_targets(getattr(cli.args, "package_sections", None), "bundle")
        if not selected_sections:
            return
        _prepare_global_pre_command(cli, context)
        UI.set_loader_message("Bundling selected dots...")
        if context.global_build_command:
            hook_runner.execute_commands([context.global_build_command], cwd=cli.source_dir)
        cli._do_package(
            context.global_owner,
            context.global_home,
            context.global_version,
            git_url=context.git_url,
            target_branch=context.target_branch,
            compress=compress,
            overwrite_existing=getattr(cli.args, "force", False),
            sections=selected_sections,
            dry_run=context.dry_run,
        )
        return
    if getattr(cli.args, "do_export", False):
        compress = not getattr(cli.args, "no_compress", False)
        selected_sections = cli._resolve_requested_config_dot_targets(getattr(cli.args, "export_sections", None), "export")
        if not selected_sections:
            return
        _prepare_global_pre_command(cli, context)
        UI.set_loader_message("Exporting dots...")
        cli._do_export(
            context.global_owner,
            context.global_home,
            context.global_version,
            selected_sections,
            compress=compress,
            overwrite_existing=getattr(cli.args, "force", False),
            dry_run=context.dry_run,
        )
        return
    if getattr(cli.args, "do_install", False):
        _prepare_global_pre_command(cli, context)
        UI.set_loader_message("Installing bundles...")
        cli._do_install(cli.args.install_tarballs, context.dry_run)
        return
    if getattr(cli.args, "do_deploy", False):
        if context.git_url:
            LOG.debug("Source: %s/%s branch=%s", context.global_owner, context.global_name, context.target_branch)
        selected_sections = cli._resolve_requested_config_dot_targets(getattr(cli.args, "deploy_sections", None), "deploy")
        if not selected_sections:
            return
        UI.set_loader_message("Resolving dependencies...")
        cli._resolve_config_dependencies(selected_sections)
        tmp_dir = tempfile.mkdtemp(prefix="deez-deploy-")
        try:
            _prepare_global_pre_command(cli, context)
            UI.set_loader_message("Bundling selected dots...")
            pkg_paths = cli._do_package(
                context.global_owner,
                context.global_home,
                context.global_version,
                git_url=context.git_url,
                target_branch=context.target_branch,
                out_dir=tmp_dir,
                sections=selected_sections,
                dry_run=context.dry_run,
            )
            if not pkg_paths:
                UI.error("Deploy failed: bundling produced no bundles.")
                raise SystemExit(1)
            UI.set_loader_message("Installing bundled dots...")
            cli._do_install(pkg_paths, context.dry_run, prechecked_dependencies=True)
            if context.global_post_command:
                hook_runner.execute_commands([context.global_post_command], cwd=cli.source_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        UI.success("Deploy complete")
        return
    if getattr(cli.args, "do_uninstall", False):
        UI.set_loader_message("Uninstalling dots...")
        cli._do_uninstall(getattr(cli.args, "uninstall_dots", []) or None, context.dry_run)
        return
    if getattr(cli.args, "do_filetree", False):
        UI.set_loader_message("Rendering tracked file tree...")
        cli._do_filetree(getattr(cli.args, "filetree_target", "all"))
        return
    if getattr(cli.args, "do_healthcheck", False):
        UI.set_loader_message("Checking tracked dot health...")
        cli._do_healthcheck(getattr(cli.args, "healthcheck_target", "all"))
        return
    if getattr(cli.args, "do_restore", False):
        UI.set_loader_message("Restoring dots...")
        cli._do_restore(getattr(cli.args, "restore_dots", []) or None, context.dry_run)
        return
    if getattr(cli.args, "do_downgrade", False):
        UI.set_loader_message("Downgrading dots...")
        cli._do_downgrade(getattr(cli.args, "downgrade_dots", []) or None, context.dry_run)
        return
    if getattr(cli.args, "list", False):
        UI.set_loader_message("Listing installed dots...")
        cli._do_list()


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