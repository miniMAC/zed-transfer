import json
import tempfile
import unittest
from pathlib import Path

from zed_transfer.config import ConfigError, is_ignored, load_config
from zed_transfer.paths import transfer_paths, validate_remote_path


class ConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / ".vscode" / "sftp.json"
        self.source.parent.mkdir()
        self.fixture = {"host": "example.test", "username": "deploy"}

    def write(self, config):
        self.source.write_text(json.dumps(config), encoding="utf-8")

    def test_reads_jsonc_without_changing_password(self):
        self.source.write_text(
            """{
                // Server
                "host": "EXAMPLE.test",
                "username": "deploy",
                "password": "abc//de/*f*/ghi,}",
                "remotePath": "/www/site/", /* URL root */
            }""",
            encoding="utf-8-sig",
        )
        config = load_config(self.root)
        self.assertEqual(config.host, "example.test")
        self.assertEqual(config.port, 22)
        self.assertEqual(config.remote_path, "/www/site")
        self.assertEqual(config.password, "abc//de/*f*/ghi,}")
        self.assertNotIn("abc//de", repr(config))

    def test_requires_named_server_and_selects_profile(self):
        self.write([
            self.fixture | {"name": "Other"},
            {
                "name": "Main",
                "username": "deploy",
                "remotePath": "/default",
                "profiles": {
                    "stage": {"host": "stage.example.test", "remotePath": "/stage"},
                    "prod": {"host": "prod.example.test"},
                },
                "defaultProfile": "stage",
            },
        ])
        with self.assertRaisesRegex(ConfigError, "--name"):
            load_config(self.root)
        stage = load_config(self.root, name="Main")
        prod = load_config(self.root, name="Main", profile="prod")
        self.assertEqual((stage.host, stage.remote_path), ("stage.example.test", "/stage"))
        self.assertEqual((prod.host, prod.remote_path), ("prod.example.test", "/default"))

    def test_explicit_ftps_and_unsupported_transport_settings(self):
        self.write(self.fixture | {"protocol": "ftp", "secure": True})
        config = load_config(self.root)
        self.assertEqual((config.protocol, config.port), ("ftps", 21))
        for options in (
            {"secure": "control"},
            {"secure": "implicit"},
            {"secureOptions": {"rejectUnauthorized": False}},
            {"hop": {"host": "other.example.test"}},
        ):
            with self.subTest(options=options):
                self.write(self.fixture | {"protocol": "ftp"} | options)
                with self.assertRaises(ConfigError):
                    load_config(self.root)

    def test_rejects_duplicate_keys_and_excludes_values_from_errors(self):
        self.source.write_text('{"password":"example-secret","password":"literal"}', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "duplicata") as caught:
            load_config(self.root)
        self.assertNotIn("example-secret", str(caught.exception))
        self.source.write_text(
            '{"host":"example.test","username":"deploy","ignore":[ /* Empty */ ,]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "JSON non valido"):
            load_config(self.root)

    def test_config_and_local_paths_reject_symlinks_and_traversal(self):
        web = self.root / "web"
        web.mkdir()
        self.write(self.fixture | {"context": "/web", "remotePath": "/public"})
        config = load_config(self.root)
        self.assertEqual(transfer_paths(config, "web/new.php"), (web / "new.php", "/public/new.php"))
        self.assertEqual(config.source, self.source)

        (web / "link.php").symlink_to(self.source)
        (web / "alias").symlink_to(web, target_is_directory=True)
        for file in ("web/link.php", "web/alias/new.php", "web/../secret", ".vscode/sftp.json"):
            with self.subTest(file=file), self.assertRaises(ConfigError):
                transfer_paths(config, file)
        self.source.rename(self.root / "actual.json")
        self.source.symlink_to(self.root / "actual.json")
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_zed_config_is_fallback_only_when_vscode_file_is_absent(self):
        fallback = self.root / ".zed" / "sftp.json"
        fallback.parent.mkdir()
        fallback.write_text(json.dumps(self.fixture), encoding="utf-8")
        self.assertEqual(load_config(self.root).source, fallback)
        self.write(self.fixture)
        self.assertEqual(load_config(self.root).source, self.source)
        self.source.unlink()
        self.source.symlink_to(self.root / "missing")
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_rejects_ssh_option_metacharacters_in_username(self):
        self.write(self.fixture | {"username": "account@example.test"})
        self.assertEqual(load_config(self.root).username, "account@example.test")
        self.write(self.fixture | {"username": 'account" value #foo'})
        with self.assertRaisesRegex(ConfigError, "username"):
            load_config(self.root)

    def test_ssh_key_expansion_and_agent_selection_are_bounded(self):
        self.write(self.fixture | {
            "privateKeyPath": "~/.ssh/id_ed25519",
            "agent": "$SSH_AUTH_SOCK",
        })
        config = load_config(self.root)
        self.assertEqual(config.private_key_path, Path.home() / ".ssh/id_ed25519")
        for key in ("~//etc/passwd", "~/folder/../key", "~/.ssh/key_%h", "${IDENTITY_FILE}"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self.write(self.fixture | {"privateKeyPath": key})
                load_config(self.root)
        self.write(self.fixture | {"agent": "/tmp/custom.sock"})
        with self.assertRaisesRegex(ConfigError, "agent"):
            load_config(self.root)

    def test_ignore_rules_match_directory_ancestors_and_absolute_patterns(self):
        patterns = ("/cache/", "node_modules", "logs/**/*.log", "*.tmp")
        for file in (
            "cache/nested/file",
            "dist/node_modules/file",
            "logs/x.log",
            "logs/2026/11/x.log",
            "dist/assets/a.tmp",
        ):
            with self.subTest(file=file):
                self.assertTrue(is_ignored(tuple(file.split("/")), patterns))
        for file in ("copy/cache/file", "dist/logs/x.log", "node", "cache"):
            with self.subTest(file=file):
                self.assertFalse(is_ignored(tuple(file.split("/")), patterns))
        self.write(self.fixture | {"ignore": ["!foo"]})
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_remote_paths_require_absolute_paths_free_of_traversal_or_controls(self):
        self.assertEqual(validate_remote_path("/path//with space/"), "/path/with space")
        for remote in ("relative", "/var/../root", "/x\r\nQUIT", "/a\\b"):
            with self.subTest(remote=remote), self.assertRaises(ConfigError):
                validate_remote_path(remote)
        self.write(self.fixture | {"remoteTempPath": "/private//staging/"})
        self.assertEqual(load_config(self.root).remote_temp_path, "/private/staging")
        self.write(self.fixture | {"remoteTempPath": "/public/../staging"})
        with self.assertRaisesRegex(ConfigError, "remoteTempPath"):
            load_config(self.root)


if __name__ == "__main__":
    unittest.main()
