import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from zed_transfer.cli import main
from zed_transfer.config import ConfigError, load_config
from zed_transfer.presets import PresetError, create_project_config, project_config_path, save_custom_preset


class PresetTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        for target in ["zed_transfer.auto_authorization._account_home", "zed_transfer.presets._account_home"]:
            home = patch(target, return_value=self.home)
            home.start()
            self.addCleanup(home.stop)

    def test_built_in_presets_create_private_manual_only_configurations(self):
        for name, protocol, password in (
            ("sftp-key", "sftp", None),
            ("sftp-password", "sftp", True),
            ("ftps", "ftps", True),
        ):
            with self.subTest(preset=name):
                root = self.root / name
                root.mkdir()
                config_file = create_project_config(root, name)
                self.assertEqual(config_file, root / ".vscode" / "sftp.json")
                self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
                config = load_config(root)
                self.assertEqual(config.protocol, protocol)
                self.assertEqual(config.host, "example.invalid")
                self.assertEqual(config.password, password)
                self.assertFalse(config.upload_on_save)

    def test_interactive_task_creates_selected_config_in_empty_project(self):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["--root", str(self.root), "install-zed"]), 0)
            tasks = json.loads((self.root / ".zed" / "tasks.json").read_text(encoding="utf-8"))
            task = next(item for item in tasks if item["label"] == "Crea JSON Zed Transfer")
            self.assertEqual(task["cwd"], "$ZED_WORKTREE_ROOT")
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="3"):
                self.assertEqual(main(["--root", str(self.root), *task["args"]]), 0)
        self.assertIn(str(self.root), output.getvalue())
        self.assertEqual(load_config(self.root).protocol, "ftps")

    def test_interactive_task_cancel_and_existing_config_are_safe(self):
        args = ["--root", str(self.root), "init"]
        with redirect_stdout(StringIO()), patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", return_value="0"):
                self.assertEqual(main(args), 0)
        self.assertIsNone(project_config_path(self.root))
        path = create_project_config(self.root, "ftps")
        original = path.read_bytes()
        with redirect_stderr(StringIO()) as error, patch("builtins.input") as prompt:
            self.assertEqual(main(args), 1)
        prompt.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("Configurazione già presente", error.getvalue())

    def test_does_not_replace_or_shadow_project_configuration(self):
        original = create_project_config(self.root, "sftp-key")
        raw = original.read_bytes()
        with self.assertRaisesRegex(PresetError, "esiste"):
            create_project_config(self.root, "ftps")
        self.assertEqual(original.read_bytes(), raw)
        original.unlink()
        alternate = self.root / ".zed" / "sftp.json"
        alternate.parent.mkdir()
        alternate.write_bytes(raw)
        with self.assertRaisesRegex(PresetError, "esiste"):
            create_project_config(self.root, "sftp-key")
        self.assertFalse(original.exists())
        self.assertEqual(alternate.read_bytes(), raw)

    def test_project_config_finder_checks_precedence_and_opens_invalid_json(self):
        self.assertIsNone(project_config_path(self.root))
        alternate = self.root / ".zed" / "sftp.json"
        alternate.parent.mkdir()
        alternate.write_text("{incompleto", encoding="utf-8")
        self.assertEqual(project_config_path(self.root), alternate)
        preferred = self.root / ".vscode" / "sftp.json"
        preferred.parent.mkdir()
        preferred.write_text("", encoding="utf-8")
        self.assertEqual(project_config_path(self.root), preferred)
        preferred.unlink()
        preferred.symlink_to(alternate)
        with self.assertRaisesRegex(PresetError, "symlink"):
            project_config_path(self.root)

    def test_metadata_directory_links_and_destination_links_are_rejected(self):
        external = self.base / "external"
        external.mkdir()
        vscode = self.root / ".vscode"
        vscode.symlink_to(external, target_is_directory=True)
        with self.assertRaises(PresetError):
            create_project_config(self.root, "sftp-key")
        self.assertFalse(any(external.iterdir()))
        vscode.unlink()
        vscode.mkdir()
        external_config = external / "sftp.json"
        (vscode / "sftp.json").symlink_to(external_config)
        with self.assertRaises(PresetError):
            create_project_config(self.root, "sftp-key")
        self.assertFalse(external_config.exists())

    def test_personal_preset_redacts_passwords_and_disables_automatic_upload_per_profile(self):
        source = self.root / "incoming.json"
        source.write_text(
            "// imported\n" + json.dumps({
                "name": "Test",
                "host": "remote.example.test",
                "username": "deploy",
                "password": "root test secret",
                "passphrase": "key secret",
                "uploadOnSave": True,
                "extraSecret": {"apikey": "another secret"},
                "profiles": {
                    "prod": {"password": "profile secret", "uploadOnSave": True},
                    "stage": {"password": None, "uploadOnSave": True},
                },
                "defaultProfile": "stage",
            }),
            encoding="utf-8",
        )
        saved = save_custom_preset(self.root, Path("incoming.json"))
        self.assertEqual(saved, self.home / ".config" / "zed-transfer" / "presets" / "sftp.json")
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        raw = saved.read_text(encoding="utf-8")
        self.assertNotIn("secret", raw)
        self.assertNotIn("passphrase", raw)
        self.assertTrue(json.loads(raw)["password"])
        self.assertIn("root test secret", source.read_text(encoding="utf-8"))
        new_root = self.base / "another-project"
        new_root.mkdir()
        installed = create_project_config(new_root, "custom")
        self.assertEqual(installed.read_text(encoding="utf-8"), raw)
        stage = load_config(new_root)
        prod = load_config(new_root, profile="prod")
        self.assertEqual(stage.host, "remote.example.test")
        self.assertIsNone(stage.password)
        self.assertTrue(prod.password)
        self.assertFalse(stage.upload_on_save)
        self.assertFalse(prod.upload_on_save)

    def test_personal_preset_rejects_symlinked_or_public_source_of_preset_and_unsafe_options(self):
        incoming = self.root / ".vscode" / "sftp.json"
        incoming.parent.mkdir()
        fixture = {"host": "remote.example.test", "username": "deploy"}
        incoming.write_text(json.dumps(fixture | {"hop": {"host": "jump.example.test"}}), encoding="utf-8")
        with self.assertRaisesRegex(PresetError, "hop"):
            save_custom_preset(self.root)
        incoming.write_text(json.dumps(fixture), encoding="utf-8")
        saved = save_custom_preset(self.root)
        saved.chmod(0o644)
        target_root = self.base / "other"
        target_root.mkdir()
        with self.assertRaises(PresetError):
            create_project_config(target_root, "custom")
        saved.unlink()
        saved.symlink_to(incoming)
        with self.assertRaises(PresetError):
            create_project_config(target_root, "custom")
        with self.assertRaises(PresetError):
            save_custom_preset(self.root)
        self.assertEqual(json.loads(incoming.read_text(encoding="utf-8")), fixture)
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(fixture), encoding="utf-8")
        with self.assertRaises(ConfigError):
            save_custom_preset(self.root, outside)

    def test_custom_template_is_an_explicit_replace_in_private_home(self):
        source = self.root / "different.json"
        for host in ("first.example.test", "second.example.test"):
            source.write_text(json.dumps({"host": host, "username": "deploy"}), encoding="utf-8")
            saved = save_custom_preset(self.root, source)
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["host"], host)
        self.assertEqual(os.stat(saved).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
