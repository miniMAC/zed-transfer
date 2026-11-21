"""Read the VS Code SFTP project configuration without executing workspace code."""

import fnmatch
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath


class ConfigError(ValueError):
    """An invalid or unsupported configuration or path."""


@dataclass(frozen=True)
class Config:
    root: Path
    source: Path
    source_fingerprint: str = field(repr=False)
    workspace_identity: tuple[int, int]
    context: Path
    name: str | None
    profile: str | None
    protocol: str
    host: str
    port: int
    username: str
    password: str | bool | None = field(repr=False)
    private_key_path: Path | None
    remote_path: str
    remote_temp_path: str | None
    timeout: float
    ignore: tuple[str, ...]
    upload_on_save: bool
    unsupported: tuple[str, ...]


_MAX_CONFIG = 128 * 1024
_JSON_STRING = r'"(?:\\.|[^"\\])*"'
_COMMENT_OR_STRING = re.compile(_JSON_STRING + r"|//[^\r\n]*|/\*[\s\S]*?\*/")
_TRAILING_COMMA_OR_STRING = re.compile(_JSON_STRING + r"|,(?=[ \t\r\n]*[}\]])")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SSH_USERNAME = re.compile(r"[A-Za-z0-9_.-][A-Za-z0-9_.@-]*")
_SUPPORTED = {
    "$schema", "name", "context", "host", "port", "protocol", "username",
    "password", "privateKeyPath", "remotePath", "remoteTempPath", "secure", "connectTimeout",
    "ignore", "uploadOnSave", "profiles", "defaultProfile", "agent",
}
_INACTIVE = {
    "concurrency", "dirPerm", "downloadOnOpen", "filePerm", "limitOpenFilesOnRemote",
    "openSsh", "remoteExplorer", "remoteTimeOffsetInHours", "syncOption",
    "useTempFile", "watcher",
}
_REJECTED = {
    "algorithms", "hop", "hops", "ignoreFile", "interactiveAuth",
    "passphrase", "proxy", "remote", "secureOptions", "sshConfigPath",
    "sshCustomParams",
}


def has_controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or has_controls(value):
        raise ConfigError(f"{label} deve essere una stringa valida.")
    return value


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Il JSON contiene una chiave duplicata.")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ConfigError("Il JSON contiene un numero non valido.")


