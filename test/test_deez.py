import argparse
import importlib.util
import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import subprocess
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEEZ_MODULE_PATH = SCRIPT_DIR / "deez"
deez_loader = SourceFileLoader("deez_module", str(DEEZ_MODULE_PATH))
deez_spec = importlib.util.spec_from_loader(deez_loader.name, deez_loader)
if deez_spec is None:
    raise ImportError(f"Unable to load deez module from {DEEZ_MODULE_PATH}")
deez_module = importlib.util.module_from_spec(deez_spec)
sys.modules[deez_spec.name] = deez_module
deez_loader.exec_module(deez_module)
DEEZ_SCRIPT = SCRIPT_DIR / "deez"
EXAMPLE_CONFIG = SCRIPT_DIR / "example" / "temp.toml"


def run_deez(args, env=None, input_data=None, cwd=None):
    env_vars = os.environ.copy()
    if env:
        env_vars.update(env)
    result = subprocess.run(
        [str(DEEZ_SCRIPT)] + args,
        cwd=str(cwd or SCRIPT_DIR),
        env=env_vars,
        input=input_data,
        capture_output=True,
        text=True,
    )
    return result


class TestDeezCLI(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="deez_test_")
        self.home_dir = Path(self.tmpdir.name) / "home"
        self.xdg_data = Path(self.tmpdir.name) / "data"
        self.xdg_cache = Path(self.tmpdir.name) / "cache"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.xdg_data.mkdir(parents=True, exist_ok=True)
        self.xdg_cache.mkdir(parents=True, exist_ok=True)
        self.env = {
            "HOME": str(self.home_dir),
            "XDG_DATA_HOME": str(self.xdg_data),
            "XDG_CACHE_HOME": str(self.xdg_cache),
            "PWD": str(SCRIPT_DIR),
        }

    def tearDown(self):
        self.tmpdir.cleanup()

    def run_cli(self, args, input_data=None, env=None):
        env_vars = dict(self.env)
        if env:
            env_vars.update(env)
        return run_deez(args, env=env_vars, input_data=input_data)

    def run_cli_in_cwd(self, args, cwd, input_data=None, env=None):
        env_vars = dict(self.env)
        if env:
            env_vars.update(env)
        env_vars["PWD"] = str(cwd)
        return run_deez(args, env=env_vars, input_data=input_data, cwd=cwd)

    def run_entrypoint(self, argv):
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.dict(os.environ, self.env, clear=False), redirect_stdout(output):
            with self.assertRaises(SystemExit) as ctx:
                deez_module.run_entrypoint()
        return ctx.exception.code, output.getvalue()

    def run_main(self, argv):
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.dict(os.environ, self.env, clear=False), redirect_stdout(output):
            deez_module.main()
        return output.getvalue()

    def _write_package_config(self):
        source_dir = Path(self.tmpdir.name) / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        config_path = Path(self.tmpdir.name) / "dots.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'name = "dots"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        return config_path

    def _write_git_config(self, *, git_url="https://github.com/HyDE-Project/HyDE.git", git_branch="dev"):
        config_path = Path(self.tmpdir.name) / "git-dots.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'version = "0.1.0"\n'
            f'git = "{git_url}"\n'
            f'git_branch = "{git_branch}"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        return config_path

    def _make_source_archive(self, archive_path, files):
        archive_path = Path(archive_path)
        stage_dir = Path(self.tmpdir.name) / f"source-archive-{archive_path.stem}"
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in files.items():
            file_path = stage_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(stage_dir, arcname="payload")
        return archive_path

    def _write_installed_manifest(self, section, owner="hyde_project", version=None, files=None, extra_meta=None):
        dots_dir = Path(self.xdg_data) / "deez" / "dots"
        dots_dir.mkdir(parents=True, exist_ok=True)
        manifest_lines = [f'name = "{section}"', f'owner = "{owner}"']
        if version is not None:
            manifest_lines.append(f'version = "{version}"')
        for key, value in (extra_meta or {}).items():
            if isinstance(value, bool):
                serialized = "true" if value else "false"
            else:
                serialized = f'"{value}"'
            manifest_lines.append(f"{key} = {serialized}")
        manifest_lines.append("")
        for file_entry in files or []:
            manifest_lines.extend(
                [
                    "[[files]]",
                    f'src = "{file_entry["src"]}"',
                    f'dst = "{file_entry["dst"]}"',
                ]
            )
            if "action" in file_entry:
                manifest_lines.append(f'action = "{file_entry["action"]}"')
            manifest_lines.append("")
        manifest_path = dots_dir / f"{section}.toml"
        manifest_path.write_text("\n".join(manifest_lines))
        return manifest_path

    def _make_bundle_tarball(self, bundle_path, name, owner, version, files, extra_meta=None):
        bundle_path = Path(bundle_path)
        with tempfile.TemporaryDirectory(prefix="deez-stage-") as stage_dir:
            stage_root = Path(stage_dir)
            manifest_meta = {
                "name": name,
                "owner": owner,
                "version": version,
            }
            manifest_meta.update(extra_meta or {})
            data_dir = stage_root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            manifest_files = []
            for file_entry in files:
                manifest_entry = {k: v for k, v in file_entry.items() if k != "content"}
                manifest_files.append(manifest_entry)
                data_path = data_dir / file_entry["src"]
                data_path.parent.mkdir(parents=True, exist_ok=True)
                data_path.write_text(file_entry.get("content", "dummy"))
            manifest_file = stage_root / "manifest.toml"
            manifest_file.write_bytes(deez_module.ManifestManager._serialize(manifest_meta, manifest_files))
            with tarfile.open(bundle_path, "w:gz") as tar:
                tar.add(manifest_file, arcname="manifest.toml")
                tar.add(data_dir, arcname="data")
        return bundle_path

    def _make_cache_bundle(self, bundle_name, manifest_text):
        cache_dir = Path(self.xdg_cache) / "deez" / "dots"
        cache_dir.mkdir(parents=True, exist_ok=True)
        stage = Path(self.tmpdir.name) / f"stage-{bundle_name}"
        stage.mkdir(parents=True, exist_ok=True)
        manifest = stage / "manifest.toml"
        manifest.write_text(manifest_text)
        tar_path = cache_dir / bundle_name
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(manifest, arcname="manifest.toml")
        return tar_path

    def _make_cli(self, main_config, source_dir=None):
        return deez_module.DeezCLI(
            argparse.Namespace(),
            main_config,
            str(source_dir or self.home_dir),
            str(self.home_dir),
            "0.1.0",
            [],
            "auto",
        )

    def test_command_help(self):
        cases = [
            (["dots"], "usage: deez dots"),
            (["deps"], "usage: deez deps"),
            (["backup"], "usage: deez backup"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                result = self.run_cli(args)
                self.assertEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout)

    def test_dots_trailing_double_dash_is_ignored(self):
        config_path = self._write_package_config()

        result = self.run_cli(["dots", "--config", str(config_path), "--"])

        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: deez dots", result.stdout)

    def test_interactive_menu_choose_many_supports_ranges(self):
        with patch("builtins.input", return_value="1-2,4"), redirect_stdout(io.StringIO()):
            selected = deez_module.InteractiveMenu.choose_many("Select dots", ["hyde", "kitty", "waybar", "hyprland"])

        self.assertEqual(selected, ["hyde", "kitty", "hyprland"])

    def test_resolve_config_dot_targets_prompts_when_global_dots_missing(self):
        cli = self._make_cli(
            {
                "global": {"owner": "hyde_project"},
                "hyde": {"files": [{"paths": ["hyde.conf"]}]},
                "kitty": {"files": [{"paths": ["kitty.conf"]}]},
                "waybar": {"files": [{"paths": ["config.jsonc"]}]},
                "hyprland": {"files": [{"paths": ["hyprland.conf"]}]},
            }
        )

        with patch.object(deez_module.DeezCLI, "_can_prompt_for_selection", return_value=True), patch("builtins.input", return_value="1,3-4"), redirect_stdout(io.StringIO()):
            selected = cli._resolve_config_dot_targets("bundle")

        self.assertEqual(selected, ["hyde", "waybar", "hyprland"])
    def test_resolve_config_dot_targets_renders_dot_descriptions(self):
        cli = self._make_cli(
            {
                "global": {"owner": "hyde_project", "description": "Shared desktop config for HyDE test fixtures"},
                "kitty": {"paths": ["kitty.conf"], "description": "Kitty terminal config"},
                "hyprland": {"paths": ["hyprland.conf"], "description": "Hyprland compositor config"},
            }
        )

        output = io.StringIO()
        with patch.object(deez_module.DeezCLI, "_can_prompt_for_selection", return_value=True), patch("builtins.input", return_value="1"), redirect_stdout(output):
            selected = cli._resolve_config_dot_targets("bundle")

        self.assertEqual(selected, ["kitty"])
        self.assertIn("Shared desktop config for HyDE test fixtures", output.getvalue())
        self.assertIn("Kitty terminal config", output.getvalue())
        self.assertIn("Hyprland compositor config", output.getvalue())
        self.assertNotIn("desc:", output.getvalue())

    def test_resolve_config_dot_targets_header_ignores_owner_when_description_exists(self):
        cli = self._make_cli(
            {
                "global": {"owner": "hyde_project", "description": "Shared desktop config for HyDE test fixtures"},
                "kitty": {"paths": ["kitty.conf"]},
                "hyprland": {"paths": ["hyprland.conf"], "owner": "other_owner"},
            }
        )

        output = io.StringIO()
        with patch.object(deez_module.DeezCLI, "_can_prompt_for_selection", return_value=True), patch("builtins.input", return_value="1"), redirect_stdout(output):
            cli._resolve_config_dot_targets("bundle")

        self.assertIn("Shared desktop config for HyDE test fixtures", output.getvalue())
        self.assertNotIn("multiple owners", output.getvalue())

    def test_resolve_config_dot_targets_header_falls_back_to_bundle_owner(self):
        cli = self._make_cli(
            {
                "global": {"owner": "hyde_project"},
                "kitty": {"paths": ["kitty.conf"]},
                "hyprland": {"paths": ["hyprland.conf"], "owner": "other_owner"},
            }
        )

        output = io.StringIO()
        with patch.object(deez_module.DeezCLI, "_can_prompt_for_selection", return_value=True), patch("builtins.input", return_value="1"), redirect_stdout(output):
            cli._resolve_config_dot_targets("bundle")

        self.assertIn("bundle dots from hyde_project", output.getvalue())

    def test_cache_list_no_cache(self):
        result = run_deez(["cache"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No cache entries found.", result.stdout)

    def test_cache_prune_dry_run(self):
        cache_dir = Path(self.xdg_cache) / "deez" / "dots"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "a.tar.gz").write_bytes(b"x")
        (cache_dir / "b.tar.gz").write_bytes(b"x")

        result = run_deez(["cache", "--prune", "--keep", "1", "--dry-run"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("would delete:", result.stdout)
        self.assertTrue((cache_dir / "a.tar.gz").exists())
        self.assertTrue((cache_dir / "b.tar.gz").exists())

    def test_cache_prune_keep_zero_deletes_all(self):
        cache_dir = Path(self.xdg_cache) / "deez" / "dots"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "a.tar.gz").write_bytes(b"x")
        (cache_dir / "a.sha256").write_text("hash a")
        (cache_dir / "b.tar.gz").write_bytes(b"x")
        (cache_dir / "b.sha256").write_text("hash b")

        result = run_deez(["cache", "--prune", "--keep", "0"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Pruning cache: keep=0, total=2, delete=2", result.stdout)
        self.assertFalse((cache_dir / "a.tar.gz").exists())
        self.assertFalse((cache_dir / "a.sha256").exists())
        self.assertFalse((cache_dir / "b.tar.gz").exists())
        self.assertFalse((cache_dir / "b.sha256").exists())

    def test_cache_list_handles_invalid_tarball(self):
        cache_dir = Path(self.xdg_cache) / "deez" / "dots"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "invalid.tar.gz").write_bytes(b"not a tar")

        result = run_deez(["cache"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("invalid.tar.gz", result.stdout)
        self.assertIn("?", result.stdout)

    def test_cache_list_shows_metadata(self):
        self._make_cache_bundle(
            "kitty-cache.tar.gz",
            'name = "kitty"\n'
            'version = "0.1.0"\n'
            'githash = "abcdef1234567890"\n'
            'builddate = "1778078960"\n'
            'origin = "package"\n'
        )

        result = self.run_cli(["cache"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("kitty", result.stdout)
        self.assertIn("0.1.0", result.stdout)
        self.assertIn("[package]", result.stdout)

    def test_dots_list_no_config(self):
        result = run_deez(["dots", "--list"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No dots found in the manifest.", result.stdout)

    def test_dots_uninstall_no_config(self):
        result = run_deez(["dots", "--uninstall", "kitty"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[UNINSTALL] 'kitty' is not installed — skipping.", result.stdout)

    def test_dots_package_auto_discovers_current_directory_config(self):
        work_dir = Path(self.tmpdir.name) / "workspace"
        source_dir = work_dir / "source"
        config_file = source_dir / "Configs/.config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "dots.toml").write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )

        result = self.run_cli_in_cwd(["dots", "--package"], cwd=work_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Using auto-discovered config from current directory:", result.stdout)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        self.assertTrue((work_dir / "build" / "kitty-0.1.0.tar.gz").exists())

    def test_dots_deploy_auto_discovers_current_directory_config(self):
        work_dir = Path(self.tmpdir.name) / "workspace-deploy"
        source_dir = work_dir / "source"
        config_file = source_dir / "Configs/.config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "dots.toml").write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )

        result = self.run_cli_in_cwd(["dots", "--deploy"], cwd=work_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Using auto-discovered config from current directory:", result.stdout)
        self.assertIn("[ok] Deploy complete", result.stdout)
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())

    def test_dots_package_without_global_dots_uses_all_discovered_sections_noninteractive(self):
        source_dir = Path(self.tmpdir.name) / "source"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/hypr").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        (source_dir / ".config/hypr/hyprland.conf").write_text("monitor = eDP-1")
        config_path = Path(self.tmpdir.name) / "discovered-dots.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'name = "dots"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
            '\n'
            '[hyprland]\n'
            '[[hyprland.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["hypr/hyprland.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        self.assertIn("[ok] Bundled hyprland ->", result.stdout)

    def test_dots_package_accepts_named_sections_after_flag(self):
        source_dir = Path(self.tmpdir.name) / "source-package-named"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/hypr").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        (source_dir / ".config/hypr/hyprland.conf").write_text("monitor = eDP-1")
        config_path = Path(self.tmpdir.name) / "named-package.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'name = "dots"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
            '\n'
            '[hyprland]\n'
            '[[hyprland.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["hypr/hyprland.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "kitty", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        self.assertNotIn("[ok] Bundled hyprland ->", result.stdout)

    def test_dots_package_accepts_all_keyword(self):
        source_dir = Path(self.tmpdir.name) / "source-package-all"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/hypr").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        (source_dir / ".config/hypr/hyprland.conf").write_text("monitor = eDP-1")
        config_path = Path(self.tmpdir.name) / "all-package.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'name = "dots"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
            '\n'
            '[hyprland]\n'
            '[[hyprland.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["hypr/hyprland.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "all", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        self.assertIn("[ok] Bundled hyprland ->", result.stdout)

    def test_dots_deploy_accepts_named_sections_after_flag(self):
        source_dir = Path(self.tmpdir.name) / "source-deploy-named"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/hypr").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        (source_dir / ".config/hypr/hyprland.conf").write_text("monitor = eDP-1")
        config_path = Path(self.tmpdir.name) / "named-deploy.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'name = "dots"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
            '\n'
            '[hyprland]\n'
            '[[hyprland.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["hypr/hyprland.conf"]\n'
        )

        result = self.run_cli(["dots", "--deploy", "kitty", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Deploy complete", result.stdout)
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())
        self.assertFalse((self.home_dir / ".config/hypr/hyprland.conf").exists())

    def test_read_meta_supports_file_url(self):
        config_path = self._write_package_config()

        loaded = deez_module.ReadMeta().read_location(config_path.resolve().as_uri())

        self.assertEqual(loaded["global"]["version"], "0.1.0")
        self.assertEqual(loaded["kitty"]["paths"], [".config/kitty/kitty.conf"])

    def test_read_meta_supports_relative_global_include(self):
        config_dir = Path(self.tmpdir.name) / "config-include-relative"
        config_dir.mkdir(parents=True, exist_ok=True)
        child_path = config_dir / "kitty.toml"
        child_path.write_text(
            '[global]\n'
            'owner = "included-owner"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )
        root_path = config_dir / "dots.toml"
        root_path.write_text(
            '[global]\n'
            'version = "0.1.0"\n'
            'owner = "root-owner"\n'
            'include = ["kitty.toml"]\n'
            '\n'
            '[hyprland]\n'
            'paths = [".config/hypr/hyprland.conf"]\n'
        )

        loaded = deez_module.ReadMeta().read_location(root_path)

        self.assertEqual(loaded["global"]["owner"], "root-owner")
        self.assertEqual(loaded["global"]["version"], "0.1.0")
        self.assertNotIn("include", loaded["global"])
        self.assertIn("kitty", loaded)
        self.assertIn("hyprland", loaded)
        self.assertEqual(loaded["kitty"]["files"][0]["paths"], ["kitty/kitty.conf"])

    def test_read_meta_supports_absolute_global_include(self):
        child_path = Path(self.tmpdir.name) / "shared-kitty.toml"
        child_path.write_text(
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        root_path = Path(self.tmpdir.name) / "dots-absolute-include.toml"
        root_path.write_text(
            '[global]\n'
            f'include = ["{child_path}"]\n'
            'version = "0.1.0"\n'
        )

        loaded = deez_module.ReadMeta().read_location(root_path)

        self.assertEqual(loaded["global"]["version"], "0.1.0")
        self.assertEqual(loaded["kitty"]["paths"], [".config/kitty/kitty.conf"])

    def test_dots_package_supports_config_file_url(self):
        config_path = self._write_package_config()
        source_dir = Path(self.tmpdir.name) / "source"
        config_file = source_dir / ".config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")

        result = self.run_cli(["dots", "--package", "--config", config_path.resolve().as_uri()])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
    def test_config_load_does_not_print_global_description_for_deps_check(self):
        config_path = Path(self.tmpdir.name) / "described-dots.toml"
        config_path.write_text(
            '[global]\n'
            'description = "Shared desktop config for HyDE test fixtures"\n'
            'version = "0.1.0"\n'
            'owner = "hyde_project"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )

        result = self.run_cli(["deps", "--check", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Config description:", result.stdout)

    def test_dots_source_override_uses_existing_non_git_path(self):
        config_path = self._write_git_config()
        source_override = Path(self.tmpdir.name) / "override-source"
        source_override.mkdir(parents=True, exist_ok=True)
        captured = {}

        class FakeCLI:
            def __init__(self, _args, _main_config, source_dir, *_rest, **_kwargs):
                captured["source_dir"] = source_dir

            def run(self):
                return None

        with patch.object(deez_module, "DeezCLI", FakeCLI), patch.object(deez_module.GitHandler, "is_git_repo", return_value=False), patch.object(deez_module.GitHandler, "git_clone") as git_clone, patch.object(deez_module.GitHandler, "git_fetch") as git_fetch, patch.object(deez_module.GitHandler, "git_pull") as git_pull, patch.object(deez_module.GitHandler, "git_checkout") as git_checkout:
            self.run_main(["deez", "dots", "--package", "--config", str(config_path), "--source", str(source_override)])

        self.assertEqual(captured["source_dir"], str(source_override))
        git_clone.assert_not_called()
        git_fetch.assert_not_called()
        git_pull.assert_not_called()
        git_checkout.assert_not_called()

    def test_dots_source_override_refreshes_matching_git_repo(self):
        config_path = self._write_git_config()
        source_override = Path(self.tmpdir.name) / "override-repo"
        source_override.mkdir(parents=True, exist_ok=True)
        captured = {}

        class FakeCLI:
            def __init__(self, _args, _main_config, source_dir, *_rest, **_kwargs):
                captured["source_dir"] = source_dir

            def run(self):
                return None

        with patch.object(deez_module, "DeezCLI", FakeCLI), patch.object(deez_module.GitHandler, "is_git_repo", return_value=True), patch.object(deez_module.GitHandler, "get_remote_url", return_value="git@github.com:HyDE-Project/HyDE.git"), patch.object(deez_module.GitHandler, "git_fetch") as git_fetch, patch.object(deez_module.GitHandler, "git_pull") as git_pull, patch.object(deez_module.GitHandler, "git_checkout") as git_checkout:
            self.run_main(["deez", "dots", "--package", "--config", str(config_path), "--source", str(source_override)])

        self.assertEqual(captured["source_dir"], str(source_override))
        git_fetch.assert_called_once_with(source_override, "dev")
        git_pull.assert_called_once_with(source_override, "dev")
        git_checkout.assert_called_once_with(source_override, "dev")

    def test_dots_source_override_skips_mismatched_git_repo(self):
        config_path = self._write_git_config()
        source_override = Path(self.tmpdir.name) / "override-other-repo"
        source_override.mkdir(parents=True, exist_ok=True)
        captured = {}

        class FakeCLI:
            def __init__(self, _args, _main_config, source_dir, *_rest, **_kwargs):
                captured["source_dir"] = source_dir

            def run(self):
                return None

        with patch.object(deez_module, "DeezCLI", FakeCLI), patch.object(deez_module.GitHandler, "is_git_repo", return_value=True), patch.object(deez_module.GitHandler, "get_remote_url", return_value="https://github.com/example/other.git"), patch.object(deez_module.GitHandler, "git_fetch") as git_fetch, patch.object(deez_module.GitHandler, "git_pull") as git_pull, patch.object(deez_module.GitHandler, "git_checkout") as git_checkout:
            self.run_main(["deez", "dots", "--package", "--config", str(config_path), "--source", str(source_override)])

        self.assertEqual(captured["source_dir"], str(source_override))
        git_fetch.assert_not_called()
        git_pull.assert_not_called()
        git_checkout.assert_not_called()

    def test_dots_source_override_clones_missing_path_from_configured_git(self):
        config_path = self._write_git_config()
        source_override = Path(self.tmpdir.name) / "cloned-source"
        captured = {}

        class FakeCLI:
            def __init__(self, _args, _main_config, source_dir, *_rest, **_kwargs):
                captured["source_dir"] = source_dir

            def run(self):
                return None

        with patch.object(deez_module, "DeezCLI", FakeCLI), patch.object(deez_module.GitHandler, "git_clone") as git_clone:
            self.run_main(["deez", "dots", "--package", "--config", str(config_path), "--source", str(source_override)])

        self.assertEqual(captured["source_dir"], str(source_override))
        git_clone.assert_called_once_with("https://github.com/HyDE-Project/HyDE.git", source_override, "dev")

    def test_dots_source_override_missing_path_without_git_fails_clearly(self):
        config_path = Path(self.tmpdir.name) / "no-git-source.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        source_override = Path(self.tmpdir.name) / "missing-source"

        exit_code, output = self.run_entrypoint(["deez", "dots", "--package", "--config", str(config_path), "--source", str(source_override)])

        self.assertEqual(exit_code, 1)
        self.assertIn(f"Source directory '{source_override}' does not exist and no git URL provided.", output)

    def test_dots_package_supports_local_tarball_source(self):
        archive_path = self._make_source_archive(
            Path(self.tmpdir.name) / "assets.tar.gz",
            {".config/kitty/kitty.conf": "font_size 12"},
        )
        config_path = Path(self.tmpdir.name) / "tarball-source.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{archive_path}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)

    def test_dots_package_supports_dot_level_tarball_source_legacy_entry(self):
        archive_path = self._make_source_archive(
            Path(self.tmpdir.name) / "cursor-theme.tar.gz",
            {"theme/cursor.theme": "cursor theme"},
        )
        config_path = Path(self.tmpdir.name) / "dot-level-source-legacy.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[cursor_theme]\n'
            f'source = "{archive_path}"\n'
            'source_root = "theme"\n'
            'target_root = "$HOME/.local/share/icons"\n'
            'paths = "cursor.theme"\n'
        )
        bundle_path = SCRIPT_DIR / "build" / "cursor_theme-0.1.0.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled cursor_theme ->", result.stdout)
        with tarfile.open(bundle_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn(f'source = "{archive_path}"', manifest_text)
        self.assertIn('src = "theme/cursor.theme"', manifest_text)

    def test_dots_package_file_entries_inherit_dot_level_source(self):
        global_source = Path(self.tmpdir.name) / "global-source"
        (global_source / ".config/global").mkdir(parents=True, exist_ok=True)
        (global_source / ".config/global/global.conf").write_text("global setting")
        archive_path = self._make_source_archive(
            Path(self.tmpdir.name) / "dot-source-files.tar.gz",
            {".config/kitty/kitty.conf": "font_size 12"},
        )
        config_path = Path(self.tmpdir.name) / "dot-level-source-files.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{global_source}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            f'source = "{archive_path}"\n'
            '[[kitty.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        with tarfile.open(bundle_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn(f'source = "{archive_path}"', manifest_text)
        self.assertIn('src = ".config/kitty/kitty.conf"', manifest_text)

    def test_dots_package_supports_file_url_tarball_source_override(self):
        archive_path = self._make_source_archive(
            Path(self.tmpdir.name) / "assets-url.tar.gz",
            {".config/kitty/kitty.conf": "font_size 12"},
        )
        config_path = Path(self.tmpdir.name) / "file-url-source.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path), "--source", archive_path.resolve().as_uri()])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)

    def test_dots_source_override_accepts_git_url(self):
        config_path = Path(self.tmpdir.name) / "git-url-source.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        captured = {}

        class FakeCLI:
            def __init__(self, _args, _main_config, source_dir, *_rest, **_kwargs):
                captured["source_dir"] = source_dir

            def run(self):
                return None

        with patch.object(deez_module, "DeezCLI", FakeCLI), patch.object(deez_module.GitHandler, "prepare_git_source", return_value="/tmp/deez-git-source") as prepare_git_source:
            self.run_main(["deez", "dots", "--package", "--config", str(config_path), "--source", "https://github.com/HyDE-Project/HyDE.git"])

        prepare_git_source.assert_called_once_with("https://github.com/HyDE-Project/HyDE.git", "main")
        self.assertEqual(captured["source_dir"], "/tmp/deez-git-source")

    def test_root_global_overrides_before_subcommand_override_config(self):
        config_path = self._write_git_config(git_url="https://github.com/example/original.git", git_branch="main")
        source_override = Path(self.tmpdir.name) / "root-source-override"
        captured = {}

        class FakeCLI:
            def __init__(self, _args, main_config, source_dir, *_rest, **_kwargs):
                captured["main_config"] = main_config
                captured["source_dir"] = source_dir

            def run(self):
                return None

        def fake_prepare_source(self, source_dir, git_url, target_branch, *, explicit_source_path=False):
            captured["prepare_source"] = {
                "source_dir": source_dir,
                "git_url": git_url,
                "target_branch": target_branch,
                "explicit_source_path": explicit_source_path,
            }
            return str(source_dir)

        with patch.object(deez_module, "DeezCLI", FakeCLI), patch.object(deez_module.GitHandler, "prepare_source", fake_prepare_source):
            self.run_main(
                [
                    "deez",
                    "--config",
                    str(config_path),
                    "--source",
                    str(source_override),
                    "--git",
                    "https://github.com/example/override.git",
                    "--git_branch",
                    "dev",
                    "dots",
                    "--package",
                ]
            )

        self.assertEqual(captured["prepare_source"]["source_dir"], str(source_override))
        self.assertEqual(captured["prepare_source"]["git_url"], "https://github.com/example/override.git")
        self.assertEqual(captured["prepare_source"]["target_branch"], "dev")
        self.assertTrue(captured["prepare_source"]["explicit_source_path"])
        self.assertEqual(captured["source_dir"], str(source_override))
        self.assertEqual(captured["main_config"]["global"]["git"], "https://github.com/example/override.git")
        self.assertEqual(captured["main_config"]["global"]["git_branch"], "dev")
        self.assertEqual(captured["main_config"]["global"]["source"], str(source_override))

    def test_dots_package_section_only_config_uses_cli_source_override(self):
        source_dir = Path(self.tmpdir.name) / "section-only-source"
        config_file = source_dir / "Configs/.config/dolphinrc"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("singleClick=false")
        config_path = Path(self.tmpdir.name) / "section-only.toml"
        config_path.write_text(
            '[dolphin]\n'
            'version = "0.1.0"\n'
            'owner = "The HyDE Project"\n'
            '\n'
            '[[dolphin.files]]\n'
            'source_root = "Configs"\n'
            'target_root = "${HOME}"\n'
            'paths = [".config/dolphinrc"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path), "--source", str(source_dir)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled dolphin ->", result.stdout)

    def test_dots_package_global_pre_command_failure_aborts(self):
        source_dir = Path(self.tmpdir.name) / "source-global-pre"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "global-pre.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            'pre_command = "false"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 1)
        self.assertIn("global pre_command failed", result.stdout)
        self.assertNotIn("Bundled kitty", result.stdout)

    def test_dots_package_dry_run_announces_global_pre_command_and_assumes_success(self):
        source_dir = Path(self.tmpdir.name) / "source-global-pre-dry-run"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "global-pre-dry-run.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            'pre_command = "false"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "--dry-run", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[DRY RUN] Would run global pre_command: false (assuming success)", result.stdout)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)

    def test_dots_package_dot_pre_command_failure_skips_only_that_dot(self):
        source_dir = Path(self.tmpdir.name) / "source-dot-pre"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/waybar").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        (source_dir / ".config/waybar/config.jsonc").write_text("{}")
        config_path = Path(self.tmpdir.name) / "dot-pre.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'pre_command = "false"\n'
            'paths = [".config/kitty/kitty.conf"]\n'
            '\n'
            '[waybar]\n'
            'paths = [".config/waybar/config.jsonc"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("Skipping dot 'kitty': pre_command failed", result.stdout)
        self.assertIn("[ok] Bundled waybar ->", result.stdout)
        self.assertNotIn("[ok] Bundled kitty ->", result.stdout)

    def test_dots_package_file_pre_command_failure_skips_only_that_entry(self):
        source_dir = Path(self.tmpdir.name) / "source-file-pre"
        (source_dir / ".config/kitty").mkdir(parents=True, exist_ok=True)
        (source_dir / ".config/kitty/kitty.conf").write_text("font_size 12")
        (source_dir / ".config/kitty/theme.conf").write_text("include theme")
        bundle_path = SCRIPT_DIR / "build" / "kitty-file-pre-2.0.0.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()
        config_path = Path(self.tmpdir.name) / "file-pre.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "2.0.0"\n'
            '\n'
            '[kitty-file-pre]\n'
            '[[kitty-file-pre.files]]\n'
            'pre_command = "false"\n'
            'source_root = ".config/kitty"\n'
            'target_root = "$HOME/.config/kitty"\n'
            'paths = ["theme.conf"]\n'
            '\n'
            '[[kitty-file-pre.files]]\n'
            'source_root = ".config/kitty"\n'
            'target_root = "$HOME/.config/kitty"\n'
            'paths = ["kitty.conf"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertIn("Skipping file entry in 'kitty-file-pre' (theme.conf): pre_command failed", result.stdout)
        self.assertTrue(bundle_path.exists())
        with tarfile.open(bundle_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn('src = ".config/kitty/kitty.conf"', manifest_text)
        self.assertNotIn('src = ".config/kitty/theme.conf"', manifest_text)

    def test_dots_package_without_config_shows_clear_error(self):
        result = self.run_cli(["dots", "--package"])

        self.assertEqual(result.returncode, 1)
        self.assertIn("No config file provided. Use --config or place dots.toml in the current directory.", result.stdout)

    def test_dots_export_no_config_uses_installed_manifest(self):
        section = "kitty"
        self._write_installed_manifest(
            "kitty",
            version="1.2.3",
            extra_meta={"source": "https://github.com/HyDE-Project/HyDE.git", "branch": "dev"},
            files=[{"src": ".config/kitty/kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
        )
        tracked_file = self.home_dir / ".config/kitty/kitty.conf"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text("dummy")

        result = self.run_cli(["dots", "--export", section])
        self.assertEqual(result.returncode, 0)
        self.assertIn("[EXPORT] Capturing installed dot: kitty", result.stdout)
        self.assertEqual(result.stdout.count("[ok] Exported kitty ->"), 1)
        self.assertTrue((SCRIPT_DIR / "build").exists())
        built = [p for p in os.listdir(SCRIPT_DIR / "build") if "kitty-1.2.3" in p]
        self.assertTrue(built, f"Expected exported package with version 1.2.3, got: {os.listdir(SCRIPT_DIR / 'build')}")
        with tarfile.open(SCRIPT_DIR / "build" / built[0], "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn('source = "https://github.com/HyDE-Project/HyDE.git"', manifest_text)
        self.assertIn('branch = "dev"', manifest_text)

    def test_dots_export_named_section_ignores_auto_discovered_config_and_uses_manifest(self):
        section = "kitty"
        self._write_installed_manifest(
            section,
            version="2.0.0",
            files=[{"src": ".config/kitty/kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
        )
        tracked_file = self.home_dir / ".config/kitty/kitty.conf"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text("dummy")
        work_dir = Path(self.tmpdir.name) / "workspace-export-manifest"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "dots.toml").write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'version = "0.1.0"\n'
            '\n'
            '[other]\n'
            '[[other.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["other/file.conf"]\n'
        )

        result = self.run_cli_in_cwd(["dots", "--export", section], cwd=work_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("[EXPORT] Capturing installed dot: kitty", result.stdout)
        self.assertNotIn("Using auto-discovered config", result.stdout)

    def test_dots_export_blank_without_config_shows_clear_error(self):
        result = self.run_cli(["dots", "--export"])

        self.assertEqual(result.returncode, 1)
        self.assertIn("Blank --export requires a config file.", result.stdout)

    def test_dots_export_blank_auto_discovers_current_directory_config(self):
        work_dir = Path(self.tmpdir.name) / "workspace-export"
        work_dir.mkdir(parents=True, exist_ok=True)
        export_file = self.home_dir / ".config/kitty/kitty.conf"
        export_file.parent.mkdir(parents=True, exist_ok=True)
        export_file.write_text("font_size 12")
        (work_dir / "dots.toml").write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )

        result = self.run_cli_in_cwd(["dots", "--export"], cwd=work_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Using auto-discovered config from current directory:", result.stdout)
        self.assertIn("[EXPORT] Capturing dot: kitty", result.stdout)
        self.assertIn("[ok] Exported kitty ->", result.stdout)

    def test_dots_export_installed_manifest_skips_non_home_files(self):
        bundle_path = SCRIPT_DIR / "build" / "kitty-home-only-9.9.9.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()
        self._write_installed_manifest(
            "kitty-home-only",
            version="9.9.9",
            files=[
                {"src": ".config/kitty/kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf", "action": "preserve"},
                {"src": "/etc/kitty/kitty.conf", "dst": "/etc/kitty/kitty.conf", "action": "sync"},
            ],
        )
        tracked_file = self.home_dir / ".config/kitty/kitty.conf"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text("dummy")

        result = self.run_cli(["dots", "--export", "kitty-home-only"])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[EXPORT] Capturing installed dot: kitty-home-only", result.stdout)
        self.assertTrue(bundle_path.exists())
        with tarfile.open(bundle_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn(f'dst = "{self.home_dir}/.config/kitty/kitty.conf"', manifest_text)
        self.assertNotIn('dst = "/etc/kitty/kitty.conf"', manifest_text)

    def test_manifest_serializes_file_action(self):
        manager = deez_module.ManifestManager()
        manager.base_dir = str(Path(self.tmpdir.name) / "manifest")
        file_entry = {"src": "kitty.conf", "dst": "/tmp/kitty.conf", "action": "preserve"}
        manager.save("kitty", {"name": "kitty", "state": "installed"}, [file_entry])
        loaded = manager.get_file_entries("kitty")
        self.assertEqual(loaded[0].get("action"), "preserve")

    def test_manifest_serializes_clean_target_bool(self):
        manager = deez_module.ManifestManager()
        manager.base_dir = str(Path(self.tmpdir.name) / "manifest")
        manager.save("kitty", {"name": "kitty", "state": "installed", "clean_target": True}, [])
        loaded = manager.load_desc("kitty")
        self.assertTrue(loaded.get("clean_target"))

    def test_normalize_action_cases(self):
        cases = [
            (None, "preserve"),
            ("overwrite", "sync"),
            ("sync", "sync"),
            ("weird", "preserve"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(deez_module.DeezUtils.normalize_action(value), expected)

    def test_fetch_all_deps_includes_file_dependency_blocks(self):
        package_manager = deez_module.PackageManager(runner=lambda *args, **kwargs: (True, "", ""))

        deps = package_manager.fetch_all_deps(
            {
                "global": {},
                "hyde": {
                    "dependency": [{"pacman": ["hyprland"]}],
                    "files": [
                        {
                            "paths": ["nvidia.conf"],
                            "dependency": [{"pacman": ["nvidia-utils"]}, {"dnf": ["another-dependency"]}],
                        }
                    ],
                },
            }
        )

        self.assertEqual(deps["pacman"], ["hyprland", "nvidia-utils"])
        self.assertEqual(deps["dnf"], ["another-dependency"])

    def test_load_pm_parses_entries(self):
        custom_commands = deez_module.PackageManager.load_pm(
            {
                "package_managers": [
                    {
                        "name": "yay",
                        "query": "yay -Qs",
                        "install": "yay -S",
                        "uninstall": "yay -R",
                        "update": "yay -Syu",
                    }
                ]
            }
        )

        self.assertEqual(custom_commands["yay"]["install"], "yay -S")
        self.assertEqual(custom_commands["yay"]["query"], "yay -Qs")

    def test_package_manager_commands_from_global_config_alias(self):
        custom_commands = deez_module.PackageManager.package_manager_commands_from_global_config(
            {"pm": {"name": "yay", "install": "yay -S --noconfirm"}}
        )

        self.assertEqual(custom_commands["yay"]["install"], "yay -S --noconfirm")

    def test_package_manager_custom_commands_override_defaults(self):
        package_manager = deez_module.PackageManager(
            runner=lambda *args, **kwargs: (True, "", ""),
            custom_commands={"yay": {"install": "yay -S --noconfirm"}},
        )

        self.assertEqual(package_manager.package_manager_commands["yay"]["install"], "yay -S --noconfirm")

    def test_dots_package_bundle_persists_dot_and_file_dependencies(self):
        source_dir = Path(self.tmpdir.name) / "source"
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()
        config_file = source_dir / ".config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "deps-package.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.dependency]]\n'
            'yay = ["kitty"]\n'
            '[[kitty.files]]\n'
            'source_root = ".config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
            '[[kitty.files.dependency]]\n'
            'pacman = ["nvidia-utils"]\n'
        )

        result = self.run_cli(["dots", "--package", "--config", str(config_path)])

        self.assertEqual(result.returncode, 0)
        self.assertTrue(bundle_path.exists())
        with tarfile.open(bundle_path, "r:gz") as tar:
            manifest = deez_module.toml.loads(tar.extractfile("manifest.toml").read().decode("utf-8"))
        self.assertEqual(manifest["dependency"][0]["yay"], ["kitty"])
        self.assertEqual(manifest["files"][0]["dependency"][0]["pacman"], ["nvidia-utils"])

    def test_write_dots_copy_with_action_sync_clean_target_moves_existing_dir(self):
        writer = deez_module.WriteDots()
        src_dir = Path(self.tmpdir.name) / "src"
        tgt_dir = Path(self.tmpdir.name) / "home"
        src_dir.mkdir(parents=True, exist_ok=True)
        tgt_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "nested.txt").write_text("source")
        (tgt_dir / "existing.txt").write_text("existing")

        result = writer._copy_with_action(str(src_dir), str(tgt_dir), "sync", clean_target=True)

        self.assertTrue(result)
        self.assertTrue((tgt_dir / "nested.txt").exists())
        self.assertFalse((tgt_dir / "existing.txt").exists())
        self.assertTrue((tgt_dir.parent / "home.old").exists())

    def test_write_dots_copy_with_action_preserve(self):
        writer = deez_module.WriteDots()
        src_dir = Path(self.tmpdir.name) / "src"
        tgt_dir = Path(self.tmpdir.name) / "home"
        src_file = src_dir / "file.txt"
        tgt_file = tgt_dir / "file.txt"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        tgt_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("source")
        tgt_file.write_text("existing")

        result = writer._copy_with_action(str(src_file), str(tgt_file), "preserve")
        self.assertFalse(result)
        self.assertEqual(tgt_file.read_text(), "existing")

    def test_write_dots_copy_with_action_preserves_symlink(self):
        writer = deez_module.WriteDots()
        src_dir = Path(self.tmpdir.name) / "src-link"
        tgt_dir = Path(self.tmpdir.name) / "home-link"
        src_dir.mkdir(parents=True, exist_ok=True)
        tgt_dir.mkdir(parents=True, exist_ok=True)
        src_link = src_dir / "kitty-current"
        os.symlink("kitty.conf", src_link)

        result = writer._copy_with_action(str(src_link), str(tgt_dir / "kitty-current"), "sync")

        self.assertTrue(result)
        self.assertTrue((tgt_dir / "kitty-current").is_symlink())
        self.assertEqual(os.readlink(tgt_dir / "kitty-current"), "kitty.conf")

    def test_write_dots_stage_ignores_globbed_paths(self):
        writer = deez_module.WriteDots()
        src_dir = Path(self.tmpdir.name) / "src"
        (src_dir / "keep.txt").parent.mkdir(parents=True, exist_ok=True)
        (src_dir / "keep.txt").write_text("keep")
        (src_dir / "skip.tmp").write_text("skip")
        (src_dir / "cache/ignored.txt").parent.mkdir(parents=True, exist_ok=True)
        (src_dir / "cache/ignored.txt").write_text("ignored")

        pkg_path = writer.stage(
            file_entries=[
                {
                    "src_root": str(src_dir),
                    "tgt_root": str(self.home_dir),
                    "rel_paths": ["."],
                    "ignored_paths": ["**/*.tmp", "cache"],
                    "action": "sync",
                }
            ],
            dot="kitty",
            owner="hyde_project",
            version="0.1.0",
            githash="deadbeef",
            out_dir=str(Path(self.tmpdir.name) / "out"),
        )

        self.assertTrue(Path(pkg_path).exists())
        with tarfile.open(pkg_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
            archived_names = tar.getnames()
        self.assertIn('src = "keep.txt"', manifest_text)
        self.assertNotIn('src = "skip.tmp"', manifest_text)
        self.assertNotIn('src = "cache/ignored.txt"', manifest_text)
        self.assertIn("data/keep.txt", archived_names)
        self.assertNotIn("data/skip.tmp", archived_names)
        self.assertNotIn("data/cache/ignored.txt", archived_names)

    def test_write_dots_export_ignores_globbed_paths(self):
        writer = deez_module.WriteDots()
        (self.home_dir / "keep.txt").write_text("keep")
        (self.home_dir / "skip.tmp").write_text("skip")
        (self.home_dir / "cache/ignored.txt").parent.mkdir(parents=True, exist_ok=True)
        (self.home_dir / "cache/ignored.txt").write_text("ignored")

        pkg_path = writer.export(
            rel_paths=["."],
            tgt_root=str(self.home_dir),
            dot="kitty-export",
            owner="hyde_project",
            version="0.1.0",
            ignored_paths=["**/*.tmp", "cache"],
        )

        self.assertTrue(Path(pkg_path).exists())
        with tarfile.open(pkg_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
            archived_names = tar.getnames()
        self.assertIn('src = "keep.txt"', manifest_text)
        self.assertNotIn('src = "skip.tmp"', manifest_text)
        self.assertNotIn('src = "cache/ignored.txt"', manifest_text)
        self.assertIn("data/keep.txt", archived_names)
        self.assertNotIn("data/skip.tmp", archived_names)
        self.assertNotIn("data/cache/ignored.txt", archived_names)

    def test_write_dots_stage_expands_globbed_paths(self):
        writer = deez_module.WriteDots()
        src_dir = Path(self.tmpdir.name) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "one.lua").write_text("one")
        (src_dir / "two.lua").write_text("two")
        (src_dir / "three.txt").write_text("three")

        pkg_path = writer.stage(
            file_entries=[
                {
                    "src_root": str(src_dir),
                    "tgt_root": str(self.home_dir),
                    "rel_paths": ["*.lua"],
                    "action": "sync",
                }
            ],
            dot="glob-stage",
            owner="hyde_project",
            version="0.1.0",
            githash="deadbeef",
            out_dir=str(Path(self.tmpdir.name) / "out"),
        )

        with tarfile.open(pkg_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn('src = "one.lua"', manifest_text)
        self.assertIn('src = "two.lua"', manifest_text)
        self.assertNotIn('src = "three.txt"', manifest_text)

    def test_write_dots_export_expands_globbed_paths(self):
        writer = deez_module.WriteDots()
        (self.home_dir / "one.lua").write_text("one")
        (self.home_dir / "two.lua").write_text("two")
        (self.home_dir / "three.txt").write_text("three")

        pkg_path = writer.export(
            rel_paths=["*.lua"],
            tgt_root=str(self.home_dir),
            dot="glob-export",
            owner="hyde_project",
            version="0.1.0",
        )

        with tarfile.open(pkg_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn('src = "one.lua"', manifest_text)
        self.assertIn('src = "two.lua"', manifest_text)
        self.assertNotIn('src = "three.txt"', manifest_text)

    def test_write_dots_stage_cleans_stage_dir_on_keyboard_interrupt(self):
        writer = deez_module.WriteDots()
        src_dir = Path(self.tmpdir.name) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "kitty.conf").write_text("source")
        file_entries = [{"src_root": str(src_dir), "tgt_root": str(self.home_dir), "rel_paths": ["kitty.conf"], "action": "sync"}]

        with patch.dict(os.environ, self.env, clear=False), patch.object(writer, "_expand_files", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                writer.stage(file_entries, "kitty", "hyde_project", "0.1.0", "deadbeef")

        self.assertFalse((Path(self.xdg_cache) / "deez" / "stage" / "kitty").exists())

    def test_write_dots_export_cleans_stage_dir_on_keyboard_interrupt(self):
        writer = deez_module.WriteDots()
        export_file = self.home_dir / "kitty.conf"
        export_file.parent.mkdir(parents=True, exist_ok=True)
        export_file.write_text("source")

        with patch.dict(os.environ, self.env, clear=False), patch.object(writer, "_expand_files", side_effect=KeyboardInterrupt()), patch.object(deez_module.time, "time", return_value=123):
            with self.assertRaises(KeyboardInterrupt):
                writer.export("kitty.conf", str(self.home_dir), "kitty", "hyde_project", "0.1.0")

        self.assertFalse((Path(self.xdg_cache) / "deez" / "stage" / "kitty-export-123").exists())

    def test_write_dots_export_preserves_symlink_loop(self):
        writer = deez_module.WriteDots()
        icons_dir = self.home_dir / ".local/share/icons/Wallbash-Icon/distro"
        icons_dir.mkdir(parents=True, exist_ok=True)
        loop_path = icons_dir / "hyde.png"
        os.symlink("hyde.png", loop_path)

        pkg_path = writer.export(
            rel_paths=[".local/share/icons/Wallbash-Icon/distro"],
            tgt_root=str(self.home_dir),
            dot="icons",
            owner="hyde_project",
            version="0.1.0",
        )

        with tarfile.open(pkg_path, "r:gz") as tar:
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
            link_member = tar.getmember("data/.local/share/icons/Wallbash-Icon/distro/hyde.png")
        self.assertIn('src = ".local/share/icons/Wallbash-Icon/distro/hyde.png"', manifest_text)
        self.assertTrue(link_member.issym())
        self.assertEqual(link_member.linkname, "hyde.png")

    def test_dots_export_with_config(self):
        config_path = Path(self.tmpdir.name) / "dots.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        export_file = self.home_dir / ".config/kitty/kitty.conf"
        export_file.parent.mkdir(parents=True, exist_ok=True)
        export_file.write_text("dummy")

        result = run_deez(["dots", "--export", "--config", str(config_path)], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[EXPORT] Capturing dot: kitty", result.stdout)
        self.assertEqual(result.stdout.count("[ok] Exported kitty ->"), 1)
        self.assertTrue((SCRIPT_DIR / "build").exists())
        self.assertTrue(any("kitty" in p for p in os.listdir(SCRIPT_DIR / "build")))

    def test_dots_install_preserves_symlink_from_bundle(self):
        bundle_path = Path(self.tmpdir.name) / "kitty-symlink.tar.gz"
        with tempfile.TemporaryDirectory(prefix="deez-symlink-bundle-") as stage_dir:
            stage_root = Path(stage_dir)
            data_dir = stage_root / "data" / ".config/kitty"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "kitty.conf").write_text("font_size 12")
            os.symlink("kitty.conf", data_dir / "current")
            (stage_root / "manifest.toml").write_text(
                'name = "kitty"\n'
                'owner = "hyde_project"\n'
                'version = "1.0"\n'
                '\n'
                '[[files]]\n'
                'src = ".config/kitty/current"\n'
                f'dst = "{self.home_dir}/.config/kitty/current"\n'
                'action = "sync"\n'
                '\n'
                '[[files]]\n'
                'src = ".config/kitty/kitty.conf"\n'
                f'dst = "{self.home_dir}/.config/kitty/kitty.conf"\n'
                'action = "sync"\n'
            )
            with tarfile.open(bundle_path, "w:gz") as tar:
                tar.add(stage_root / "manifest.toml", arcname="manifest.toml")
                tar.add(stage_root / "data", arcname="data")

        result = self.run_cli(["dots", "--install", str(bundle_path), "--no-backup"])

        self.assertEqual(result.returncode, 0)
        installed_link = self.home_dir / ".config/kitty/current"
        self.assertTrue(installed_link.is_symlink())
        self.assertEqual(os.readlink(installed_link), "kitty.conf")

    def test_dots_export_with_config_merges_multiple_file_entries(self):
        config_path = Path(self.tmpdir.name) / "hyprland.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            'git = "https://github.com/HyDE-Project/HyDE.git"\n'
            'git_branch = "dev"\n'
            '\n'
            '[hyprland]\n'
            'version = "1.2.3"\n'
            '\n'
            '[[hyprland.files]]\n'
            'source_root = "Configs/.config/hypr"\n'
            'target_root = "$HOME/.config/hypr"\n'
            'paths = ["hyprland.conf"]\n'
            '\n'
            '[[hyprland.files]]\n'
            'source_root = "Configs/.local/state/hypr"\n'
            'target_root = "$HOME/.local/state/hypr"\n'
            'paths = ["*.lua"]\n'
        )
        config_file = self.home_dir / ".config/hypr/hyprland.conf"
        state_file = self.home_dir / ".local/state/hypr/theme.lua"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("monitor=,preferred,auto,1")
        state_file.write_text("return {}")

        bundle_path = SCRIPT_DIR / "build" / "hyprland-1.2.3.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()

        result = run_deez(["dots", "--export", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("[EXPORT] Capturing dot: hyprland", result.stdout)
        self.assertEqual(result.stdout.count("[ok] Exported hyprland ->"), 1)
        self.assertTrue(bundle_path.exists())

        with tarfile.open(bundle_path, "r:gz") as tar:
            archived_names = tar.getnames()
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")

        self.assertIn("data/Configs/.config/hypr/hyprland.conf", archived_names)
        self.assertIn("data/Configs/.local/state/hypr/theme.lua", archived_names)
        self.assertIn('source = "https://github.com/HyDE-Project/HyDE.git"', manifest_text)
        self.assertIn('branch = "dev"', manifest_text)
        self.assertIn('src = "Configs/.config/hypr/hyprland.conf"', manifest_text)
        self.assertIn('src = "Configs/.local/state/hypr/theme.lua"', manifest_text)
        self.assertIn('source_root = "Configs/.config/hypr"', manifest_text)
        self.assertIn('source_root = "Configs/.local/state/hypr"', manifest_text)
        self.assertIn(f'dst = "{self.home_dir}/.config/hypr/hyprland.conf"', manifest_text)
        self.assertIn(f'dst = "{self.home_dir}/.local/state/hypr/theme.lua"', manifest_text)

    def test_dots_deploy_with_config_installs_built_bundle(self):
        source_dir = Path(self.tmpdir.name) / "source"
        config_file = source_dir / "Configs/.config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "deploy.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )

        result = run_deez(["dots", "--deploy", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        self.assertIn("Installed 'kitty'", result.stdout)
        self.assertIn("[ok] Deploy complete", result.stdout)
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())
        self.assertTrue((Path(self.xdg_data) / "deez" / "dots" / "kitty.toml").exists())

    def test_dots_deploy_checks_config_dependencies_before_packaging(self):
        main_config = {
            "global": {
                "home": str(self.home_dir),
                "owner": "hyde_project",
                "version": "0.1.0",
            },
            "kitty": {
                "dependency": [{"pacman": ["kitty"]}],
                "files": [{"paths": ["kitty.conf"]}],
            },
        }
        with patch.dict(os.environ, self.env, clear=False):
            cli = self._make_cli(main_config, source_dir=self.home_dir)
            cli.args = argparse.Namespace(
                deps_check=False,
                deps_update=False,
                install_deps=False,
                backup_list=False,
                backup_prune=False,
                do_package=False,
                do_export=False,
                do_install=False,
                do_deploy=True,
                do_uninstall=False,
                do_restore=False,
                do_downgrade=False,
                cache_prune=False,
                cache_list=False,
                list=False,
                no_backup=True,
                no_deps_checks=False,
                no_deps_install=False,
                dry_run=False,
            )
            cli.available_package_managers = ["pacman"]
            order = []
            cli.package_manager_instance.query_installed = lambda manager, package: False

            def fake_install_packages(dependency_map):
                order.append(("deps_install", dependency_map))
                return True

            def fake_do_package(*args, **kwargs):
                order.append(("package", kwargs.get("sections")))
                return [str(Path(self.tmpdir.name) / "kitty.tar.gz")]

            def fake_do_install(*args, **kwargs):
                order.append(("install", kwargs.get("prechecked_dependencies")))

            cli.package_manager_instance.install_packages = fake_install_packages

            with patch.object(cli, "_do_package", side_effect=fake_do_package), patch.object(cli, "_do_install", side_effect=fake_do_install):
                cli.run()

        self.assertEqual(order[0][0], "deps_install")
        self.assertEqual(order[1][0], "package")
        self.assertEqual(order[2], ("install", True))

    def test_dots_package_warns_on_missing_sources_and_continues(self):
        source_dir = Path(self.tmpdir.name) / "source"
        config_file = source_dir / "Configs/.config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "package-warn.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf", "kitty/missing.conf"]\n'
        )
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()

        result = run_deez(["dots", "--package", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("[warn] Source path missing:", result.stdout)
        self.assertIn("kitty/missing.conf", result.stdout)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        self.assertTrue(bundle_path.exists())
        with tarfile.open(bundle_path, "r:gz") as tar:
            archived_names = tar.getnames()
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn("data/Configs/.config/kitty/kitty.conf", archived_names)
        self.assertIn('src = "Configs/.config/kitty/kitty.conf"', manifest_text)
        self.assertNotIn('missing.conf', manifest_text)

    def test_dots_package_warns_when_ignored_paths_filter_all_matched_files(self):
        source_dir = Path(self.tmpdir.name) / "source"
        kept_file = source_dir / "Configs/.config/kitty/kitty.conf"
        ignored_file = source_dir / "Configs/.local/share/kitty/theme.conf"
        kept_file.parent.mkdir(parents=True, exist_ok=True)
        ignored_file.parent.mkdir(parents=True, exist_ok=True)
        kept_file.write_text("font_size 12")
        ignored_file.write_text("theme value")
        config_path = Path(self.tmpdir.name) / "package-ignored-all.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
            '\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.local/share/kitty"\n'
            'target_root = "$HOME/.local/share/kitty"\n'
            'paths = "."\n'
            'ignored_paths = ["*.conf"]\n'
        )
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        if bundle_path.exists():
            bundle_path.unlink()

        result = run_deez(["dots", "--package", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("[warn] Ignored all matched files:", result.stdout)
        self.assertIn("Configs/.local/share/kitty", result.stdout)
        self.assertIn("[ok] Bundled kitty ->", result.stdout)
        with tarfile.open(bundle_path, "r:gz") as tar:
            archived_names = tar.getnames()
            manifest_text = tar.extractfile("manifest.toml").read().decode("utf-8")
        self.assertIn("data/Configs/.config/kitty/kitty.conf", archived_names)
        self.assertNotIn("data/Configs/.local/share/kitty/", archived_names)
        self.assertNotIn('source_root = "Configs/.local/share/kitty"', manifest_text)

    def test_debug_flag_emits_debug_logs(self):
        source_dir = Path(self.tmpdir.name) / "source"
        config_file = source_dir / "Configs/.config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "debug-package.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )

        result = run_deez(["--debug", "dots", "--package", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("[DEBUG] Debug logging enabled", result.stderr)
        self.assertIn("[DEBUG] [PACKAGE] Packaging dot: kitty", result.stderr)

    def test_default_run_command_returns_false_on_non_zero_exit(self):
        success, out, err = deez_module.default_run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            retries=1,
        )

        self.assertFalse(success)
        self.assertEqual(out, "")
        self.assertEqual(err, "boom")

    def test_default_run_command_streams_live_output(self):
        output = io.StringIO()

        with redirect_stdout(output):
            success, out, err = deez_module.default_run_command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('prompt: '); sys.stderr.flush(); sys.stdout.write('done\\n'); sys.stdout.flush()",
                ],
                stream_output=True,
                retries=1,
            )

        self.assertTrue(success)
        self.assertEqual(err, "")
        self.assertIn("prompt: done", out)
        self.assertIn("prompt: done", output.getvalue())

    def test_package_manager_update_primes_sudo_and_streams_live_output(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return True, "", ""

        with patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in {"sudo", "pacman"} else None):
            manager = deez_module.PackageManager(runner=fake_runner)

        with patch.object(manager, "_should_stream_output", return_value=True), redirect_stdout(io.StringIO()):
            result = manager.update("pacman")

        self.assertTrue(result)
        self.assertEqual(calls[0][0], ["sudo", "-v"])
        self.assertTrue(calls[0][1]["stream_output"])
        self.assertEqual(calls[1][0], "sudo pacman -Syu")
        self.assertTrue(calls[1][1]["stream_output"])
        self.assertFalse(calls[1][1]["capture_output"])

    def test_dots_package_warns_about_stale_extracted_build_dir(self):
        source_dir = Path(self.tmpdir.name) / "source"
        config_file = source_dir / "Configs/.config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "package-stale.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        stale_dir = SCRIPT_DIR / "build" / "kitty-0.1.0"
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / "old.txt").write_text("stale")
        if bundle_path.exists():
            bundle_path.unlink()

        result = run_deez(["dots", "--package", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Existing extracted build directory may be stale:", result.stdout)
        self.assertTrue((stale_dir / "old.txt").exists())
        self.assertTrue(bundle_path.exists())

    def test_dots_package_force_removes_stale_extracted_build_dir(self):
        source_dir = Path(self.tmpdir.name) / "source"
        config_file = source_dir / "Configs/.config/kitty/kitty.conf"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("font_size 12")
        config_path = Path(self.tmpdir.name) / "package-force.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            f'source = "{source_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            '[[kitty.files]]\n'
            'source_root = "Configs/.config"\n'
            'target_root = "$HOME/.config"\n'
            'paths = ["kitty/kitty.conf"]\n'
        )
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        stale_dir = SCRIPT_DIR / "build" / "kitty-0.1.0"
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / "old.txt").write_text("stale")
        if bundle_path.exists():
            bundle_path.unlink()

        result = run_deez(["dots", "--package", "--force", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Removed existing extracted build directory:", result.stdout)
        self.assertFalse(stale_dir.exists())
        self.assertTrue(bundle_path.exists())

    def test_dots_export_warns_about_stale_extracted_build_dir(self):
        config_path = Path(self.tmpdir.name) / "export-stale.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        export_file = self.home_dir / ".config/kitty/kitty.conf"
        export_file.parent.mkdir(parents=True, exist_ok=True)
        export_file.write_text("dummy")
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        stale_dir = SCRIPT_DIR / "build" / "kitty-0.1.0"
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / "old.txt").write_text("stale")
        if bundle_path.exists():
            bundle_path.unlink()

        result = run_deez(["dots", "--export", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Existing extracted build directory may be stale:", result.stdout)
        self.assertTrue((stale_dir / "old.txt").exists())
        self.assertTrue(bundle_path.exists())

    def test_dots_export_force_removes_stale_extracted_build_dir(self):
        config_path = Path(self.tmpdir.name) / "export-force.toml"
        config_path.write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )
        export_file = self.home_dir / ".config/kitty/kitty.conf"
        export_file.parent.mkdir(parents=True, exist_ok=True)
        export_file.write_text("dummy")
        bundle_path = SCRIPT_DIR / "build" / "kitty-0.1.0.tar.gz"
        stale_dir = SCRIPT_DIR / "build" / "kitty-0.1.0"
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / "old.txt").write_text("stale")
        if bundle_path.exists():
            bundle_path.unlink()

        result = run_deez(["dots", "--export", "--force", "--config", str(config_path)], env=self.env)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Removed existing extracted build directory:", result.stdout)
        self.assertFalse(stale_dir.exists())
        self.assertTrue(bundle_path.exists())

    def test_dots_deploy_fails_when_package_produces_no_bundles(self):
        config_path = self._write_package_config()

        with patch.object(deez_module.DeezCLI, "_do_package", return_value=[]):
            exit_code, output = self.run_entrypoint(["deez", "dots", "--deploy", "--config", str(config_path)])

        self.assertEqual(exit_code, 1)
        self.assertIn("Deploy failed: bundling produced no bundles.", output)

    def test_run_entrypoint_handles_cancelled_actions(self):
        config_path = self._write_package_config()
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[{"src": "kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
        )
        cases = [
            (
                "package",
                ["deez", "dots", "--package", "--config", str(config_path)],
                patch.object(deez_module.DeezCLI, "_do_package", side_effect=KeyboardInterrupt()),
            ),
            (
                "install",
                ["deez", "dots", "--install", str(bundle_path)],
                patch.object(deez_module.DeezCLI, "_do_install", side_effect=KeyboardInterrupt()),
            ),
            (
                "deploy",
                ["deez", "dots", "--deploy", "--config", str(config_path)],
                patch.object(deez_module.DeezCLI, "_do_package", side_effect=KeyboardInterrupt()),
            ),
        ]

        for action, argv, mocked_call in cases:
            with self.subTest(action=action):
                with mocked_call:
                    exit_code, output = self.run_entrypoint(argv)
                self.assertEqual(exit_code, 1)
                self.assertIn("Cancelled.", output)

    def test_pyproject_console_entrypoint_uses_run_entrypoint(self):
        pyproject = (SCRIPT_DIR / "pyproject.toml").read_text()

        self.assertIn('deez = "deez:run_entrypoint"', pyproject)

    # ------------------------------------------------------------------ helpers
    def _make_backup_tarball(self, section, owner, version, timestamp, files):
        """
        Create a backup tarball at the correct path under XDG_DATA_HOME/deez/backup/user/.
        files: list of (rel_path_from_home, content_bytes)
        Returns the Path to the created tarball.
        """
        import re, io
        safe = lambda s: re.sub(r"[^a-zA-Z0-9._-]", "-", str(s or "unknown"))
        dirname = f"{safe(section)}.{safe(owner)}.{safe(version)}"
        backup_dir = Path(self.xdg_data) / "deez" / "backup" / "user" / dirname
        backup_dir.mkdir(parents=True, exist_ok=True)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Build file_pairs for manifest
            file_pairs = []
            data_files = []  # (arcname, content)
            for rel, content in files:
                dst_abs = str(self.home_dir / rel)
                data_rel = dst_abs.lstrip("/")
                file_pairs.append({"src": data_rel, "dst": dst_abs, "action": "sync"})
                data_files.append((f"data/{data_rel}", content if isinstance(content, bytes) else content.encode()))

            # Write manifest.toml
            manifest_lines = [
                f'name = "{section}"',
                f'owner = "{owner}"',
                f'version = "{version}"',
                'state = "backup"',
                '',
            ]
            for fp in file_pairs:
                manifest_lines += [
                    '[[files]]',
                    f'src = "{fp["src"]}"',
                    f'dst = "{fp["dst"]}"',
                    f'action = "{fp["action"]}"',
                    '',
                ]
            manifest_bytes = "\n".join(manifest_lines).encode()
            info = tarfile.TarInfo("manifest.toml")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))

            for arcname, content in data_files:
                info2 = tarfile.TarInfo(arcname)
                info2.size = len(content)
                tar.addfile(info2, io.BytesIO(content))

        tarball = backup_dir / f"{timestamp}.tar.gz"
        tarball.write_bytes(buf.getvalue())
        return tarball

    def _make_empty_backup_tarballs(self, section, owner, version, timestamps):
        """Create minimal (structurally valid) backup tarballs for prune/list tests."""
        import re, io
        safe = lambda s: re.sub(r"[^a-zA-Z0-9._-]", "-", str(s or "unknown"))
        dirname = f"{safe(section)}.{safe(owner)}.{safe(version)}"
        backup_dir = Path(self.xdg_data) / "deez" / "backup" / "user" / dirname
        backup_dir.mkdir(parents=True, exist_ok=True)
        for ts in timestamps:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz"):
                pass
            (backup_dir / f"{ts}.tar.gz").write_bytes(buf.getvalue())

    # ------------------------------------------------------------------ tests
    def test_dots_restore_no_config(self):
        result = run_deez(["dots", "--restore", "kitty"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No backup snapshots found.", result.stdout)

    def test_dots_restore_updates_manifest_state(self):
        section = "kitty"
        self._make_backup_tarball(
            "kitty", "hyde_project", "0.1.0", "20260506-174729",
            [(".config/kitty/kitty.conf", b"restored")],
        )

        result = run_deez(["dots", "--restore", section], env=self.env, input_data="1\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("[RESTORE] Restoring 'kitty' from", result.stdout)
        dots_manifest = Path(self.xdg_data) / "deez" / "dots" / "kitty.toml"
        self.assertTrue(dots_manifest.exists(), "dots store manifest should be written after restore")
        restored_text = dots_manifest.read_text()
        self.assertIn('installdate =', restored_text)
        self.assertNotIn('removeddate', restored_text)
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())

    def test_backup_list_no_config(self):
        result = run_deez(["backup", "--list"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("User backups", result.stdout)

    def test_backup_list_no_config_with_backups(self):
        self._make_empty_backup_tarballs("kitty", "unknown", "unknown", ["20240101-000000"])
        result = run_deez(["backup", "--list"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("User backups", result.stdout)
        self.assertIn("kitty", result.stdout)

    def test_backup_prune_defaults_to_keep_five(self):
        timestamps = [f"2024010{i + 1}-000000" for i in range(7)]
        self._make_empty_backup_tarballs("kitty", "unknown", "unknown", timestamps)

        result = run_deez(["backup", "--prune", "--dry-run"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Pruning backups for kitty: keep=5, total=7, delete=2", result.stdout)
        self.assertIn("would delete", result.stdout)

    def test_backup_prune_keep_one_dry_run(self):
        timestamps = [f"2024010{i + 1}-000000" for i in range(3)]
        self._make_empty_backup_tarballs("kitty", "unknown", "unknown", timestamps)

        result = run_deez(["backup", "--prune", "kitty", "--keep", "1", "--dry-run"], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Pruning backups for kitty: keep=1, total=3, delete=2", result.stdout)
        self.assertIn("would delete", result.stdout)

    def test_dots_install_dry_run(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[{"src": "kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
            extra_meta={"state": "installed"},
        )

        result = self.run_cli(["dots", "--install", str(bundle_path), "--dry-run"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("[DRY RUN] [INSTALL] 'kitty' would be installed (1 files).", result.stdout)

    def test_dots_install_pre_command_skips_bundle_and_file_scope(self):
        bundle_skip = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-bundle-pre.tar.gz",
            name="kitty-bundle-pre",
            owner="hyde_project",
            version="1.0",
            files=[{"src": "kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
            extra_meta={"pre_command": "false"},
        )
        bundle_partial = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-file-pre.tar.gz",
            name="kitty-file-pre-install",
            owner="hyde_project",
            version="1.0",
            files=[
                {"src": "theme.conf", "dst": f"{self.home_dir}/.config/kitty/theme.conf", "pre_command": "false"},
                {"src": "kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"},
            ],
        )

        result = self.run_cli(["dots", "--install", str(bundle_skip), str(bundle_partial), "--no-backup"])

        self.assertEqual(result.returncode, 0)
        self.assertIn("Skipping dot 'kitty-bundle-pre': pre_command failed", result.stdout)
        self.assertIn("Skipping file entry in 'kitty-file-pre-install' (theme.conf): pre_command failed", result.stdout)
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())
        self.assertFalse((self.home_dir / ".config/kitty/theme.conf").exists())

    def test_dots_install_dry_run_announces_bundle_and_file_pre_commands(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-install-dry-pre.tar.gz",
            name="kitty-install-dry-pre",
            owner="hyde_project",
            version="1.0",
            files=[
                {"src": "theme.conf", "dst": f"{self.home_dir}/.config/kitty/theme.conf", "pre_command": "false"},
                {"src": "kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"},
            ],
            extra_meta={"pre_command": "false"},
        )

        result = self.run_cli(["dots", "--install", str(bundle_path), "--dry-run"])

        self.assertEqual(result.returncode, 0)
        self.assertIn("[DRY RUN] Would run dot 'kitty-install-dry-pre' pre_command: false (assuming success)", result.stdout)
        self.assertIn("[DRY RUN] Would run file entry in 'kitty-install-dry-pre' (theme.conf) pre_command: false (assuming success)", result.stdout)
        self.assertIn("[DRY RUN] [INSTALL] 'kitty-install-dry-pre' would be installed (2 files).", result.stdout)

    def test_dots_install_installs_missing_dependencies_before_copy(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-deps.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[
                {
                    "src": "kitty.conf",
                    "dst": f"{self.home_dir}/.config/kitty/kitty.conf",
                    "dependency": [{"pacman": ["nvidia-utils"]}],
                }
            ],
            extra_meta={"dependency": [{"yay": ["kitty"]}]},
        )
        with patch.dict(os.environ, self.env, clear=False):
            cli = self._make_cli({"global": {}}, source_dir=self.home_dir)
            cli.args = argparse.Namespace(no_backup=True, no_deps_checks=False, no_deps_install=False)
            cli.available_package_managers = ["pacman", "yay"]
            order = []

            def fake_query_installed(manager, package):
                order.append(("check", manager, package))
                return False

            def fake_install_packages(dependency_map):
                order.append(("install", dependency_map))
                return True

            original_copy = deez_module.WriteDots._copy_with_action

            def wrapped_copy(src_path, tgt_path, action, clean_target=False):
                order.append(("copy", str(tgt_path)))
                return original_copy(src_path, tgt_path, action, clean_target=clean_target)

            cli.package_manager_instance.query_installed = fake_query_installed
            cli.package_manager_instance.install_packages = fake_install_packages

            with patch.object(deez_module.WriteDots, "_copy_with_action", side_effect=wrapped_copy):
                cli._do_install([str(bundle_path)])

        install_index = next(i for i, item in enumerate(order) if item[0] == "install")
        copy_index = next(i for i, item in enumerate(order) if item[0] == "copy")
        self.assertLess(install_index, copy_index)
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())

    def test_dots_install_dependency_preflight_shows_detected_managers_and_plan(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-deps-ui.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[
                {
                    "src": "kitty.conf",
                    "dst": f"{self.home_dir}/.config/kitty/kitty.conf",
                    "dependency": [{"pacman": ["nvidia-utils"]}],
                }
            ],
            extra_meta={"dependency": [{"yay": ["kitty"]}]},
        )
        with patch.dict(os.environ, self.env, clear=False):
            cli = self._make_cli({"global": {}}, source_dir=self.home_dir)
            cli.args = argparse.Namespace(no_backup=True, no_deps_checks=False, no_deps_install=False)
            cli.available_package_managers = ["pacman", "yay"]
            cli.package_manager_instance.query_installed = lambda manager, package: package == "kitty"
            cli.package_manager_instance.install_packages = lambda dependency_map: True
            output = io.StringIO()

            with redirect_stdout(output):
                cli._do_install([str(bundle_path)])

        text = output.getvalue()
        self.assertIn("Detected package managers: pacman, yay", text)
        self.assertIn("Dependency plan for: kitty", text)
        self.assertIn("yay: kitty", text)
        self.assertIn("pacman: nvidia-utils", text)
        self.assertIn("Deps already satisfied via yay: kitty", text)
        self.assertIn("Installing via pacman: nvidia-utils", text)

    def test_dots_install_honors_file_level_dependency_from_bundle_manifest(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-theme-deps.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[
                {
                    "src": "Configs/.config/kitty/hyde.conf",
                    "dst": f"{self.home_dir}/.config/kitty/hyde.conf",
                    "action": "sync",
                },
                {
                    "src": "Configs/.config/kitty/kitty.conf",
                    "dst": f"{self.home_dir}/.config/kitty/kitty.conf",
                    "action": "sync",
                },
                {
                    "src": "Configs/.config/kitty/theme.conf",
                    "dst": f"{self.home_dir}/.config/kitty/theme.conf",
                    "action": "preserve",
                    "dependency": [{"pacman": ["imagemagick"]}],
                },
            ],
            extra_meta={"dependency": [{"yay": ["kitty"]}]},
        )
        with patch.dict(os.environ, self.env, clear=False):
            cli = self._make_cli({"global": {}}, source_dir=self.home_dir)
            cli.args = argparse.Namespace(no_backup=True, no_deps_checks=False, no_deps_install=False)
            cli.available_package_managers = ["pacman", "yay"]
            checked = []
            installed = []
            output = io.StringIO()

            def fake_query_installed(manager, package):
                checked.append((manager, package))
                return package == "kitty"

            def fake_install_packages(dependency_map):
                installed.append(dependency_map)
                return True

            cli.package_manager_instance.query_installed = fake_query_installed
            cli.package_manager_instance.install_packages = fake_install_packages

            with redirect_stdout(output):
                cli._do_install([str(bundle_path)])

        self.assertIn(("yay", "kitty"), checked)
        self.assertIn(("pacman", "imagemagick"), checked)
        self.assertEqual(installed, [{"pacman": ["imagemagick"]}])
        self.assertIn("Installing via pacman: imagemagick", output.getvalue())
        self.assertTrue((self.home_dir / ".config/kitty/hyde.conf").exists())
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())
        self.assertTrue((self.home_dir / ".config/kitty/theme.conf").exists())

    def test_dots_install_prefers_first_available_manager_from_dependency_block(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "hyprland-alt-managers.tar.gz",
            name="hyprland",
            owner="hyde_project",
            version="1.0",
            files=[
                {
                    "src": "Configs/.config/hypr/hyprland.lua",
                    "dst": f"{self.home_dir}/.config/hypr/hyprland.lua",
                    "action": "sync",
                }
            ],
            extra_meta={"dependency": [{"yay": ["hyprland-git"], "paru": ["hyprland-git"], "dnf": ["hyprland"]}]},
        )
        with patch.dict(os.environ, self.env, clear=False):
            cli = self._make_cli({"global": {}}, source_dir=self.home_dir)
            cli.args = argparse.Namespace(no_backup=True, no_deps_checks=False, no_deps_install=False)
            cli.available_package_managers = ["pacman", "yay", "paru"]
            checked = []
            installed = []
            output = io.StringIO()

            def fake_query_installed(manager, package):
                checked.append((manager, package))
                return False

            def fake_install_packages(dependency_map):
                installed.append(dependency_map)
                return True

            cli.package_manager_instance.query_installed = fake_query_installed
            cli.package_manager_instance.install_packages = fake_install_packages

            with redirect_stdout(output):
                cli._do_install([str(bundle_path)])

        self.assertEqual(checked, [("yay", "hyprland-git")])
        self.assertEqual(installed, [{"yay": ["hyprland-git"]}])
        text = output.getvalue()
        self.assertIn("yay: hyprland-git", text)
        self.assertNotIn("paru: hyprland-git", text)
        self.assertNotIn("dnf: hyprland", text)
        self.assertNotIn("Dependency managers unavailable", text)

    def test_dots_install_no_deps_install_warns_and_continues(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-no-deps-install.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[{"src": "kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
            extra_meta={"dependency": [{"pacman": ["kitty"]}]},
        )
        with patch.dict(os.environ, self.env, clear=False):
            cli = self._make_cli({"global": {}}, source_dir=self.home_dir)
            cli.args = argparse.Namespace(no_backup=True, no_deps_checks=False, no_deps_install=True)
            cli.available_package_managers = ["pacman"]
            output = io.StringIO()
            cli.package_manager_instance.query_installed = lambda manager, package: False
            cli.package_manager_instance.install_packages = lambda dependency_map: True

            with redirect_stdout(output):
                cli._do_install([str(bundle_path)])

        self.assertIn("Skipping dependency installation because --no-deps-install was provided.", output.getvalue())
        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())

    def test_dots_install_no_deps_checks_skips_dependency_preflight(self):
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-no-deps-checks.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[{"src": "kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
            extra_meta={"dependency": [{"pacman": ["kitty"]}]},
        )
        with patch.dict(os.environ, self.env, clear=False):
            cli = self._make_cli({"global": {}}, source_dir=self.home_dir)
            cli.args = argparse.Namespace(no_backup=True, no_deps_checks=True, no_deps_install=False)
            cli.available_package_managers = ["pacman"]
            cli.package_manager_instance.query_installed = lambda manager, package: (_ for _ in ()).throw(AssertionError("query_installed should not run"))
            cli.package_manager_instance.install_packages = lambda dependency_map: (_ for _ in ()).throw(AssertionError("install_packages should not run"))

            cli._do_install([str(bundle_path)])

        self.assertTrue((self.home_dir / ".config/kitty/kitty.conf").exists())

    def test_dots_install_dry_run_reports_conflict_with_other_dot(self):
        target_path = f"{self.home_dir}/.config/kitty/kitty.conf"
        self._write_installed_manifest(
            "other-dot",
            files=[{"src": ".config/kitty/kitty.conf", "dst": target_path}],
        )
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-conflict.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[{"src": "kitty.conf", "dst": target_path}],
        )

        result = self.run_cli(["dots", "--install", str(bundle_path), "--dry-run"])

        self.assertEqual(result.returncode, 0)
        self.assertIn("File conflict:", result.stdout)
        self.assertIn("owned by other-dot", result.stdout)
        self.assertIn("'kitty' skipped due to conflict.", result.stdout)

    def test_dots_install_dry_run_reports_declared_dot_conflict(self):
        self._write_installed_manifest(
            "hyprland",
            files=[{"src": ".config/hypr/hyprland.conf", "dst": f"{self.home_dir}/.config/hypr/hyprland.conf"}],
        )
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "hyprland-legacy-conflict.tar.gz",
            name="hyprland-legacy",
            owner="hyde_project",
            version="1.0",
            files=[{"src": "hyprland.conf", "dst": f"{self.home_dir}/.config/hypr/hyprland-legacy.conf"}],
            extra_meta={"conflicts": ["hyprland"]},
        )

        result = self.run_cli(["dots", "--install", str(bundle_path), "--dry-run"])

        self.assertEqual(result.returncode, 0)
        self.assertIn("Dot conflict:", result.stdout)
        self.assertIn("conflicts with installed dot 'hyprland'", result.stdout)
        self.assertIn("'hyprland-legacy' skipped due to conflict.", result.stdout)

    def test_dots_install_dry_run_reports_conflicted_paths_conflict(self):
        conflict_path = f"{self.home_dir}/.config/hypr/hyprland.conf"
        self._write_installed_manifest(
            "hyprland",
            files=[{"src": ".config/hypr/hyprland.conf", "dst": conflict_path}],
        )
        bundle_path = self._make_bundle_tarball(
            Path(self.tmpdir.name) / "kitty-conflicted-path.tar.gz",
            name="kitty",
            owner="hyde_project",
            version="1.0",
            files=[
                {
                    "src": "kitty.conf",
                    "dst": f"{self.home_dir}/.config/kitty/kitty.conf",
                    "conflicted_paths": [conflict_path],
                }
            ],
        )

        result = self.run_cli(["dots", "--install", str(bundle_path), "--dry-run"])

        self.assertEqual(result.returncode, 0)
        self.assertIn("File conflict:", result.stdout)
        self.assertIn(conflict_path, result.stdout)
        self.assertIn("owned by hyprland", result.stdout)
        self.assertIn("'kitty' skipped due to conflict.", result.stdout)

    def test_dots_uninstall_dry_run(self):
        section = "kitty"
        self._write_installed_manifest(
            "kitty",
            files=[{"src": ".config/kitty/kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
        )
        tracked_file = self.home_dir / ".config/kitty/kitty.conf"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text("dummy")

        result = self.run_cli(["dots", "--uninstall", section, "--dry-run"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("[DRY RUN] [UNINSTALL] Would remove dot 'kitty'", result.stdout)
        self.assertTrue(tracked_file.exists(), "Dry-run uninstall should not remove tracked files")

    def test_dots_uninstall_interactive_cancel(self):
        self._write_installed_manifest(
            "kitty",
            files=[{"src": ".config/kitty/kitty.conf", "dst": f"{self.home_dir}/.config/kitty/kitty.conf"}],
        )

        result = self.run_cli(["dots", "--uninstall"], input_data="\n")

        self.assertEqual(result.returncode, 0)
        self.assertIn("Installed dots:", result.stdout)
        self.assertIn("Cancelled.", result.stdout)

    def test_dots_restore_dry_run(self):
        section = "kitty"
        self._make_backup_tarball(
            "kitty", "hyde_project", "1.0", "20240101-000000",
            [(".config/kitty/kitty.conf", b"dummy")],
        )

        result = run_deez(["dots", "--restore", section, "--dry-run"], env=self.env, input_data="1\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("[DRY RUN] [INSTALL] 'kitty' would be installed (1 files).", result.stdout)

    def test_dots_restore_cancel_snapshot_selection(self):
        self._make_backup_tarball(
            "kitty", "hyde_project", "1.0", "20240101-000000",
            [(".config/kitty/kitty.conf", b"dummy")],
        )

        result = self.run_cli(["dots", "--restore", "kitty"], input_data="0\n")

        self.assertEqual(result.returncode, 0)
        self.assertIn("Skipping 'kitty'.", result.stdout)
        self.assertFalse((self.home_dir / ".config/kitty/kitty.conf").exists())

    def test_deps_check_with_temp_config(self):
        result = run_deez(["deps", "--check", "--config", str(EXAMPLE_CONFIG)], env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            "Deps:" in result.stdout,
            msg=f"Expected deps output in stdout, got: {result.stdout!r}",
        )

    def test_deps_check_auto_discovers_current_directory_config(self):
        work_dir = Path(self.tmpdir.name) / "workspace-deps"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "dots.toml").write_text(
            '[global]\n'
            f'home = "{self.home_dir}"\n'
            'owner = "hyde_project"\n'
            'version = "0.1.0"\n'
            '\n'
            '[kitty]\n'
            'paths = [".config/kitty/kitty.conf"]\n'
        )

        result = self.run_cli_in_cwd(["deps", "--check"], cwd=work_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("Using auto-discovered config from current directory:", result.stdout)
        self.assertIn("Deps:", result.stdout)


if __name__ == "__main__":
    unittest.main()
