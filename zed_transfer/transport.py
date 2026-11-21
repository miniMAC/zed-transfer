"""SFTP through OpenSSH and FTP/FTPS through the Python standard library."""

from __future__ import annotations

import errno
import ftplib
import math
import os
import posixpath
import re
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

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def _sftp_error(status: int, stderr: bytes) -> TransportError:
    diagnostics = stderr.lower()
    lines = diagnostics.splitlines()
    missing = any(line.endswith((b": no such file or directory", b" not found.")) for line in lines)
    if b"host key verification failed" in diagnostics or b"host identification has changed" in diagnostics:
        message = "Verifica della chiave SSH fallita. Controlla l'impronta del server in known_hosts."
    elif re.search(rb"permission denied \([a-z0-9@.,_-]+\)\.?\r?$", diagnostics, re.MULTILINE):
        message = "Autenticazione SFTP fallita. Controlla utente e credenziali SSH."
    elif any(line.endswith((b"no space left on device", b"no space on filesystem", b"quota exceeded")) for line in lines):
        message = "Spazio o quota esauriti sul server SFTP. Libera spazio o verifica la quota dell'account."
    elif any(line.startswith(b"remote rename ") for line in lines):
        if missing:
            message = "La cartella di destinazione SFTP è assente. Verifica remotePath e le sottocartelle."
        else:
            message = ("Il server SFTP ha rifiutato la rinomina finale. Verifica i permessi sulla destinazione "
                       "e che remoteTempPath sia sullo stesso filesystem.")
    elif missing and any(line.startswith(b"dest open ") for line in lines):
        message = ("La cartella per l'upload SFTP è assente. Verifica remotePath, le sottocartelle "
                   "e remoteTempPath se impostata.")
    elif missing:
        message = "Il percorso remoto SFTP è assente. Verifica remotePath e il nome del file."
    elif any(line.endswith(b": permission denied") for line in lines):
        message = "Permesso negato sul server SFTP. Verifica i permessi dell'account sulla cartella remota."
    elif b"could not resolve hostname" in diagnostics:
        message = "Nome del server SFTP non risolto. Verifica host e DNS."
    elif b"timed out" in diagnostics:
        message = "Connessione SFTP scaduta. Verifica host, porta e raggiungibilità del server."
    elif any(reason in diagnostics for reason in (b"connection refused", b"no route to host", b"network is unreachable")):
        message = "Connessione SFTP fallita. Controlla host, porta e firewall."
    elif any(reason in diagnostics for reason in (b"connection closed", b"connection reset", b"broken pipe")):
        message = "Connessione SFTP interrotta. Verifica la rete e che il servizio SFTP sia attivo."
    else:
        message = "Operazione SFTP fallita. Controlla il percorso remoto, i permessi e i log del server."
    return TransportError(message, code=status)


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
            raise _sftp_error(status, stderr)
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


_FTP_REASONS = {
    421: "Servizio FTP/FTPS non disponibile. Verifica lo stato del server o riprova più tardi.",
    425: "Il server FTP/FTPS non ha aperto il canale dati. Verifica firewall e porte passive.",
    426: "Il trasferimento FTP/FTPS si è interrotto. Verifica la rete e riprova.",
    430: "Autenticazione FTP/FTPS rifiutata. Verifica utente e password.",
    451: "Il server FTP/FTPS ha interrotto l'operazione per un errore interno. Controlla i log del server.",
    452: "Spazio insufficiente sul server FTP/FTPS. Verifica la quota dell'account.",
    530: "Autenticazione FTP/FTPS rifiutata. Verifica utente e password.",
    532: "L'account FTP/FTPS non è autorizzato a scrivere. Verifica i permessi sul server.",
    534: "Il server FTP/FTPS ha rifiutato il metodo di autenticazione. Verifica protocollo e porta.",
    536: "Il server FTPS non consente la protezione richiesta per il canale dati.",
    550: "File o cartella FTP/FTPS non accessibili. Verifica percorso e permessi dell'account.",
    552: "Quota o spazio esauriti sul server FTP/FTPS. Libera spazio o verifica la quota dell'account.",
    553: "Il server FTP/FTPS ha rifiutato il nome del file. Verifica nome e permessi della cartella.",
}
_FTP_CODE = re.compile(r"([1-5][0-9]{2})(?=[ \t\r\n-]|$)")


