import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zed_transfer.cli import _auto_upload, _enable_auto_upload
from zed_transfer.config import load_config
from zed_transfer.local import LocalError


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
        auth = patch("zed_transfer.cli.get_authorization", return_value=self.auth)
        self.get_auth = auth.start()
        self.addCleanup(auth.stop)
        verify = patch("zed_transfer.cli.verify_authorization", return_value=True)
        self.verify = verify.start()
        self.addCleanup(verify.stop)

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
            result = _auto_upload(self.root, self.root, self.file)
        self.assertIn("Caricato", result)
        self.assertEqual(calls, [("<?php echo 'saved';", "/wordpress/index.php")])

    def test_skips_revoked_changed_ignored_and_protected_files(self):
        with patch("zed_transfer.cli.open_transport") as connect:
            self.get_auth.return_value = None
            self.assertIsNone(_auto_upload(self.root, self.root, self.file))
            self.get_auth.return_value = self.auth
            self.verify.return_value = False
            self.assertIn("Configurazione cambiata", _auto_upload(self.root, self.root, self.file))
            self.verify.return_value = True
            for name in ("wp-config.php", "ignored.php", ".env"):
                file = self.root / name
                file.write_text("fixture", encoding="utf-8")
                self.assertIsNone(_auto_upload(self.root, self.root, file))
            connect.assert_not_called()

    def test_requires_explicit_consent_for_existing_password(self):
        original = self.source.read_text(encoding="utf-8")
        self.source.write_text(original.replace('{', '{"password": "fixture",', 1), encoding="utf-8")
        config = load_config(self.root)
        args = SimpleNamespace(password=False, allow_config_password=False)
        with self.assertRaisesRegex(LocalError, "--allow-config-password"):
            _enable_auto_upload(args, config)
