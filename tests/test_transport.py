import ftplib
import ssl
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zed_transfer.transport import (
    _FTP, _FTPSClient, _SFTP, _askpass, _ftp_errors, _sftp_error, TransportError, open_transport,
)


class TransportTest(unittest.TestCase):
    def test_ssh_always_verifies_host_and_only_enables_requested_auth(self):
        config = SimpleNamespace(host="example.test", port=22, username="deploy",
                                 timeout=1.5, private_key_path=None, remote_temp_path=None)
        with patch("zed_transfer.transport.shutil.which", return_value="/usr/bin/sftp"):
            key = _SFTP(config, None)
            password = _SFTP(config, "fixture only")
        for client in (key, password):
            self.assertIn("StrictHostKeyChecking=yes", client._args)
            self.assertIn("ConnectTimeout=2", client._args)
            self.assertEqual(client._args[-2:], ["--", "example.test"])
        self.assertIn("PreferredAuthentications=publickey", key._args)
        self.assertIn("BatchMode=no", password._args)
        self.assertIn("PreferredAuthentications=password", password._args)
        self.assertNotIn("fixture only", password._args)

    def test_password_helper_uses_a_private_fifo(self):
        fixture = 'fixture with spàces and "quotes"'
        with _askpass(fixture) as env:
            helper = Path(env["SSH_ASKPASS"])
            fifo = helper.with_suffix(".fifo")
            self.assertFalse(any(fixture in value for value in env.values()))
            self.assertEqual(stat.S_IMODE(helper.parent.stat().st_mode), 0o700)
            self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode))
            result = subprocess.check_output([helper], env=env, timeout=3)
            self.assertEqual(result, (fixture + "\n").encode())
        self.assertFalse(helper.exists())

    def test_ftp_rename_failure_only_deletes_the_staged_file(self):
        client = Mock(spec=ftplib.FTP)
        client.rename.side_effect = ftplib.error_perm("550 rename refused")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.write_bytes(b"fixture")
            with self.assertRaisesRegex(TransportError, "rinomina finale") as caught:
                _FTP(client, "/staging").upload(source, "/public/target")
        self.assertEqual(caught.exception.code, 550)
        stage, target = client.rename.call_args.args
        self.assertTrue(stage.startswith("/staging/.zed-transfer-"))
        self.assertEqual(target, "/public/target")
        client.delete.assert_called_once_with(stage)

    def test_cleartext_ftp_requires_explicit_permission(self):
        with self.assertRaisesRegex(TransportError, "--allow-insecure-ftp"):
            with open_transport(SimpleNamespace(protocol="ftp")):
                self.fail("cleartext FTP was opened")

    def test_ftps_data_channel_uses_the_authenticated_control_session(self):
        context = Mock(spec=ssl.SSLContext)
        client = _FTPSClient(context=context)
        client.host = "example.test"
        client.sock = Mock(spec=ssl.SSLSocket)
        data_socket = Mock()
        with patch.object(ftplib.FTP, "ntransfercmd", return_value=(data_socket, 10)):
            wrapped, size = client.ntransfercmd("RETR /file")
        context.wrap_socket.assert_called_once_with(
            data_socket, server_hostname="example.test", session=client.sock.session,
        )
        self.assertEqual((wrapped, size), (context.wrap_socket.return_value, 10))

    def test_sftp_reports_actionable_errors_without_remote_diagnostics(self):
        secret_path = b'"/private/untrusted\x1b[31m-fixture"'
        for diagnostic, expected in (
            (b"dest open " + secret_path + b": No such file or directory", "cartella per l'upload"),
            (b"dest open " + secret_path + b": Permission denied", "Permesso negato"),
            (b"remote rename " + secret_path + b' to "/remote/target": Failure', "rinomina finale"),
            (b"write remote " + secret_path + b": Disk quota exceeded", "quota esauriti"),
            (b"deploy@example.test: Permission denied (publickey,password).", "Autenticazione SFTP"),
            (b"ssh: connect to host example.test port 22: Connection timed out", "Connessione SFTP scaduta"),
        ):
            with self.subTest(expected=expected):
                error = _sftp_error(1, diagnostic)
                self.assertIn(expected, str(error))
                self.assertNotIn("untrusted", str(error))
                self.assertEqual(error.code, 1)

    def test_ftp_status_codes_and_tls_errors_do_not_include_raw_messages(self):
        for exception, expected, code in (
            (ftplib.error_perm("530 opaque-credential"), "Autenticazione", 530),
            (ftplib.error_temp("425-opaque-credential"), "canale dati", 425),
            (ftplib.error_perm("552 opaque-credential"), "Quota", 552),
            (TimeoutError("opaque-credential"), "scaduta", None),
            (ssl.SSLCertVerificationError("opaque-credential"), "certificato", None),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(TransportError, expected) as caught:
                    with _ftp_errors("upload"):
                        raise exception
                self.assertNotIn("opaque-credential", str(caught.exception))
                self.assertEqual(caught.exception.code, code)