def _ftp_reply_error(error: ftplib.Error, operation: str) -> TransportError:
    reply = str(error)
    match = _FTP_CODE.match(reply)
    code = int(match[1]) if match else None
    if operation == "rename" and code in {450, 550, 553}:
        message = ("Il server FTP/FTPS ha rifiutato la rinomina finale. Verifica i permessi sulla destinazione "
                   "e che remoteTempPath sia sullo stesso filesystem.")
    elif code == 550:
        reason = reply[3:].lower()
        if "quota exceeded" in reason or "no space left" in reason:
            message = _FTP_REASONS[552]
        elif "permission denied" in reason or "access denied" in reason:
            message = "Permesso negato sul server FTP/FTPS. Verifica i permessi dell'account sulla cartella remota."
        elif "no such file or directory" in reason and operation == "upload":
            message = ("La cartella per l'upload FTP/FTPS è assente. Verifica remotePath, le sottocartelle "
                       "e remoteTempPath se impostata.")
        else:
            message = _FTP_REASONS[550]
    elif operation == "ftps_login" and code in {500, 502, 504}:
        message = "Il server non ha accettato FTPS esplicito. Verifica protocollo e porta."
    elif operation == "ftps_protection" and code not in _FTP_REASONS:
        message = "Il server non ha attivato la protezione FTPS del canale dati. Verifica la configurazione TLS."
    else:
        message = _FTP_REASONS.get(code, "Operazione FTP/FTPS fallita. Verifica connessione, percorso remoto e log del server.")
    if code is not None:
        message += f" (codice FTP {code})"
    return TransportError(message, code=code)


@contextmanager
def _ftp_errors(operation: str):
    try:
        yield
    except ssl.SSLCertVerificationError:
        raise TransportError("Verifica del certificato FTPS fallita. Controlla nome host, scadenza e catena del certificato.") from None
    except ssl.SSLError:
        raise TransportError("Connessione TLS FTPS fallita. Controlla protocollo, porta e configurazione TLS del server.") from None
    except TimeoutError:
        raise TransportError("Connessione FTP/FTPS scaduta. Verifica rete, host e porta.") from None
    except socket.gaierror:
        raise TransportError("Nome del server FTP/FTPS non risolto. Verifica host e DNS.") from None
    except ConnectionRefusedError:
        raise TransportError("Connessione FTP/FTPS rifiutata. Verifica host, porta e firewall.") from None
    except (ConnectionError, EOFError):
        raise TransportError("Connessione FTP/FTPS interrotta. Verifica la rete e che il servizio sia attivo.") from None
    except ftplib.Error as error:
        raise _ftp_reply_error(error, operation) from None
    except OSError:
        raise TransportError("Operazione FTP/FTPS non riuscita per un errore di I/O. Verifica rete e file locale.") from None


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
        with _ftp_errors("list"):
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
        with local.open("rb") as source:
            try:
                with _ftp_errors("upload"):
                    self._client.storbinary(f"STOR {staging}", source, blocksize=65536)
                with _ftp_errors("rename"):
                    self._client.rename(staging, remote)
            except TransportError:
                with suppress(*ftplib.all_errors):
                    self._client.delete(staging)
                raise

    def download(self, remote: str, local: Path) -> None:
        remote = validate_remote_path(remote)
        with local.open("wb") as target, _ftp_errors("download"):
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
        # Keep passive data connections on the authenticated control peer.
        client.trust_server_pasv_ipv4_address = False
        with _ftp_errors("connect"):
            client.connect(config.host, config.port, timeout=config.timeout)
        with _ftp_errors("ftps_login" if config.protocol == "ftps" else "login"):
            client.login(config.username, password or "")
        if isinstance(client, ftplib.FTP_TLS):
            with _ftp_errors("ftps_protection"):
                client.prot_p()
        yield _FTP(client, config.remote_temp_path)
    finally:
        client.close()
