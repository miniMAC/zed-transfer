import io
import os
import tempfile
import unittest
from pathlib import Path

from zed_transfer.local import LocalError, existing_file, install_file, project_parent, snapshot_upload


class LocalTransferTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.target = self.root / "target.txt"

    def test_new_download_cannot_overwrite_a_concurrent_file(self):
        with project_parent(self.root, self.target) as parent:
            original = existing_file(parent, self.target.name)
            self.target.write_bytes(b"other process")
            with self.assertRaises(LocalError):
                install_file(parent, self.target.name, io.BytesIO(b"remote"), original)
        self.assertEqual(self.target.read_bytes(), b"other process")
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_download_detects_local_edit_after_confirmation(self):
        self.target.write_bytes(b"local")
        with project_parent(self.root, self.target) as parent:
            original = existing_file(parent, self.target.name)
            self.target.write_bytes(b"local edit")
            with self.assertRaises(LocalError):
                install_file(parent, self.target.name, io.BytesIO(b"remote"), original)
        self.assertEqual(self.target.read_bytes(), b"local edit")

    def test_download_replaces_content_and_keeps_file_mode(self):
        self.target.write_bytes(b"local")
        self.target.chmod(0o640)
        with project_parent(self.root, self.target) as parent:
            original = existing_file(parent, self.target.name)
            install_file(parent, self.target.name, io.BytesIO(b"remote"), original)
        self.assertEqual(self.target.read_bytes(), b"remote")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)

    def test_upload_refuses_symlink_and_hardlink(self):
        original = self.root / "private.txt"
        original.write_bytes(b"secret")
        staged = self.root / "staged.txt"
        with project_parent(self.root, self.target) as parent:
            self.target.symlink_to(original)
            with self.assertRaises(OSError):
                snapshot_upload(parent, self.target.name, staged)
            self.target.unlink()
            os.link(original, self.target)
            with self.assertRaises(LocalError):
                snapshot_upload(parent, self.target.name, staged)
        self.assertFalse(staged.exists())


if __name__ == "__main__":
    unittest.main()
