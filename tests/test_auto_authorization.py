import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from zed_transfer.auto_authorization import (
    AuthorizationError,
    authorize,
    get_authorization,
    get_auto_upload_status,
    revoke,
    verify_authorization,
)
from zed_transfer.config import ConfigError, load_config


class AutoAuthorizationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        account_home = patch("zed_transfer.auto_authorization._account_home", return_value=self.home)
        account_home.start()
        self.addCleanup(account_home.stop)
        self.root = base / "project"
        self.source = self.root / ".vscode" / "sftp.json"
        self.source.parent.mkdir(parents=True)
        self.state = self.home / ".config" / "zed-transfer" / "auto-upload"
        self.fixture = {
            "name": "test-server",
            "host": "private.example.test",
            "username": "deploy-account",
            "password": "example-password-for-test",
            "uploadOnSave": True,
            "profiles": {"staging": {"remotePath": "/staging"}, "production": {}},
            "defaultProfile": "staging",
        }
        self.source.write_text(json.dumps(self.fixture), encoding="utf-8")

    def test_approval_roundtrip_keeps_credentials_out_of_state(self):
        self.assertIsNone(get_authorization(self.root))
        config = load_config(self.root)
        persisted = authorize(config, allow_config_password=True)
        approval = get_authorization(self.root)
        self.assertEqual(persisted, approval)
        self.assertEqual(approval.name, "test-server")
        self.assertEqual(approval.profile, "staging")
        self.assertEqual(approval.config_path, Path(".vscode/sftp.json"))
        self.assertTrue(approval.allow_config_password)
        self.assertTrue(verify_authorization(approval, config))
        self.assertFalse(verify_authorization(approval, load_config(self.root, profile="production")))
        record = next(self.state.glob("*.json"))
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        stored = record.read_text(encoding="utf-8")
        for key in ("host", "username", "password"):
            self.assertNotIn(self.fixture[key], stored)

    def test_raw_change_in_approved_config_rejects_previous_approval(self):
        config = load_config(self.root)
        approval = authorize(config, allow_config_password=False)
        original = self.source.read_text(encoding="utf-8")
        self.source.write_text("// comment changed\n" + original, encoding="utf-8")
        changed = load_config(self.root)
        self.assertFalse(verify_authorization(approval, changed))
        self.assertTrue(revoke(self.root))
        self.assertIsNone(get_authorization(self.root))
        self.assertEqual(get_auto_upload_status(self.root), (None, True))
        self.assertFalse(revoke(self.root))

    def test_replacing_project_directory_rejects_previous_approval(self):
        approval = authorize(load_config(self.root), allow_config_password=False)
        original = self.source.read_text(encoding="utf-8")
        self.root.rename(self.root.with_name("old-project"))
        self.source.parent.mkdir(parents=True)
        self.source.write_text(original, encoding="utf-8")
        self.assertEqual(get_authorization(self.root), approval)
        self.assertFalse(verify_authorization(approval, load_config(self.root)))

    def test_authorization_directories_and_files_cannot_be_symlinks(self):
        config = load_config(self.root)
        self.state.parent.mkdir(parents=True, mode=0o700)
        outside = self.home.parent / "outside"
        outside.mkdir()
        self.state.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AuthorizationError):
            authorize(config, allow_config_password=False)
        self.assertFalse(any(outside.iterdir()))
        self.state.unlink()
        authorize(config, allow_config_password=False)
        path = next(self.state.glob("*.json"))
        original = path.read_bytes()
        shadow = outside / "shadow.json"
        shadow.write_bytes(original)
        path.unlink()
        path.symlink_to(shadow)
        with self.assertRaises(AuthorizationError):
            get_authorization(self.root)
        self.assertFalse(revoke(self.root))
        self.assertEqual(get_auto_upload_status(self.root), (None, True))
        self.assertEqual(shadow.read_bytes(), original)

    def test_group_readable_state_is_rejected(self):
        authorize(load_config(self.root), allow_config_password=False)
        path = next(self.state.glob("*.json"))
        path.chmod(0o644)
        with self.assertRaises(AuthorizationError):
            get_authorization(self.root)

    def test_symlinked_workspace_config_stays_rejected(self):
        approval = authorize(load_config(self.root), allow_config_password=False)
        actual = self.root / "actual.json"
        self.source.rename(actual)
        self.source.symlink_to(actual)
        with self.assertRaises(ConfigError):
            load_config(self.root, path=approval.config_path)

    def test_state_location_must_be_outside_workspace(self):
        with self.assertRaisesRegex(AuthorizationError, "fuori dal progetto"):
            get_authorization(self.home)

    def test_disable_even_without_approval_bypasses_pending_prompt_until_explicit_enable(self):
        config = load_config(self.root)
        pending = get_auto_upload_status(self.root)
        self.assertEqual(pending, (None, False))
        self.assertFalse(revoke(self.root))
        self.assertEqual(get_auto_upload_status(self.root), (None, True))
        with self.assertRaisesRegex(AuthorizationError, "già cambiato"):
            authorize(config, allow_config_password=False, expected_status=pending)
        self.assertEqual(get_auto_upload_status(self.root), (None, True))
        active = authorize(config, allow_config_password=False)
        self.assertEqual(get_auto_upload_status(self.root), (active, False))

    def test_refresh_of_stale_approval_respects_new_status(self):
        initial = authorize(load_config(self.root), allow_config_password=False)
        expected = get_auto_upload_status(self.root)
        self.assertEqual(expected, (initial, False))
        self.source.write_text(json.dumps(self.fixture | {"port": 2222}), encoding="utf-8")
        updated = load_config(self.root)
        active = authorize(updated, allow_config_password=False, expected_status=expected)
        self.assertTrue(verify_authorization(active, updated))
        with self.assertRaisesRegex(AuthorizationError, "già cambiato"):
            revoke(self.root, expected_status=expected)
        self.assertEqual(get_auto_upload_status(self.root), (active, False))

    def test_only_one_concurrent_answer_can_publish_against_same_status(self):
        config = load_config(self.root)
        expected = get_auto_upload_status(self.root)
        barrier = Barrier(2)

        def approve(use_password: bool) -> bool:
            barrier.wait(timeout=5)
            try:
                authorize(config, allow_config_password=use_password, expected_status=expected)
            except AuthorizationError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(approve, (True, False))), [False, True])
        self.assertIsNotNone(get_authorization(self.root))


if __name__ == "__main__":
    unittest.main()
