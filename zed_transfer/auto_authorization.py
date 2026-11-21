"""Keep upload-on-save approvals in the user's private configuration directory."""

import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import stat
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .config import Config, has_controls


class AuthorizationError(RuntimeError):
    """Unavailable or unsafe automatic upload authorization."""


@dataclass(frozen=True)
class Authorization:
    name: str | None
    profile: str | None
    config_path: Path
    allow_config_password: bool
    _binding: str = field(repr=False)


AutoUploadStatus = tuple[Authorization | None, bool]


_STATE = (".config", "zed-transfer", "auto-upload")
_MAX_RECORD = 256 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def _account_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError, RuntimeError):
        raise AuthorizationError("Impossibile trovare la directory personale dell'utente.") from None


def _root(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AuthorizationError("Impossibile aprire la directory del progetto.") from None


def _assert_directory(fd: int, *, private: bool) -> None:
    info = os.fstat(fd)
    unsafe_permissions = 0o077 if private else 0o022
    if info.st_uid != os.getuid() or info.st_mode & unsafe_permissions:
        raise AuthorizationError("La directory delle autorizzazioni ha permessi non sicuri.")


@contextmanager
def _state_directory(root: Path, *, create: bool = False, location: tuple[str, ...] = _STATE):
    home = _account_home()
    if home.joinpath(*location).is_relative_to(root):
        raise AuthorizationError("La directory delle autorizzazioni deve essere fuori dal progetto.")
    with ExitStack() as cleanup:
        try:
            current = os.open(home, _DIR_FLAGS)
            cleanup.callback(os.close, current)
            _assert_directory(current, private=False)
            for index, part in enumerate(location):
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=current)
                current = os.open(part, _DIR_FLAGS, dir_fd=current)
                cleanup.callback(os.close, current)
                _assert_directory(current, private=index != 0)
        except FileNotFoundError:
            if create:
                raise AuthorizationError("Impossibile creare la directory delle autorizzazioni.") from None
            current = None
        except OSError:
            raise AuthorizationError("Impossibile aprire la directory delle autorizzazioni.") from None
        yield current


def _record_name(root: Path) -> str:
    return hashlib.sha256(os.fsencode(root)).hexdigest() + ".json"


def _binding(config: Config, allow_config_password: bool) -> str:
    data = (
        1,
        str(config.root),
        config.workspace_identity,
        str(config.source),
        config.source_fingerprint,
        config.name,
        config.profile,
        str(config.context),
        config.protocol,
        config.host,
        config.port,
        config.username,
        config.remote_path,
        config.remote_temp_path,
        str(config.private_key_path) if config.private_key_path is not None else None,
        config.upload_on_save,
        allow_config_password,
    )
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode("ascii")).hexdigest()


def _parse_record(raw: bytes) -> AutoUploadStatus:
    try:
        record = json.loads(raw)
        if type(record) is not dict or type(record.get("version")) is not int or record["version"] != 1:
            raise ValueError
        if set(record) == {"version", "disabled"} and record["disabled"] is True:
            return None, True
        if set(record) != {
            "version", "name", "profile", "config_path", "allow_config_password", "binding"
        }:
            raise ValueError
        name, profile = record["name"], record["profile"]
        if any(value is not None and (not isinstance(value, str) or has_controls(value))
               for value in (name, profile)):
            raise ValueError
        path = record["config_path"]
        if not isinstance(path, str) or has_controls(path) or "\\" in path:
            raise ValueError
        config_path = Path(path)
        if config_path.is_absolute() or not config_path.parts or ".." in config_path.parts:
            raise ValueError
        use_password, binding = record["allow_config_password"], record["binding"]
        if type(use_password) is not bool or not isinstance(binding, str) or not _HEX_DIGEST.fullmatch(binding):
            raise ValueError
    except (ValueError, RecursionError):
        raise AuthorizationError("Autorizzazione non valida. Revocala e confermala di nuovo.") from None
    return Authorization(name, profile, config_path, use_password, binding), False


