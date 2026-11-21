import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zed_transfer.cli import _auto_upload, _enable_auto_upload, _preset_menu
from zed_transfer.config import load_config
from zed_transfer.local import LocalError
from zed_transfer.lsp import MenuPrompt, SavePrompt, TransferResult, UploadJob


class AutoUploadTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "index.php"
        self.file.write_text("<?php echo 'saved';", encoding="utf-8")
        self.source = self.root / ".vscode" / "sftp.json"
        self.source.parent.mkdir()
        self.source.write_text('''{
          "host": "example.test", "username": "deploy", "remotePath": "/wordpress",
          "uploadOnSave": true, "ignore": ["ignored.php"]
        }''', encoding="utf-8")
        self.auth = SimpleNamespace(config_path=self.source, name=None, profile=None, allow_config_password=False)
        auth = patch("zed_transfer.cli.get_auto_upload_status", return_value=(self.auth, False))
        self.get_auth = auth.start()
        self.addCleanup(auth.stop)
        verify = patch("zed_transfer.cli.verify_authorization", return_value=True)
        self.verify = verify.start()
        self.addCleanup(verify.stop)
        self.prompted = set()

    def save(self, file=None):
        return _auto_upload(self.root, self.root, file or self.file, self.prompted)

    def test_upload_uses_snapshot_from_approved_project(self):
        calls = []

        def upload(staged, remote):
            self.assertNotEqual(staged, self.file)
            calls.append((staged.read_text(encoding="utf-8"), remote))

        @contextmanager
        def connect(config, password):
            self.assertEqual(config.host, "example.test")
            self.assertIsNone(password)
            yield SimpleNamespace(upload=upload)

        with patch("zed_transfer.cli.open_transport", side_effect=connect):
            job = self.save()
            self.assertIsInstance(job, UploadJob)
            self.assertEqual(calls, [])
            result = job.run()
        self.assertTrue(result.success)
        self.assertIn("Caricato", result.message)
        self.assertEqual(calls, [("<?php echo 'saved';", "/wordpress/index.php")])

    def test_skips_revoked_changed_ignored_and_protected_files(self):
        with patch("zed_transfer.cli.open_transport") as connect:
            self.get_auth.return_value = (None, True)
            self.assertIsNone(self.save())
            self.get_auth.return_value = (self.auth, False)
            self.verify.return_value = False
            self.assertIsInstance(self.save(), SavePrompt)
            self.verify.return_value = True
            for name in ("wp-config.php", "ignored.php", ".env"):
                file = self.root / name
                file.write_text("fixture", encoding="utf-8")
                self.assertIsNone(self.save(file))
            connect.assert_not_called()

    def test_requires_explicit_consent_for_existing_password(self):
        original = self.source.read_text(encoding="utf-8")
        self.source.write_text(original.replace('{', '{"password": "fixture",', 1), encoding="utf-8")
        config = load_config(self.root)
        args = SimpleNamespace(password=False, allow_config_password=False)
        with self.assertRaisesRegex(LocalError, "--allow-config-password"):
            _enable_auto_upload(args, config)

    def test_first_save_requests_ftps_consent_and_only_uploads_after_click(self):
        self.get_auth.return_value = (None, False)
        original = self.source.read_text(encoding="utf-8")
        self.source.write_text(
            original.replace('{', '{"protocol": "ftps", "password": "local-secret",', 1),
            encoding="utf-8",
        )
        approved = SimpleNamespace(**vars(self.auth))
        approved.allow_config_password = True
        calls = []

        def approve(config, *, allow_config_password, expected_status):
            self.assertEqual(config.host, "example.test")
            self.assertTrue(allow_config_password)
            self.assertEqual(expected_status, (None, False))
            self.get_auth.return_value = (approved, False)
            return approved

        @contextmanager
        def connect(config, password):
            self.assertEqual(config.protocol, "ftps")
            self.assertEqual(password, "local-secret")
            yield SimpleNamespace(upload=lambda staged, remote: calls.append(remote))

        with patch("zed_transfer.cli.authorize", side_effect=approve) as store:
            with patch("zed_transfer.cli.open_transport", side_effect=connect) as transport:
                prompt = self.save()
                self.assertIsInstance(prompt, SavePrompt)
                self.assertIn("ftps://deploy@example.test:21/wordpress", prompt.message)
                self.assertIn("password", prompt.message)
                self.assertNotIn("local-secret", prompt.message)
                self.assertIsNone(self.save())
                store.assert_not_called()
                transport.assert_not_called()
                job = prompt.accept()
                self.assertIsInstance(job, UploadJob)
                self.assertEqual(calls, [])
                self.assertIn("Caricato", job.run().message)
        self.assertEqual(calls, ["/wordpress/index.php"])

    def test_changed_configuration_invalidates_open_prompt(self):
        self.get_auth.return_value = (None, False)
        prompt = self.save()
        self.assertIsInstance(prompt, SavePrompt)
        original = self.source.read_text(encoding="utf-8")
        self.source.write_text(original.replace("/wordpress", "/another-site"), encoding="utf-8")
        with patch("zed_transfer.cli.authorize") as approve, patch("zed_transfer.cli.open_transport") as connect:
            result = prompt.accept()
            self.assertIsInstance(result, TransferResult)
            self.assertFalse(result.success)
            self.assertIn("Configurazione cambiata", result.message)
            approve.assert_not_called()
            connect.assert_not_called()
        self.assertIn("/another-site", self.save().message)

    def test_confirmation_cannot_send_a_file_written_after_the_prompt(self):
        self.get_auth.return_value = (None, False)
        prompt = self.save()
        self.file.write_text("a later write", encoding="utf-8")
        with patch("zed_transfer.cli.authorize", side_effect=lambda *args, **kwargs: self.auth) as approve:
            with patch("zed_transfer.cli.open_transport") as connect:
                job = prompt.accept()
                self.get_auth.return_value = (self.auth, False)
                result = job.run()
                self.assertFalse(result.success)
                self.assertIn("Il file è cambiato", result.message)
                approve.assert_called_once()
                connect.assert_not_called()

    def test_config_menu_existing_project_can_open_and_save_a_preset(self):
        menu = _preset_menu(self.root, self.root)
        self.assertIsInstance(menu, MenuPrompt)
        self.assertEqual(menu.actions, ("Apri sftp.json", "Salva come modello personale"))
        self.assertEqual(menu.select("Apri sftp.json").open_path, self.source)
        with patch("zed_transfer.cli.save_custom_preset") as save:
            result = menu.select("Salva come modello personale")
        self.assertIn("Modello personale salvato", result.message)
        save.assert_called_once_with(self.root)

    def test_config_menu_creates_template_only_after_choice(self):
        self.source.unlink()
        menu = _preset_menu(self.root, self.root)
        self.assertIn("Crea JSON · FTPS", menu.actions)
        self.assertFalse(self.source.exists())
        config = menu.select("Crea JSON · FTPS")
        self.assertEqual(config.open_path, self.source)
        self.assertFalse(load_config(self.root).upload_on_save)
