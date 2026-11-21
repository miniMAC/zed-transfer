"""Command line and Zed Tasks entry point."""

import argparse
import errno
import getpass
import io
import os
import sys
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from .auto_authorization import AuthorizationError, authorize, get_authorization, revoke, verify_authorization
from .config import Config, ConfigError, load_config
from .local import LocalError, existing_file, install_file, project_parent, snapshot_upload
from .lsp import run_lsp
from .paths import transfer_paths
from .transport import TransportError, open_transport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zed-transfer", description="Trasferimenti SFTP e FTP per Zed.")
    parser.add_argument("--root", type=Path, help="Directory del progetto, predefinita alla directory corrente")
    parser.add_argument("--config", type=Path, help="Percorso del JSON dentro il progetto")
    parser.add_argument("--name", help="Nome del server in un array di connessioni")
    parser.add_argument("--profile", help="Profilo definito nella proprietà profiles")
    parser.add_argument("--allow-insecure-ftp", action="store_true", help="Consente FTP in chiaro per questa esecuzione")
    password = parser.add_mutually_exclusive_group()
    password.add_argument("--password", action="store_true", help="Chiede la password nel terminale")
    password.add_argument("--allow-config-password", action="store_true", help="Consente l'uso della password nel JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Verifica la configurazione in locale")
    commands.add_parser("list", help="Elenca il contenuto della directory remota")
    commands.add_parser("install-zed", help="Installa i task Zed nel progetto")
    commands.add_parser("enable-auto-upload", help="Autorizza l'upload al salvataggio per il progetto")
    commands.add_parser("disable-auto-upload", help="Revoca l'autorizzazione per il progetto")
    commands.add_parser("lsp", help="Avvia il server locale usato dall'estensione Zed")
    for command, description in (("upload", "Carica un file"), ("download", "Scarica un file")):
        transfer = commands.add_parser(command, help=description)
        transfer.add_argument("file", nargs="?", help="Percorso nel progetto, oppure il file attivo in Zed")
    return parser


def _display(value: Path | str) -> str:
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in str(value))


def _endpoint(config: Config, remote: str) -> str:
    host = f"[{config.host}]" if ":" in config.host else config.host
    user = quote(config.username, safe="")
    return _display(f"{config.protocol}://{user}@{host}:{config.port}{remote}")