def _assert_private_file(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise AuthorizationError("Il file di autorizzazione ha permessi o tipo non sicuri.")


def _read_status(directory: int, root: Path) -> AutoUploadStatus:
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(_record_name(root), flags, dir_fd=directory), "rb") as record:
            _assert_private_file(record.fileno())
            raw = record.read(_MAX_RECORD + 1)
    except FileNotFoundError:
        return None, False
    except OSError:
        raise AuthorizationError("Impossibile leggere il file di autorizzazione.") from None
    if len(raw) > _MAX_RECORD:
        raise AuthorizationError("Il file di autorizzazione supera la dimensione massima.")
    return _parse_record(raw)


def get_auto_upload_status(root: Path) -> AutoUploadStatus:
    """Read an approval and whether the user explicitly disabled automatic upload."""
    root = _root(root)
    with _state_directory(root) as directory:
        return _read_status(directory, root) if directory is not None else (None, False)


def get_authorization(root: Path) -> Authorization | None:
    """Read the active approval for a workspace, if it exists."""
    approval, _ = get_auto_upload_status(root)
    return approval


@contextmanager
def _update_lock(directory: int):
    with ExitStack() as cleanup:
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            lock = os.open(".lock", flags, 0o600, dir_fd=directory)
            cleanup.callback(os.close, lock)
            _assert_private_file(lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            raise AuthorizationError("Impossibile aggiornare l'autorizzazione.") from None
        yield


def _write_record(
    root: Path, data: dict[str, object], expected_status: AutoUploadStatus | None
) -> AutoUploadStatus:
    raw = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
    if len(raw) > _MAX_RECORD:
        raise AuthorizationError("Il file di autorizzazione supera la dimensione massima.")
    with _state_directory(root, create=True) as directory, _update_lock(directory):
        try:
            current = _read_status(directory, root)
        except AuthorizationError:
            if expected_status is not None:
                raise
            current = (None, False)
        if expected_status is not None and current != expected_status:
            raise AuthorizationError("Lo stato dell'upload automatico è già cambiato.")

        temporary = f".{uuid4().hex}.tmp"
        written = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600, dir_fd=directory)
            written = True
            with os.fdopen(fd, "wb") as record:
                record.write(raw)
                record.flush()
                os.fsync(record.fileno())
            os.replace(temporary, _record_name(root), src_dir_fd=directory, dst_dir_fd=directory)
            written = False
        except OSError:
            raise AuthorizationError("Impossibile salvare l'autorizzazione.") from None
        finally:
            if written:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=directory)
    return current


def authorize(
    config: Config, *, allow_config_password: bool, expected_status: AutoUploadStatus | None = None
) -> Authorization:
    """Persist an approval after a user has confirmed the exact target."""
    config_path = config.source.relative_to(config.root)
    binding = _binding(config, allow_config_password)
    data = {
        "version": 1,
        "name": config.name,
        "profile": config.profile,
        "config_path": config_path.as_posix(),
        "allow_config_password": allow_config_password,
        "binding": binding,
    }
    _write_record(config.root, data, expected_status)
    return Authorization(config.name, config.profile, config_path, allow_config_password, binding)


def verify_authorization(approval: Authorization, config: Config) -> bool:
    """Match both the resolved destination and the exact configuration bytes."""
    return (
        approval.config_path == config.source.relative_to(config.root)
        and approval.name == config.name
        and approval.profile == config.profile
        and hmac.compare_digest(approval._binding, _binding(config, approval.allow_config_password))
    )


def revoke(root: Path, *, expected_status: AutoUploadStatus | None = None) -> bool:
    """Disable automatic upload for the workspace until a new explicit approval."""
    active, _ = _write_record(_root(root), {"version": 1, "disabled": True}, expected_status)
    return active is not None
