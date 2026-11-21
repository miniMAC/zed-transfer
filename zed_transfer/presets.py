"""Create project configurations from built-in or private reusable presets."""

import io
import json
import os
import stat
from contextlib import ExitStack, suppress
from pathlib import Path

from .auto_authorization import (
    AuthorizationError,
    _account_home,
    _assert_private_file,
    _state_directory,
)
from .config import (
    ConfigError,
    _MAX_CONFIG,
    _REJECTED,
    _host,
    _ignore_patterns,
    _key_path,
    _load_source,
    _parse_jsonc,
    _protocol,
    _SSH_USERNAME,
    _text,
    _workspace_path,
    validate_remote_path,
)
from .local import LocalError, _directory_flags, existing_file, install_file, project_parent


class PresetError(ValueError):
    """Unavailable or invalid configuration preset."""


_LOCATION = (".config", "zed-transfer", "presets")
_FILENAME = "sftp.json"
_BUILTINS = {
    "sftp-key": {"protocol": "sftp", "port": 22, "agent": "$SSH_AUTH_SOCK"},
    "sftp-password": {"protocol": "sftp", "port": 22, "password": True},
    "ftps": {"protocol": "ftp", "port": 21, "secure": True, "password": True},
}
_TEXT_FIELDS = {"name", "protocol", "username", "agent", "defaultProfile"}
_PATH_FIELDS = {"remotePath", "remoteTempPath"}


def _project_root(root: Path) -> Path:
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PresetError("Impossibile trovare la directory del progetto.") from None
    if not root.is_dir():
        raise PresetError("Il progetto deve essere una directory.")
    return root


def _encode(document: object) -> bytes:
    result = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(result) > _MAX_CONFIG:
        raise PresetError("Il modello supera la dimensione massima di 128 KiB.")
    return result


def _clean_options(data: dict[str, object], root: Path) -> dict[str, object]:
    clean: dict[str, object] = {"uploadOnSave": False}
    for key, value in data.items():
        if key in _REJECTED and key != "passphrase" and value is not None and value is not False:
            raise PresetError(f"L'opzione {key} non è supportata nei modelli.")
        if key in _TEXT_FIELDS and not (key in {"agent", "name"} and value is None):
            clean[key] = _text(value, key)
        elif key in {"agent", "name", "remoteTempPath", "privateKeyPath"} and value is None:
            clean[key] = None
        elif key in _PATH_FIELDS:
            clean[key] = validate_remote_path(value, field=key)
        elif key == "host":
            clean[key] = _host(value)
        elif key == "context":
            context = _text(value, "context")
            _workspace_path(root, context.lstrip("/"), label="context")
            clean[key] = context
        elif key == "privateKeyPath":
            _key_path(value, root)
            clean[key] = value
        elif key == "ignore":
            clean[key] = _ignore_patterns(value)
        elif key == "secure":
            if type(value) is not bool:
                raise PresetError("secure deve essere true oppure false.")
            clean[key] = value
        elif key == "password":
            if value is not None and not isinstance(value, (str, bool)):
                raise PresetError("password deve essere una stringa o un valore booleano.")
            clean[key] = bool(value) if value is not None else None
        elif key == "port":
            if type(value) is not int or not 1 <= value <= 65535:
                raise PresetError("port deve essere un intero da 1 a 65535.")
            clean[key] = value
        elif key == "connectTimeout":
            if type(value) not in {int, float} or not 1 <= value <= 300_000:
                raise PresetError("connectTimeout deve essere da 1 a 300000 millisecondi.")
            clean[key] = value
    return clean


def _validate_connection(data: dict[str, object]) -> None:
    protocol = _protocol(data)
    _host(data.get("host"))
    username = _text(data.get("username"), "username")
    if len(username.encode("utf-8")) > 255 or (
        protocol == "sftp" and _SSH_USERNAME.fullmatch(username) is None
    ):
        raise PresetError("Il nome utente del modello non è valido per il protocollo scelto.")
    if data.get("agent") is not None and (protocol != "sftp" or data["agent"] != "$SSH_AUTH_SOCK"):
        raise PresetError("agent accetta solo $SSH_AUTH_SOCK con SFTP.")
    if data.get("privateKeyPath") is not None and protocol != "sftp":
        raise PresetError("privateKeyPath richiede SFTP.")


def _clean_server(server: object, root: Path) -> dict[str, object]:
    if not isinstance(server, dict):
        raise PresetError("Ogni server del modello deve essere un oggetto.")
    clean = _clean_options(server, root)
    if "profiles" in server:
        profiles = server["profiles"]
        if not isinstance(profiles, dict) or not profiles:
            raise PresetError("profiles deve contenere almeno un profilo.")
        clean_profiles = {}
        for profile, item in profiles.items():
            if not isinstance(item, dict) or set(item) & {"name", "context", "profiles", "defaultProfile"}:
                raise PresetError("Ogni profilo deve essere un oggetto con le sole impostazioni del profilo.")
            clean_profiles[_text(profile, "Nome del profilo")] = _clean_options(item, root)
        if "defaultProfile" in clean and clean["defaultProfile"] not in clean_profiles:
            raise PresetError("defaultProfile non è presente in profiles.")
        for settings in clean_profiles.values():
            _validate_connection(clean | settings)
        clean["profiles"] = clean_profiles
    else:
        if "defaultProfile" in clean:
            raise PresetError("defaultProfile richiede profiles.")
        _validate_connection(clean)
    return clean


