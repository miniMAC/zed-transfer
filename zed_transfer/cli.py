"""Command line and Zed Tasks entry point."""

import argparse
import errno
import getpass
import io
import os
import re
import sys
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from .auto_authorization import Authorization, AuthorizationError, authorize, get_auto_upload_status, revoke, verify_authorization
from .config import Config, ConfigError, load_config
from .local import LocalError, existing_file, install_file, project_parent, snapshot_upload
from .lsp import MenuPrompt, MenuResult, SavePrompt, TransferResult, UploadJob, run_lsp
from .paths import transfer_paths
from .presets import PresetError, create_project_config, project_config_path, save_custom_preset
from .transport import TransportError, open_transport


_PRESET_LABELS = {
    "sftp-key": "SFTP con chiave",
    "sftp-password": "SFTP con password",
    "ftps": "FTPS",
    "custom": "Modello personale",
}


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
    init = commands.add_parser("init", help="Crea .vscode/sftp.json da un modello")
    init.add_argument("preset", nargs="?", choices=tuple(_PRESET_LABELS), help="Ometti per scegliere il modello")
    commands.add_parser("save-preset", help="Salva la configurazione come modello personale senza credenziali")
    for command, description in (("upload", "Carica un file"), ("download", "Scarica un file")):
        transfer = commands.add_parser(command, help=description)
        transfer.add_argument("file", nargs="?", help="Percorso nel progetto, oppure il file attivo in Zed")
    return parser


def _display(value: Path | str) -> str:
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in str(value))