def _parse_jsonc(raw: bytes) -> object:
    def strip_comment(match: re.Match[str]) -> str:
        token = match.group()
        if token.startswith('"'):
            return token
        return re.sub(r"[^\r\n]", " ", token)

    def strip_trailing_comma(match: re.Match[str]) -> str:
        token = match.group()
        if token != ",":
            return token
        previous = match.start() - 1
        while previous >= 0 and without_comments[previous] in " \t\r\n":
            previous -= 1
        if previous < 0 or without_comments[previous] in "[{,:":
            return token
        return " "

    try:
        data = raw.decode("utf-8-sig")
        without_comments = _COMMENT_OR_STRING.sub(strip_comment, data)
        data = _TRAILING_COMMA_OR_STRING.sub(strip_trailing_comma, without_comments)
        return json.loads(data, object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON non valido alla riga {exc.lineno}, colonna {exc.colno}.") from None
    except ConfigError:
        raise
    except (ValueError, RecursionError):
        raise ConfigError("Il contenuto della configurazione non è un JSON UTF-8 valido.") from None


def _workspace_path(root: Path, path: str | Path, *, label: str) -> Path:
    value = str(path)
    if has_controls(value) or "\\" in value or PureWindowsPath(value).drive:
        raise ConfigError(f"{label} contiene un percorso non valido.")
    candidate = Path(value)
    if ".." in candidate.parts:
        raise ConfigError(f"{label} contiene un segmento '..'.")
    absolute = candidate if candidate.is_absolute() else root / candidate
    if not absolute.is_relative_to(root):
        raise ConfigError(f"{label} deve trovarsi all'interno del workspace.")
    return absolute


def _read_config(
    root: Path, source: Path, *, allow_missing: bool = False
) -> tuple[bytes, tuple[int, int]] | None:
    parts = source.relative_to(root).parts
    if not parts:
        raise ConfigError("La configurazione deve essere un file regolare.")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(root, directory_flags)
        try:
            root_info = os.fstat(current)
            for part in parts[:-1]:
                child = os.open(part, directory_flags, dir_fd=current)
                os.close(current)
                current = child
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(parts[-1], flags, dir_fd=current), "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ConfigError("La configurazione deve essere un file regolare.")
                raw = handle.read(_MAX_CONFIG + 1)
        finally:
            os.close(current)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ConfigError("Il file di configurazione non esiste.") from None
    except OSError:
        raise ConfigError(
            "Impossibile leggere la configurazione. Verifica che esista e che il percorso "
            "non contenga collegamenti simbolici."
        ) from None
    if len(raw) > _MAX_CONFIG:
        raise ConfigError("La configurazione supera la dimensione massima di 128 KiB.")
    return raw, (root_info.st_dev, root_info.st_ino)


def _load_source(root: Path, path: Path | None) -> tuple[Path, bytes, tuple[int, int]]:
    if path is not None:
        source = _workspace_path(root, path, label="Il file di configurazione")
        loaded = _read_config(root, source)
    else:
        source = root / ".vscode" / "sftp.json"
        loaded = _read_config(root, source, allow_missing=True)
        if loaded is None:
            source = root / ".zed" / "sftp.json"
            loaded = _read_config(root, source)
    if loaded is None:
        raise ConfigError("Il file di configurazione non esiste.")
    raw, identity = loaded
    return source, raw, identity


def _select_server(data: object, name: str | None) -> dict[str, object]:
    if isinstance(data, dict):
        if name is not None and data.get("name") != name:
            raise ConfigError("Il nome richiesto non corrisponde alla configurazione.")
        return data
    if not isinstance(data, list) or not data:
        raise ConfigError("La configurazione deve essere un oggetto o un elenco non vuoto.")
    servers: dict[str, dict[str, object]] = {}
    for server in data:
        if not isinstance(server, dict):
            raise ConfigError("Ogni elemento dell'elenco deve essere un oggetto.")
        server_name = _text(server.get("name"), "name")
        if server_name in servers:
            raise ConfigError("L'elenco contiene nomi di server duplicati.")
        servers[server_name] = server
    if name is None and len(servers) == 1:
        return next(iter(servers.values()))
    if name is None:
        raise ConfigError("La configurazione contiene più server. Selezionane uno con --name.")
    if name not in servers:
        raise ConfigError("Il server richiesto non è presente nella configurazione.")
    return servers[name]


def _apply_profile(
    data: dict[str, object], profile: str | None
) -> tuple[dict[str, object], str | None]:
    base = {key: value for key, value in data.items() if key not in {"profiles", "defaultProfile"}}
    if "profiles" not in data:
        if "defaultProfile" in data or profile is not None:
            raise ConfigError("Il profilo richiesto non è presente nella configurazione.")
        return base, None
    profiles = data["profiles"]
    if not isinstance(profiles, dict) or not profiles:
        raise ConfigError("profiles deve contenere almeno un profilo.")
    for key, item in profiles.items():
        _text(key, "Nome del profilo")
        if not isinstance(item, dict):
            raise ConfigError("Ogni profilo deve essere un oggetto.")
        if set(item).intersection({"context", "name", "profiles", "defaultProfile"}):
            raise ConfigError("Usa context, name, profiles e defaultProfile al livello del server.")
    if "defaultProfile" in data:
        default = _text(data["defaultProfile"], "defaultProfile")
        if default not in profiles:
            raise ConfigError("defaultProfile non è presente in profiles.")
    else:
        default = None
    selected = profile if profile is not None else default
    if selected is None:
        if len(profiles) != 1:
            raise ConfigError("La configurazione contiene più profili. Selezionane uno con --profile.")
        selected = next(iter(profiles))
    if selected not in profiles:
        raise ConfigError("Il profilo richiesto non è presente nella configurazione.")
    return base | profiles[selected], selected


def _protocol(data: dict[str, object]) -> str:
    protocol = _text(data.get("protocol", "sftp"), "protocol").lower()
    if protocol not in {"sftp", "ftp", "ftps"}:
        raise ConfigError("protocol deve essere sftp, ftp oppure ftps.")
    secure = data.get("secure")
    if "secure" in data and type(secure) is not bool:
        raise ConfigError("secure accetta true o false. Per FTPS esplicito usa secure: true.")
    if protocol == "sftp" and secure is True:
        raise ConfigError("secure: true si usa con FTP per attivare FTPS.")
    if protocol == "ftps" and secure is False:
        raise ConfigError("protocol ftps richiede TLS e non ammette secure: false.")
    return "ftps" if protocol == "ftp" and secure is True else protocol


def _host(value: object) -> str:
    host = _text(value, "host")
    if "%" not in host:
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
    try:
        host = host.encode("idna").decode("ascii").lower().removesuffix(".")
    except UnicodeError:
        raise ConfigError("host deve contenere solo il nome DNS o l'indirizzo IP.") from None
    if len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split(".")):
        raise ConfigError("host deve contenere solo il nome DNS o l'indirizzo IP.")
    return host