def _confirm(message: str) -> None:
    if not sys.stdin.isatty():
        raise LocalError("Apri un terminale interattivo per confermare la connessione.")
    try:
        answer = input(f"{message}\nConfermi? [s/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "s":
        raise LocalError("Operazione annullata.")


def _password(args: argparse.Namespace, config: Config) -> str | None:
    if args.allow_config_password and isinstance(config.password, str) and config.password:
        return config.password
    if args.password or config.password or config.protocol in {"ftp", "ftps"}:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                result = getpass.getpass("Password del server: ")
            except getpass.GetPassWarning as error:
                raise LocalError("Il terminale non consente di nascondere la password.") from error
        if not result:
            raise LocalError("Password vuota. Operazione annullata.")
        return result
    return None


def _check(config: Config) -> None:
    print(f"Configurazione valida ({_display(config.source)}).")
    print(_endpoint(config, config.remote_path))
    print(f"Directory locale {_display(config.context)}")
    if config.remote_temp_path is not None:
        print(f"Directory temporanea remota {_display(config.remote_temp_path)}")
    if config.protocol == "ftp":
        print("FTP in chiaro richiede --allow-insecure-ftp per ogni esecuzione.")
    if isinstance(config.password, str) and config.password:
        print("Il JSON contiene una password. Il tool la chiede al terminale per impostazione predefinita.")
    if config.upload_on_save:
        auth = get_authorization(config.root)
        if auth is not None and verify_authorization(auth, config):
            print("Upload al salvataggio autorizzato. Zed deve avere l'estensione Zed Transfer attiva.")
        else:
            print("uploadOnSave è presente. Usa enable-auto-upload per autorizzare il progetto.")
    if config.unsupported:
        print(f"Opzioni senza effetto in Zed Transfer: {_display(', '.join(config.unsupported))}")


def _install_zed(root: Path) -> None:
    target = root / ".zed" / "tasks.json"
    template = resources.files("zed_transfer").joinpath("templates", "tasks.json")
    with project_parent(root, target, create_zed=True) as directory:
        if existing_file(directory, target.name) is not None:
            raise LocalError(".zed/tasks.json esiste già. Aggiungi i task dal modello del repository.")
        install_file(directory, target.name, io.BytesIO(template.read_bytes()), None)
    print("Task installati in .zed/tasks.json. Apri la palette di Zed e cerca task: spawn.")


def _protect_local(config: Config, local: Path) -> None:
    parts = {part.casefold() for part in local.relative_to(config.root).parts}
    protected = {
        ".git", ".zed", ".vscode", ".ssh", ".env", "wp-config.php", ".htpasswd", "auth.json",
        ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    }
    named = {str(path).casefold() for path in (config.source, config.private_key_path) if path is not None}
    if (str(local).casefold() in named or parts & protected
            or any(part.startswith(".env.") or part.endswith((".key", ".pem", ".p12", ".pfx")) for part in parts)):
        raise LocalError("Il percorso locale è protetto dai trasferimenti.")


@contextmanager
def _upload_snapshot(config: Config, local: Path) -> Iterator[Path]:
    with project_parent(config.root, local, workspace_identity=config.workspace_identity) as directory:
        with TemporaryDirectory(prefix="zed-transfer-") as temporary:
            staged = Path(temporary) / "data"
            snapshot_upload(directory, local.name, staged)
            yield staged


def _enable_auto_upload(args: argparse.Namespace, config: Config) -> None:
    if not config.upload_on_save:
        raise LocalError('Imposta "uploadOnSave": true nel profilo scelto.')
    if config.protocol == "ftp":
        raise LocalError("Per l'upload automatico usa SFTP o FTPS.")
    if args.password or config.password is True:
        raise LocalError("Per l'upload automatico usa una chiave SSH o autorizza la password già nel JSON.")
    if config.password or config.protocol == "ftps":
        if not args.allow_config_password or not isinstance(config.password, str):
            raise LocalError("Per usare la password già nel JSON, aggiungi --allow-config-password prima del comando.")
    message = (
        f"Autorizza i salvataggi in Zed da {_display(config.context)} a {_endpoint(config, config.remote_path)}.\n"
        "I file sul server possono essere sostituiti automaticamente."
    )
    if config.remote_temp_path is not None:
        message += f"\nDirectory temporanea remota {_display(config.remote_temp_path)}."
    if args.allow_config_password and isinstance(config.password, str):
        message += "\nVerrà usata la password già presente nel JSON."
    _confirm(message)
    authorize(config, allow_config_password=args.allow_config_password)
    print("Upload al salvataggio autorizzato. Apri il progetto in Zed con l'estensione Zed Transfer attiva.")


def _auto_upload(root: Path, workspace: Path, file: Path) -> str | None:
    if not file.is_relative_to(workspace) or workspace.resolve(strict=True) != root:
        return None
    try:
        auth = get_authorization(root)
    except AuthorizationError as error:
        return f"Zed Transfer · {_display(str(error))}"
    if auth is None:
        return None

    try:
        config = load_config(root, path=auth.config_path, name=auth.name, profile=auth.profile)
    except ConfigError:
        return "Zed Transfer · Configurazione cambiata o non valida. Verificala con zed-transfer check."
    if not config.upload_on_save:
        return None
    if not verify_authorization(auth, config):
        return "Zed Transfer · Configurazione cambiata. Conferma di nuovo con enable-auto-upload."

    if config.protocol == "ftp" or config.password is True:
        return None
    password = config.password if auth.allow_config_password and isinstance(config.password, str) else None
    if password is None and (config.password or config.protocol == "ftps"):
        return None

    try:
        local, remote = transfer_paths(config, file.relative_to(workspace))
        _protect_local(config, local)
    except (ConfigError, LocalError):
        return None
    try:
        with _upload_snapshot(config, local) as staged:
            if get_authorization(root) != auth:
                return None
            with open_transport(config, password) as transport:
                transport.upload(staged, remote)
    except (AuthorizationError, LocalError, TransportError) as error:
        return f"Zed Transfer · Upload non riuscito per {_display(local.name)}. {_display(str(error))}"
    except OSError:
        return f"Zed Transfer · Impossibile leggere o caricare {_display(local.name)}."
    return f"Zed Transfer · Caricato {_endpoint(config, remote)}"


def _transfer(args: argparse.Namespace, config: Config) -> None:
    file = args.file if args.file is not None else os.environ.get("ZED_RELATIVE_FILE")
    if not file:
        raise LocalError("Apri un file in Zed o passa il percorso del file da trasferire.")
    local, remote = transfer_paths(config, file)
    _protect_local(config, local)
    destination = _endpoint(config, remote)

    if args.command == "upload":
        message = f"Carica {_display(local)} su {destination}. Il file remoto potrebbe essere sostituito."
        if config.remote_temp_path is not None:
            message += f" Directory temporanea remota {_display(config.remote_temp_path)}."
        with _upload_snapshot(config, local) as staged:
            _confirm(message)
            with open_transport(config, _password(args, config), allow_insecure_ftp=args.allow_insecure_ftp) as transport:
                transport.upload(staged, remote)
    else:
        with project_parent(config.root, local) as directory, TemporaryDirectory(prefix="zed-transfer-") as temporary:
            staged = Path(temporary) / "data"
            original = existing_file(directory, local.name)
            message = f"Scarica {destination} in {_display(local)}."
            if original is not None:
                message += " Il file locale sarà sostituito."
            _confirm(message)
            with open_transport(config, _password(args, config), allow_insecure_ftp=args.allow_insecure_ftp) as transport:
                transport.download(remote, staged)
            with staged.open("rb") as downloaded:
                install_file(directory, local.name, downloaded, original)
    print("Trasferimento completato.")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        try:
            root = (args.root or Path.cwd()).resolve(strict=True)
        except RuntimeError as error:
            raise LocalError("Impossibile risolvere il percorso del progetto.") from error
        if not root.is_dir():
            raise LocalError("La radice del progetto deve essere una directory.")
        if args.command == "install-zed":
            _install_zed(root)
            return 0
        if args.command == "disable-auto-upload":
            revoke(root)
            print("Upload al salvataggio disattivato per questo progetto.")
            return 0
        if args.command == "lsp":
            return run_lsp(lambda workspace, file: _auto_upload(root, workspace, file))
        config = load_config(root, path=args.config, name=args.name, profile=args.profile)
        if args.command == "check":
            _check(config)
            return 0
        if args.command == "enable-auto-upload":
            _enable_auto_upload(args, config)
            return 0
        if config.protocol == "ftp" and not args.allow_insecure_ftp:
            raise LocalError("FTP in chiaro richiede --allow-insecure-ftp. Abilita FTPS o SFTP se disponibili.")
        if args.command == "list":
            _confirm(f"Elenca il contenuto di {_endpoint(config, config.remote_path)}.")
            with open_transport(config, _password(args, config), allow_insecure_ftp=args.allow_insecure_ftp) as transport:
                for line in transport.list(config.remote_path):
                    print(_display(line))
        else:
            _transfer(args, config)
    except (AuthorizationError, ConfigError, LocalError, TransportError) as error:
        print(f"Errore. {_display(str(error))}", file=sys.stderr)
        return 1
    except OSError as error:
        message = "Percorso inesistente." if error.errno == errno.ENOENT else (error.strerror or "Errore di I/O.")
        print(f"Errore. {_display(message)}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nOperazione annullata.", file=sys.stderr)
        return 130
    return 0