def _markdown_code(value: Path | str) -> str:
    text = _display(value)
    delimiter = "`" * (1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{delimiter} {text} {delimiter}"


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
        auth, disabled = get_auto_upload_status(config.root)
        if auth is not None and verify_authorization(auth, config):
            print("Upload al salvataggio autorizzato. Zed deve avere l'estensione Zed Transfer attiva.")
        elif disabled:
            print("Upload al salvataggio disattivato. Usa enable-auto-upload per riattivarlo.")
        else:
            print("Al prossimo salvataggio Zed può chiedere di autorizzare l'upload automatico.")
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


def _choose_preset(root: Path) -> str | None:
    existing = project_config_path(root)
    if existing is not None:
        raise LocalError(f"Configurazione già presente. Apri {_display(existing)} per modificarla.")
    if not sys.stdin.isatty():
        raise LocalError("Scegli il modello in un terminale interattivo o passalo come argomento a init.")

    print(f"Crea JSON Zed Transfer\nProgetto {_display(root)}\n")
    choices = {}
    for index, (preset, label) in enumerate(_PRESET_LABELS.items(), start=1):
        choices[str(index)] = preset
        print(f"{index}. {label}")
    print("0. Annulla")
    answer = input("Scegli il modello [0–4] ").strip()
    if answer in {"", "0"}:
        return None
    if answer not in choices:
        raise LocalError("Scelta non valida. Usa un numero da 0 a 4.")
    return choices[answer]


def _preset_menu(root: Path, workspace: Path) -> MenuPrompt | MenuResult | None:
    try:
        if workspace.resolve(strict=True) != root:
            return None
        existing = project_config_path(root)
    except (OSError, RuntimeError, PresetError) as error:
        return MenuResult(f"Impossibile aprire la configurazione locale. {_display(str(error))}")

    templates = {f"Crea JSON · {label}": preset for preset, label in _PRESET_LABELS.items() if preset != "custom"}
    templates["Crea JSON dal modello personale"] = "custom"
    open_json = "Apri sftp.json"
    save_preset = "Salva come modello personale"
    if existing is None:
        message = "Scegli il modello per creare `.vscode/sftp.json` in questo progetto."
        actions = tuple(templates)
    else:
        message = (
            "Apri il JSON del progetto per modificarlo. Salvarlo come modello personale "
            "aggiorna il modello riutilizzabile senza copiare la password."
        )
        actions = (open_json, save_preset)

    def select(action: str) -> MenuResult | None:
        try:
            if action == open_json:
                path = project_config_path(root)
                return MenuResult("Configurazione del progetto." if path else "Il file sftp.json non esiste più.", path)
            if action == save_preset:
                save_custom_preset(root)
                return MenuResult("Modello personale salvato. Puoi usarlo negli altri progetti.")
            preset = templates.get(action)
            if preset is not None:
                path = create_project_config(root, preset)
                return MenuResult("Creato .vscode/sftp.json. Controlla i dati della connessione prima di usarlo.", path)
        except (LocalError, PresetError, ConfigError) as error:
            return MenuResult(_display(str(error)))
        except OSError:
            return MenuResult("Impossibile leggere o scrivere la configurazione locale.")
        return None

    return MenuPrompt(message, actions, select)


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
def _upload_snapshot(
    config: Config, local: Path, *, expected_source: os.stat_result | None = None,
) -> Iterator[Path]:
    with project_parent(config.root, local, workspace_identity=config.workspace_identity) as directory:
        with TemporaryDirectory(prefix="zed-transfer-") as temporary:
            staged = Path(temporary) / "data"
            snapshot_upload(directory, local.name, staged, expected_source=expected_source)
            yield staged


def _approval_message(config: Config, *, uses_password: bool, markdown: bool = False) -> str:
    render = _markdown_code if markdown else _display
    lines = [
        f"Cartella locale {render(config.context)}",
        f"Destinazione {render(_endpoint(config, config.remote_path))}",
        "I file sul server possono essere sostituiti automaticamente.",
    ]
    if config.remote_temp_path is not None:
        lines.append(f"Directory temporanea remota {render(config.remote_temp_path)}")
    if uses_password:
        lines.append("Verrà usata la password già presente nel JSON.")
    return ("\n\n" if markdown else "\n").join(lines)


def _enable_auto_upload(args: argparse.Namespace, config: Config) -> None:
    if not config.upload_on_save:
        raise LocalError('Imposta "uploadOnSave": true nel profilo scelto.')
    if config.protocol == "ftp":
        raise LocalError("Per l'upload automatico usa SFTP o FTPS.")
    if args.password or config.password is True:
        raise LocalError("Per l'upload automatico usa una chiave SSH o autorizza la password già nel JSON.")
    uses_password = args.allow_config_password and isinstance(config.password, str)
    if (config.password or config.protocol == "ftps") and not uses_password:
        raise LocalError("Per usare la password già nel JSON, aggiungi --allow-config-password prima del comando.")
    _confirm(_approval_message(config, uses_password=uses_password))
    authorize(config, allow_config_password=args.allow_config_password)
    print("Upload al salvataggio autorizzato. Apri il progetto in Zed con l'estensione Zed Transfer attiva.")


def _read_auto_config(root: Path, auth: Authorization | None) -> Config:
    if auth is None:
        return load_config(root)
    return load_config(root, path=auth.config_path, name=auth.name, profile=auth.profile)


def _upload_approved(
    config: Config, local: Path, remote: str, auth: Authorization,
    *, expected_source: os.stat_result | None = None,
) -> TransferResult | None:
    filename = _display(local.name)
    try:
        if get_auto_upload_status(config.root) != (auth, False):
            return None
        if not verify_authorization(auth, _read_auto_config(config.root, auth)):
            raise LocalError("Configurazione cambiata. Salva di nuovo per controllare la destinazione.")
        password = config.password if auth.allow_config_password and isinstance(config.password, str) else None
        if (config.password or config.protocol == "ftps") and password is None:
            raise LocalError("L'upload automatico richiede credenziali disponibili senza prompt.")
        with _upload_snapshot(config, local, expected_source=expected_source) as staged:
            if get_auto_upload_status(config.root) != (auth, False):
                return None
            with open_transport(config, password) as transport:
                transport.upload(staged, remote)
    except (AuthorizationError, ConfigError, LocalError, TransportError) as error:
        return TransferResult(f"Upload non riuscito per {filename}. {_display(str(error))}", success=False)
    except OSError:
        return TransferResult(f"Impossibile leggere o caricare {filename}.", success=False)
    return TransferResult(f"Caricato {filename} su {_endpoint(config, remote)}", success=True)


def _auto_upload(
    root: Path, workspace: Path, file: Path, prompted: set[str],
) -> str | SavePrompt | TransferResult | UploadJob | None:
    try:
        if not file.is_relative_to(workspace) or workspace.resolve(strict=True) != root:
            return None
        auth, disabled = get_auto_upload_status(root)
    except AuthorizationError as error:
        return f"Zed Transfer · {_display(str(error))}"
    except (OSError, RuntimeError):
        return None
    if disabled:
        return None
    try:
        config = _read_auto_config(root, auth)
    except ConfigError as error:
        if auth is not None:
            return TransferResult(f"Configurazione FTP o SFTP non valida. {_display(str(error))}", success=False)
        return None
    if not config.upload_on_save or config.protocol == "ftp" or config.password is True:
        return None
    try:
        local, remote = transfer_paths(config, file.relative_to(workspace))
        _protect_local(config, local)
    except (ConfigError, LocalError):
        return None
    filename = _display(local.name)
    if auth is not None and verify_authorization(auth, config):
        return UploadJob(filename, lambda: _upload_approved(config, local, remote, auth))

    uses_password = isinstance(config.password, str)
    if config.source_fingerprint in prompted or (config.protocol == "ftps" and not uses_password):
        return None
    try:
        saved_file = local.lstat()
    except OSError:
        return None
    prompted.add(config.source_fingerprint)

    def accept() -> TransferResult | UploadJob:
        try:
            if _read_auto_config(root, auth) != config:
                raise LocalError("Configurazione cambiata. Salva di nuovo per controllare la destinazione.")
            approved = authorize(config, allow_config_password=uses_password, expected_status=(auth, False))
        except (AuthorizationError, ConfigError, LocalError) as error:
            return TransferResult(_display(str(error)), success=False)
        return UploadJob(filename, lambda: _upload_approved(config, local, remote, approved, expected_source=saved_file))

    message = _approval_message(config, uses_password=uses_password, markdown=True)
    message += f"\n\nConfermando caricherai anche il file appena salvato, {_markdown_code(local.name)}."
    return SavePrompt(message, accept)


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
            prompted: set[str] = set()
            return run_lsp(
                lambda workspace, file: _auto_upload(root, workspace, file, prompted),
                menu=lambda workspace: _preset_menu(root, workspace),
            )
        if args.command == "init":
            preset = args.preset or _choose_preset(root)
            if preset is None:
                return 0
            path = create_project_config(root, preset)
            print(f"Configurazione creata in {_display(path)}")
            return 0
        if args.command == "save-preset":
            path = save_custom_preset(root, args.config)
            print(f"Modello personale salvato in {_display(path)}")
            return 0
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
    except (AuthorizationError, ConfigError, LocalError, PresetError, TransportError) as error:
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