def validate_remote_path(value: object, *, field: str = "remotePath") -> str:
    path = _text(value, field)
    if not path.startswith("/") or "\\" in path or ".." in path.split("/"):
        raise ConfigError(f"{field} deve essere un percorso POSIX assoluto senza '..'.")
    return "/" + "/".join(part for part in path.split("/") if part not in {"", "."})


def _key_path(value: object, root: Path) -> Path | None:
    if value is None:
        return None
    raw = _text(value, "privateKeyPath")
    home_relative = raw.startswith("~/")
    value = raw[2:] if home_relative else raw
    if (
        not value
        or value.startswith("~")
        or (home_relative and value.startswith("/"))
        or "${" in value
        or "%" in value
        or "\\" in value
        or PureWindowsPath(value).drive
    ):
        raise ConfigError("privateKeyPath deve essere un percorso POSIX. Puoi usare ~/.")
    key = Path(value)
    if ".." in key.parts:
        raise ConfigError("privateKeyPath contiene un segmento '..'.")
    if home_relative:
        return Path.home() / key
    return key if key.is_absolute() else root / key


def _ignore_patterns(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError("ignore deve essere un elenco di pattern.")
    patterns = []
    for item in value:
        if not isinstance(item, str) or has_controls(item):
            raise ConfigError("ignore deve contenere solamente pattern di testo.")
        if not item or item.startswith("#"):
            continue
        if (
            item.startswith("!")
            or item.rstrip() != item
            or any(char in item for char in "[]{}\\")
            or any(part in {"", ".", ".."} for part in item.strip("/").split("/"))
            or any("**" in part and part != "**" for part in item.split("/"))
        ):
            raise ConfigError("ignore supporta *, ? e ** come segmento intero, senza negazioni.")
        patterns.append(item)
    return tuple(patterns)


def is_ignored(parts: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    """Apply a subset of gitignore rules to a file path relative to context."""
    for pattern in patterns:
        stripped = pattern.strip("/")
        segments = stripped.split("/")
        directory_only = pattern.endswith("/")
        limit = len(parts) - int(directory_only)
        if not pattern.startswith("/") and "/" not in stripped:
            if any(fnmatch.fnmatchcase(part, stripped) for part in parts[:limit]):
                return True
            continue
        positions = {0}
        for index, segment in enumerate(segments):
            if not positions:
                break
            if segment == "**":
                start = min(positions) + int(index == len(segments) - 1)
                positions = set(range(start, limit + 1))
            else:
                positions = {
                    position + 1
                    for position in positions
                    if position < limit and fnmatch.fnmatchcase(parts[position], segment)
                }
        if positions:
            return True
    return False


def load_config(
    root: Path, *, name: str | None = None, profile: str | None = None, path: Path | None = None
) -> Config:
    """Load a selected server and profile from a trusted workspace."""
    try:
        root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ConfigError("Impossibile aprire la directory del workspace.") from None
    if not root.is_dir():
        raise ConfigError("Il workspace deve essere una directory.")
    source, raw, identity = _load_source(root, path)
    server = _select_server(_parse_jsonc(raw), name)
    data, selected_profile = _apply_profile(server, profile)
    for key in sorted(_REJECTED.intersection(data)):
        if data[key] is not None and data[key] is not False:
            raise ConfigError(f"L'opzione {key} non è supportata.")
    unsupported = sorted(_INACTIVE.intersection(data))
    if data.keys() - _SUPPORTED - _INACTIVE - _REJECTED:
        unsupported.append("altre opzioni")

    protocol = _protocol(data)
    agent = data.get("agent")
    if agent is not None and (protocol != "sftp" or agent != "$SSH_AUTH_SOCK"):
        raise ConfigError("agent accetta solo $SSH_AUTH_SOCK con il protocollo sftp.")
    port = data.get("port", 22 if protocol == "sftp" else 21)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("port deve essere un intero da 1 a 65535.")
    timeout = data.get("connectTimeout", 20_000)
    if type(timeout) not in {int, float} or not 1 <= timeout <= 300_000 or not math.isfinite(timeout):
        raise ConfigError("connectTimeout deve essere un numero da 1 a 300000 millisecondi.")
    context = _workspace_path(
        root, _text(data.get("context", "."), "context").lstrip("/"), label="context"
    )
    _check_local(context, root, directory=True)
    password = data.get("password")
    if (
        password is not None
        and type(password) is not bool
        and (not isinstance(password, str) or not password or has_controls(password))
    ):
        raise ConfigError("password deve essere una stringa valida oppure un valore booleano.")
    upload_on_save = data.get("uploadOnSave", False)
    if type(upload_on_save) is not bool:
        raise ConfigError("uploadOnSave deve essere un valore booleano.")
    key_path = _key_path(data.get("privateKeyPath"), root)
    if key_path is not None and protocol != "sftp":
        raise ConfigError("privateKeyPath richiede il protocollo sftp.")
    username = _text(data.get("username"), "username")
    if len(username.encode("utf-8")) > 255:
        raise ConfigError("username supera la lunghezza massima.")
    if protocol == "sftp" and _SSH_USERNAME.fullmatch(username) is None:
        raise ConfigError("username SFTP accetta lettere ASCII, numeri, punti, trattini, _ e @.")
    config_name = data.get("name")
    remote_temp = data.get("remoteTempPath")
    return Config(
        root=root,
        source=source,
        source_fingerprint=hashlib.sha256(raw).hexdigest(),
        workspace_identity=identity,
        context=context,
        name=_text(config_name, "name") if config_name is not None else None,
        profile=selected_profile,
        protocol=protocol,
        host=_host(data.get("host")),
        port=port,
        username=username,
        password=password,
        private_key_path=key_path,
        remote_path=validate_remote_path(data.get("remotePath", "/")),
        remote_temp_path=(
            validate_remote_path(remote_temp, field="remoteTempPath") if remote_temp is not None else None
        ),
        timeout=timeout / 1000,
        ignore=_ignore_patterns(data.get("ignore", [])),
        upload_on_save=upload_on_save,
        unsupported=tuple(unsupported),
    )


def _check_local(path: Path, root: Path, *, directory: bool) -> None:
    parts = path.relative_to(root).parts
    current = root
    for index, part in enumerate(parts):
        current /= part
        final = index == len(parts) - 1
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if final and not directory:
                return
            raise ConfigError("La directory locale di destinazione deve esistere.") from None
        except OSError:
            raise ConfigError("Impossibile verificare il percorso locale.") from None
        if stat.S_ISLNK(mode):
            raise ConfigError("Il percorso locale attraversa un collegamento simbolico.")
        if directory or not final:
            if not stat.S_ISDIR(mode):
                raise ConfigError("Una directory richiesta dal percorso locale non è valida.")
        elif not stat.S_ISREG(mode):
            raise ConfigError("Il percorso locale deve indicare un file regolare.")


def transfer_paths(config: Config, file: str | Path) -> tuple[Path, str]:
    """Map a workspace-relative file or an absolute local file into the remote context."""
    local = _workspace_path(config.root, file, label="Il file locale")
    if local == config.context or not local.is_relative_to(config.context):
        raise ConfigError("Il file locale deve trovarsi all'interno di context.")
    relative = local.relative_to(config.context)
    if is_ignored(relative.parts, config.ignore):
        raise ConfigError("Il file corrisponde a un pattern ignore.")
    _check_local(local, config.root, directory=False)
    remote = validate_remote_path(
        config.remote_path.rstrip("/") + "/" + relative.as_posix(), field="Il file remoto"
    )
    return local, remote