def _clean_document(data: object, root: Path) -> object:
    if not isinstance(data, list):
        return _clean_server(data, root)
    if not data:
        raise PresetError("L'elenco del modello deve contenere almeno un server.")
    clean = [_clean_server(server, root) for server in data]
    names = [_text(server.get("name"), "name") for server in clean]
    if len(set(names)) != len(names):
        raise PresetError("Ogni server del modello deve avere un nome diverso.")
    return clean


def _read_custom(root: Path) -> bytes:
    try:
        with _state_directory(root, location=_LOCATION) as directory:
            if directory is None:
                raise FileNotFoundError
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(_FILENAME, flags, dir_fd=directory), "rb") as template:
                _assert_private_file(template.fileno())
                raw = template.read(_MAX_CONFIG + 1)
    except FileNotFoundError:
        raise PresetError("Il modello personale non esiste. Apri il menu Zed Transfer in un sito già configurato e scegli Salva come modello personale.") from None
    except (OSError, AuthorizationError):
        raise PresetError("Impossibile leggere il modello personale in modo sicuro.") from None
    if len(raw) > _MAX_CONFIG:
        raise PresetError("Il modello supera la dimensione massima di 128 KiB.")
    return raw


def save_custom_preset(root: Path, source: Path | None = None) -> Path:
    """Save a reusable template outside the project after removing credentials."""
    root = _project_root(root)
    _, raw, _ = _load_source(root, source)
    payload = _encode(_clean_document(_parse_jsonc(raw), root))
    try:
        with _state_directory(root, create=True, location=_LOCATION) as directory:
            original = existing_file(directory, _FILENAME)
            if original is not None and (
                original.st_uid != os.getuid() or original.st_mode & 0o077 or original.st_nlink != 1
            ):
                raise PresetError("Il file del modello personale ha permessi non sicuri.")
            install_file(directory, _FILENAME, io.BytesIO(payload), original)
        return _account_home().joinpath(*_LOCATION, _FILENAME)
    except (OSError, AuthorizationError, LocalError):
        raise PresetError("Impossibile salvare il modello personale in modo sicuro.") from None


def _has_project_config(project: int, component: str) -> bool:
    try:
        directory = os.open(component, _directory_flags(), dir_fd=project)
    except FileNotFoundError:
        return False
    try:
        try:
            info = os.stat(_FILENAME, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise PresetError(f"{component}/sftp.json deve essere un file regolare senza symlink.")
        return True
    finally:
        os.close(directory)


def project_config_path(root: Path) -> Path | None:
    """Find a regular project configuration even when its JSON is incomplete."""
    root = _project_root(root)
    try:
        with project_parent(root, root / _FILENAME) as project:
            for component in (".vscode", ".zed"):
                if _has_project_config(project, component):
                    return root / component / _FILENAME
    except OSError:
        raise PresetError("Impossibile trovare il JSON del progetto in modo sicuro.") from None
    return None


def create_project_config(root: Path, preset: str) -> Path:
    """Create the project configuration privately and fail if one exists."""
    root = _project_root(root)
    if preset == "custom":
        document = _clean_document(_parse_jsonc(_read_custom(root)), root)
    elif preset in _BUILTINS:
        document = {
            "name": "Il mio server",
            "host": "example.invalid",
            "username": "utente",
            **_BUILTINS[preset],
            "remotePath": "/public_html",
            "uploadOnSave": False,
            "ignore": [".DS_Store"],
        }
    else:
        raise PresetError("Modello sconosciuto. Scegli sftp-key, sftp-password, ftps oppure custom.")

    payload = _encode(document)
    directory = root / ".vscode"
    target = directory / _FILENAME
    try:
        with project_parent(root, directory) as project, ExitStack() as cleanup:
            if _has_project_config(project, ".zed"):
                raise PresetError(".zed/sftp.json esiste già. Continua a usare quel file.")
            with suppress(FileExistsError):
                os.mkdir(".vscode", 0o700, dir_fd=project)
            vscode = os.open(".vscode", _directory_flags(), dir_fd=project)
            cleanup.callback(os.close, vscode)
            install_file(vscode, _FILENAME, io.BytesIO(payload), None)
    except LocalError as error:
        raise PresetError(".vscode/sftp.json esiste già. Il file è rimasto intatto.") from error
    except OSError:
        raise PresetError("Impossibile creare .vscode/sftp.json in modo sicuro.") from None
    return target
