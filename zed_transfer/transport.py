"""SFTP through OpenSSH and FTP/FTPS through the Python standard library."""

from __future__ import annotations

import errno
import ftplib
import math
import os
import posixpath
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .paths import validate_remote_path

if TYPE_CHECKING:
    from .config import Config


class TransportError(Exception):
    """A transport failure safe to report without printing server responses."""


def _staging_path(remote: str, directory: str | None) -> str:
    parent = directory if directory is not None else posixpath.dirname(remote)
    return posixpath.join(parent, f".zed-transfer-{uuid4().hex}.tmp")


def _quote_sftp_path(value: str | Path) -> str:
    path = str(value)
    if not path.startswith("/") or any(ord(char) < 32 or char == "\\" for char in path):
        raise TransportError("SFTP richiede percorsi assoluti senza barre inverse o caratteri di controllo.")
    # OpenSSH quotes glob metacharacters inside quotes. A literal quote still needs escaping.
    return '"' + path.replace('"', '\\"') + '"'


@contextmanager
def _askpass(password: str):
    secret = password.encode("utf-8")
    if len(secret) > 1022 or any(char in secret for char in (b"\0", b"\r", b"\n")):
        raise TransportError("La password SFTP deve stare su una riga di massimo 1022 byte UTF-8.")

    # The helper is executable by this user only and holds no credentials. The
    # private FIFO delivers the password once, after OpenSSH requests it.
    with tempfile.TemporaryDirectory(prefix="zt-") as directory:
        helper = Path(directory) / "ask"
        fifo = helper.with_suffix(".fifo")
        os.mkfifo(fifo, 0o600)
        helper.write_text('#!/bin/sh\nexec /bin/cat "$0.fifo"\n')
        helper.chmod(0o700)
        finished = threading.Event()

        def deliver_password():
            while not finished.is_set():
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                except OSError as error:
                    if error.errno != errno.ENXIO:
                        return
                    finished.wait(0.02)
                    continue
                with suppress(OSError), os.fdopen(fd, "wb") as writer:
                    os.set_blocking(writer.fileno(), True)
                    writer.write(secret + b"\n")
                return

        sender = threading.Thread(target=deliver_password, daemon=True)
        sender.start()
        try:
            yield {
                **os.environ,
                "SSH_ASKPASS": os.fspath(helper),
                "SSH_ASKPASS_REQUIRE": "force",
            }
        finally:
            finished.set()
            sender.join()


class _SFTP:
    def __init__(self, config: Config, password: str | None):
        binary = shutil.which("sftp", path=os.defpath)
        if binary is None:
            raise TransportError("Il comando sftp di OpenSSH manca nel percorso di sistema.")

        self._password = password
        self._remote_temp_path = config.remote_temp_path
        self._args = [
            binary, "-F", "/dev/null", "-P", str(config.port),
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "ClearAllForwardings=yes", "-o", f"User={config.username}",
            "-o", f"ConnectTimeout={math.ceil(config.timeout)}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        ]
        if password is None:
            self._args += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey"]
            if config.private_key_path is not None:
                key = str(config.private_key_path)
                if not key.startswith("/") or any(char in key for char in ("%", "${", "\n", "\r", "\0")):
                    raise TransportError("Il percorso della chiave SSH deve essere assoluto e senza token OpenSSH.")
                self._args += ["-o", "IdentitiesOnly=yes", "-i", key]
        else:
            self._args += [
                "-o", "BatchMode=no", "-o", "PreferredAuthentications=password",
                "-o", "NumberOfPasswordPrompts=1",
            ]

        host = f"[{config.host}]" if ":" in config.host else config.host
        self._args += ["-b", "-", "--", host]

    def _run(self, *commands: str) -> str:
        lines = [f"@{command}".encode("utf-8") for command in commands]
        if any(len(line) > 4000 for line in lines):
            raise TransportError("I percorsi del comando SFTP sono troppo lunghi.")
        payload = b"\n".join(lines) + b"\n"

        def run(env=None):
            with subprocess.Popen(
                self._args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            ) as process:
                try:
                    stdout, stderr = process.communicate(payload, timeout=600)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    raise
                return process.returncode, stdout, stderr

        try:
            if self._password is None:
                status, stdout, stderr = run()
            else:
                with _askpass(self._password) as env:
                    status, stdout, stderr = run(env)
        except subprocess.TimeoutExpired:
            raise TransportError("L'operazione SFTP ha superato il limite di 10 minuti.") from None
        except OSError:
            raise TransportError("Impossibile avviare OpenSSH o la procedura locale per la password.") from None

        if status:
            errors = stderr.lower()
            if b"host key verification failed" in errors or b"host identification has changed" in errors:
                raise TransportError("Verifica della chiave SSH fallita. Controlla l'impronta del server in known_hosts.")
            if b"permission denied (" in errors:
                raise TransportError("Autenticazione SFTP fallita. Controlla utente e credenziali SSH.")
            if b"connection refused" in errors or b"connection timed out" in errors:
                raise TransportError("Connessione SFTP fallita. Controlla host e porta.")
            raise TransportError("Operazione SFTP fallita. Controlla il percorso remoto, i permessi e i log del server.")
        return stdout.decode("utf-8", errors="replace")

    def list(self, remote: str) -> list[str]:
        remote = validate_remote_path(remote)
        output = self._run(f"cd {_quote_sftp_path(remote)}", "ls -1a")
        return [entry for entry in output.splitlines() if entry not in {"", ".", ".."}]

    def upload(self, local: Path, remote: str) -> None:
        remote = validate_remote_path(remote)
        source = _quote_sftp_path(local)
        target = _quote_sftp_path(remote)
        staging = _quote_sftp_path(_staging_path(remote, self._remote_temp_path))
        try:
            self._run(f"put {source} {staging}", f"rename {staging} {target}")
        except TransportError:
            with suppress(TransportError):
                self._run(f"-rm {staging}")
            raise

    def download(self, remote: str, local: Path) -> None:
        remote = validate_remote_path(remote)
        self._run(f"get {_quote_sftp_path(remote)} {_quote_sftp_path(local)}")


@contextmanager
def _ftp_errors():
    try:
        yield
    except ssl.SSLCertVerificationError:
        raise TransportError("Verifica del certificato FTPS fallita. Controlla il certificato e il nome host.") from None
    except ssl.SSLError:
        raise TransportError("Handshake FTPS fallito. Controlla la configurazione TLS del server.") from None
    except ftplib.all_errors as error:
        status = str(error).split(" ", 1)[0]
        status = f" (codice FTP {status})" if len(status) == 3 and status.isascii() and status.isdigit() else ""
        raise TransportError(f"Operazione FTP fallita{status}. Controlla connessione, credenziali e percorso remoto.") from None


class _FTPSClient(ftplib.FTP_TLS):
    def ntransfercmd(self, cmd: str, rest: str | None = None) -> tuple[socket.socket, int | None]:
        control = self.sock
        if not isinstance(control, ssl.SSLSocket):
            raise TransportError("La connessione di controllo FTPS deve essere protetta da TLS.")
        connection, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        try:
            return self.context.wrap_socket(
                connection, server_hostname=self.host, session=control.session,
            ), size
        except OSError:
            connection.close()
            raise


class _FTP:
    def __init__(self, client: ftplib.FTP, remote_temp_path: str | None):
        self._client = client
        self._remote_temp_path = remote_temp_path

    def list(self, remote: str) -> list[str]:
        remote = validate_remote_path(remote)
        with _ftp_errors():
            self._client.cwd(remote)
            try:
                entries = self._client.mlsd()
                return [name for name, facts in entries if facts.get("type") not in {"cdir", "pdir"}]
            except ftplib.error_perm as error:
                if not str(error).startswith(("500", "501", "502", "504")):
                    raise
                return [entry for entry in self._client.nlst() if entry not in {".", ".."}]

    def upload(self, local: Path, remote: str) -> None:
        remote = validate_remote_path(remote)
        staging = _staging_path(remote, self._remote_temp_path)
        with _ftp_errors(), local.open("rb") as source:
            try:
                self._client.storbinary(f"STOR {staging}", source, blocksize=65536)
                self._client.rename(staging, remote)
            except ftplib.all_errors:
                with suppress(*ftplib.all_errors):
                    self._client.delete(staging)
                raise

    def download(self, remote: str, local: Path) -> None:
        remote = validate_remote_path(remote)
        with _ftp_errors(), local.open("wb") as target:
            self._client.retrbinary(f"RETR {remote}", target.write, blocksize=65536)


@contextmanager
def open_transport(config: Config, password: str | None = None, *, allow_insecure_ftp: bool = False):
    """Open a transport and close the FTP control connection on exit."""
    if config.protocol == "sftp":
        yield _SFTP(config, password)
        return

    if config.protocol == "ftp" and not allow_insecure_ftp:
        raise TransportError("FTP in chiaro richiede --allow-insecure-ftp. Usa SFTP o FTPS se disponibili.")
    if config.protocol == "ftps":
        client = _FTPSClient(context=ssl.create_default_context())
    elif config.protocol == "ftp":
        client = ftplib.FTP()
    else:
        raise TransportError("Protocollo di trasferimento non supportato.")

    try:
        with _ftp_errors():
            # Keep passive data connections on the authenticated control peer.
            client.trust_server_pasv_ipv4_address = False
            client.connect(config.host, config.port, timeout=config.timeout)
            client.login(config.username, password or "")
            if isinstance(client, ftplib.FTP_TLS):
                client.prot_p()
        yield _FTP(client, config.remote_temp_path)
    finally:
        client.close()
