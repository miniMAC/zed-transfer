"""Receive Zed's save notifications using the Language Server Protocol."""

import json
import re
import selectors
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from time import monotonic
from typing import BinaryIO
from urllib.parse import unquote_to_bytes, urlsplit

from .config import has_controls

_MAX_HEADERS = 8192
_MAX_BODY = 1024 * 1024
_MAX_FRAME = 64 * 1024 * 1024
_MAX_QUEUED_FILES = 128
_PROGRESS_ACK_TIMEOUT = 2.0
_SAVE_CAPABILITIES = {"textDocumentSync": {"openClose": False, "change": 0, "save": {"includeText": False}}}
_ACCEPT_UPLOAD = "Attiva upload automatico"
_CANCEL_UPLOAD = "Annulla"
_MENU_COMMAND = {"title": "Zed Transfer · Configurazione…", "command": "zed-transfer.config"}
_TRANSFER_FAILURE = "Zed Transfer · Errore durante il caricamento automatico."


@dataclass(frozen=True)
class TransferResult:
    message: str
    success: bool


@dataclass(frozen=True)
class UploadJob:
    filename: str
    run: Callable[[], TransferResult | None]


_SaveResult = str | TransferResult | UploadJob | None


@dataclass(frozen=True)
class SavePrompt:
    """Request project consent before running the supplied action."""

    message: str
    accept: Callable[[], _SaveResult]


@dataclass(frozen=True)
class MenuResult:
    message: str
    open_path: Path | None = None


@dataclass(frozen=True)
class MenuPrompt:
    message: str
    actions: tuple[str, ...]
    select: Callable[[str], MenuResult | str | None]


@dataclass(frozen=True)
class _PendingUpload:
    token: str
    job: UploadJob
    deadline: float


class _InvalidFrame(ValueError):
    """A framing error for which the input stream cannot be resynchronized."""


def _frame(stream: BinaryIO) -> bytes | None:
    headers: dict[bytes, bytes] = {}
    remaining = _MAX_HEADERS
    while True:
        line = stream.readline(remaining + 1)
        if not line and remaining == _MAX_HEADERS:
            return None
        if len(line) > remaining or not line.endswith(b"\r\n") or not line.isascii():
            raise _InvalidFrame
        remaining -= len(line)
        if line == b"\r\n":
            break
        name, separator, value = line[:-2].partition(b":")
        name = name.lower()
        if not separator or not re.fullmatch(rb"[a-z0-9-]+", name) or name in headers:
            raise _InvalidFrame
        headers[name] = value.strip()

    size = headers.get(b"content-length", b"")
    if not size.isdigit() or len(size) > len(str(_MAX_FRAME)) or not 0 < int(size) <= _MAX_FRAME:
        raise _InvalidFrame
    remaining = int(size)
    # Zed can send a complete didOpen even when the server only listens for saves.
    # Drain large frames without loading or processing the document contents.
    payload = bytearray() if remaining <= _MAX_BODY else None
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            raise _InvalidFrame
        remaining -= len(chunk)
        if payload is not None:
            payload.extend(chunk)
    return bytes(payload) if payload is not None else b""


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON-RPC key.")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("Invalid JSON-RPC number.")


def _send(stream: BinaryIO, **message: object) -> None:
    body = json.dumps({"jsonrpc": "2.0", **message}, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def _error(stream: BinaryIO, request_id: object, code: int, message: str) -> None:
    _send(stream, id=request_id, error={"code": code, "message": message})


def _log(stream: BinaryIO, message: str, *, severity: int = 3) -> None:
    _send(stream, method="window/logMessage", params={"type": severity, "message": message})


def _show(stream: BinaryIO, message: str, *, severity: int = 3) -> None:
    # Zed renders showMessage as Markdown. Preserve literal paths and filenames.
    text = re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~])", r"\\\1", message)
    _send(stream, method="window/showMessage", params={"type": severity, "message": text})


def _valid_response(message: dict[str, object]) -> bool:
    if ("result" in message) == ("error" in message):
        return False
    if "result" in message:
        return True
    error = message["error"]
    return isinstance(error, dict) and type(error.get("code")) is int and isinstance(error.get("message"), str)


def _file_uri(uri: object) -> Path | None:
    if (not isinstance(uri, str) or not uri.lower().startswith("file://") or has_controls(uri)
            or "?" in uri or "#" in uri or re.search(r"%(?![0-9a-fA-F]{2})", uri)):
        return None
    try:
        parts = urlsplit(uri)
        if parts.netloc.lower() not in {"", "localhost"}:
            return None
        path = unquote_to_bytes(parts.path).decode("utf-8")
    except (UnicodeError, ValueError):
        return None
    if (not path.startswith("/") or path.startswith("//") or "\\" in path
            or has_controls(path) or ".." in path.split("/")):
        return None
    return Path(path)


def _workspace_root(params: object) -> Path | None:
    if not isinstance(params, dict):
        return None
    root = params.get("rootUri")
    if root is None:
        folders = params.get("workspaceFolders")
        if isinstance(folders, list) and len(folders) == 1 and isinstance(folders[0], dict):
            root = folders[0].get("uri")
    path = _file_uri(root)
    if path is not None and path.is_dir():
        return path
    return None


def _save_path(params: object, root: Path) -> Path | None:
    if not isinstance(params, dict) or not isinstance(params.get("textDocument"), dict):
        return None
    path = _file_uri(params["textDocument"].get("uri"))
    if path is not None and path != root and path.is_relative_to(root):
        return path
    return None


def _input_ready(stream: BinaryIO, timeout: float) -> bool:
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            return bool(selector.select(timeout))
    except (OSError, ValueError):
        # Non-pipe readers such as BytesIO are immediately available.
        return True


class _Session:
    def __init__(
        self, sink: BinaryIO, on_save: Callable[[Path, Path], _SaveResult | SavePrompt],
        menu: Callable[[Path], MenuPrompt | MenuResult | str | None] | None,
    ):
        self.sink = sink
        self.on_save = on_save
        self.menu = menu
        self.root: Path | None = None
        self.ready = False
        self.shutdown = False
        self.supports_progress = False
        self.supports_show_document = False
        self.consent: tuple[str, SavePrompt] | None = None
        self.menu_prompt: tuple[str, MenuPrompt] | None = None
        self.upload: _PendingUpload | None = None
        self.saves: dict[Path, None] = {}
        self.queue_full = False

    def _initialize(self, request_id: object, params: object) -> None:
        if self.root is not None:
            _error(self.sink, request_id, -32600, "Server already initialized")
            return
        self.root = _workspace_root(params)
        if self.root is None:
            _error(self.sink, request_id, -32602, "A local workspace directory is required")
            return
        capabilities = params.get("capabilities") if isinstance(params, dict) else None
        window = capabilities.get("window") if isinstance(capabilities, dict) else None
        if isinstance(window, dict):
            self.supports_progress = window.get("workDoneProgress") is True
            show_document = window.get("showDocument")
            self.supports_show_document = isinstance(show_document, dict) and show_document.get("support") is True
        server_capabilities = dict(_SAVE_CAPABILITIES)
        if self.menu is not None:
            server_capabilities.update({
                "codeActionProvider": {"codeActionKinds": ["quickfix"]},
                "codeLensProvider": {"resolveProvider": False},
                "executeCommandProvider": {"commands": [_MENU_COMMAND["command"]]},
            })
        _send(self.sink, id=request_id, result={"capabilities": server_capabilities})

    def _progress(self, token: str, **value: object) -> None:
        _send(self.sink, method="$/progress", params={"token": token, "value": value})

    def _transfer_result(self, result: TransferResult) -> None:
        severity = 3 if result.success else 1
        _log(self.sink, result.message, severity=severity)
        prefix = "✅ " if result.success else ""
        _show(self.sink, prefix + result.message, severity=severity)

    def _run_upload(self, job: UploadJob, token: str | None = None) -> None:
        if token is not None:
            self._progress(token, kind="begin", title="Zed Transfer", message=f"Caricamento di {job.filename}",
                           cancellable=False)
        try:
            result = job.run()
        except Exception:
            result = TransferResult(_TRANSFER_FAILURE, False)
        if token is not None:
            end_message = "Caricamento annullato."
            if result is not None:
                end_message = "Caricamento completato." if result.success else "Caricamento non riuscito."
            self._progress(token, kind="end", message=end_message)
        if result is not None:
            self._transfer_result(result)

    def _finish_pending_upload(self, acknowledged: bool) -> None:
        upload = self.upload
        if upload is None:
            return
        self.upload = None
        self.queue_full = False
        self._run_upload(upload.job, upload.token if acknowledged else None)
        self._drain_saves()

    def _save_result(self, result: _SaveResult) -> None:
        if isinstance(result, UploadJob):
            _log(self.sink, f"Zed Transfer · Caricamento di {result.filename}…")
            if self.supports_progress:
                token = f"zed-transfer-upload-{token_hex(16)}"
                self.upload = _PendingUpload(token, result, monotonic() + _PROGRESS_ACK_TIMEOUT)
                _send(self.sink, id=token, method="window/workDoneProgress/create", params={"token": token})
            else:
                self._run_upload(result)
        elif isinstance(result, TransferResult):
            self._transfer_result(result)
        elif result:
            _log(self.sink, result)

    def _offer(self, prompt: SavePrompt | MenuPrompt) -> None:
        if isinstance(prompt, SavePrompt):
            prompt_id = f"zed-transfer-consent-{token_hex(16)}"
            self.consent = (prompt_id, prompt)
            severity = 2
            actions = (_ACCEPT_UPLOAD, _CANCEL_UPLOAD)
        else:
            prompt_id = f"zed-transfer-menu-{token_hex(16)}"
            self.menu_prompt = (prompt_id, prompt)
            severity = 3
            actions = prompt.actions
        _send(self.sink, id=prompt_id, method="window/showMessageRequest", params={
            "type": severity, "message": prompt.message, "actions": [{"title": title} for title in actions],
        })

    def _save(self, path: Path) -> None:
        if self.root is None:
            return
        try:
            result = self.on_save(self.root, path)
        except Exception:
            result = TransferResult(_TRANSFER_FAILURE, False)
        if isinstance(result, SavePrompt):
            self._offer(result)
        else:
            self._save_result(result)

    def _did_save(self, path: Path) -> None:
        if self.upload is not None:
            if path in self.saves or len(self.saves) < _MAX_QUEUED_FILES:
                self.saves[path] = None
            elif not self.queue_full:
                _log(self.sink, "Zed Transfer · Coda dei caricamenti piena. Salva di nuovo i file in attesa.", severity=2)
                self.queue_full = True
        elif self.consent is None:
            self._save(path)

    def _drain_saves(self) -> None:
        while self.saves and self.upload is None and self.consent is None:
            path = next(iter(self.saves))
            self.saves.pop(path)
            self._save(path)

    def _open_document(self, path: Path) -> None:
        if not self.supports_show_document or self.root is None or not path.is_absolute():
            return
        try:
            target = path.resolve(strict=True)
            if not target.is_relative_to(self.root.resolve(strict=True)) or not target.is_file():
                return
        except (OSError, RuntimeError):
            return
        _send(self.sink, id=f"zed-transfer-open-{token_hex(16)}", method="window/showDocument", params={
            "uri": target.as_uri(), "takeFocus": True,
        })

    def _menu_result(self, result: MenuResult | str | None) -> None:
        if isinstance(result, MenuResult):
            if result.message:
                _show(self.sink, result.message)
            if result.open_path is not None:
                self._open_document(result.open_path)
        elif result:
            _show(self.sink, result)

    def _handle_response(self, request_id: object, message: dict[str, object]) -> None:
        if not _valid_response(message):
            return
        if self.upload is not None and request_id == self.upload.token:
            self._finish_pending_upload("result" in message and message["result"] is None)
            return
        if self.consent is not None and request_id == self.consent[0]:
            prompt = self.consent[1]
        elif self.menu_prompt is not None and request_id == self.menu_prompt[0]:
            prompt = self.menu_prompt[1]
        else:
            return
        result = message.get("result")
        actions = (_ACCEPT_UPLOAD, _CANCEL_UPLOAD) if isinstance(prompt, SavePrompt) else prompt.actions
        selection = None
        if isinstance(result, dict) and set(result) == {"title"} and result["title"] in actions:
            selection = result["title"]
        elif result is not None:
            return
        if isinstance(prompt, SavePrompt):
            self.consent = None
            if selection == _ACCEPT_UPLOAD:
                try:
                    save_result = prompt.accept()
                except Exception:
                    save_result = TransferResult(_TRANSFER_FAILURE, False)
                self._save_result(save_result)
        else:
            self.menu_prompt = None
            if selection is not None:
                try:
                    menu_result = prompt.select(selection)
                except Exception:
                    menu_result = "Zed Transfer · Impossibile aggiornare la configurazione."
                self._menu_result(menu_result)
        self._drain_saves()

    def _present_menu(self, request_id: object, params: object, root: Path) -> None:
        if (self.menu is None or not isinstance(params, dict) or params.get("command") != _MENU_COMMAND["command"]
                or params.get("arguments") not in (None, [])):
            _error(self.sink, request_id, -32602, "Invalid configuration command")
            return
        _send(self.sink, id=request_id, result=None)
        if self.menu_prompt is not None:
            return
        try:
            result = self.menu(root)
        except Exception:
            result = "Zed Transfer · Impossibile leggere la configurazione."
        if isinstance(result, MenuPrompt):
            self._offer(result)
        else:
            self._menu_result(result)

    def _editor_command(self, request_id: object, method: str, params: object, root: Path) -> None:
        result: list[dict[str, object]] = []
        if _save_path(params, root) is not None:
            if method == "textDocument/codeLens":
                point = {"line": 0, "character": 0}
                result.append({"range": {"start": point, "end": point}, "command": _MENU_COMMAND})
            else:
                context = params.get("context") if isinstance(params, dict) else None
                kinds = context.get("only") if isinstance(context, dict) else None
                trigger = context.get("triggerKind") if isinstance(context, dict) else None
                if trigger != 2 and (kinds is None or (isinstance(kinds, list) and "quickfix" in kinds)):
                    result.append({"title": _MENU_COMMAND["title"], "kind": "quickfix", "command": _MENU_COMMAND})
        _send(self.sink, id=request_id, result=result)

    def _handle(self, message: object) -> int | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            _error(self.sink, None, -32600, "Invalid request")
            return None
        request_id = message.get("id")
        if "method" not in message:
            self._handle_response(request_id, message)
            return None
        is_request = "id" in message
        method = message.get("method")
        if (request_id is not None and type(request_id) not in {int, str}) or not isinstance(method, str):
            _error(self.sink, None, -32600, "Invalid request")
            return None
        if method == "exit" and not is_request:
            return 0 if self.shutdown else 1

        params = message.get("params")
        if self.shutdown:
            if is_request:
                _error(self.sink, request_id, -32600, "Server already stopped")
        elif method == "initialize" and is_request:
            self._initialize(request_id, params)
        elif self.root is None:
            if is_request:
                _error(self.sink, request_id, -32002, "Server not initialized")
        elif method == "initialized" and not is_request:
            self.ready = True
        elif method == "shutdown" and is_request:
            self.shutdown = True
            self.consent = None
            self.menu_prompt = None
            self.upload = None
            self.saves.clear()
            _send(self.sink, id=request_id, result=None)
        elif method == "textDocument/didSave" and not is_request and self.ready:
            path = _save_path(params, self.root)
            if path is not None:
                self._did_save(path)
        elif is_request and self.ready and self.menu is not None and method in {"textDocument/codeAction", "textDocument/codeLens"}:
            self._editor_command(request_id, method, params, self.root)
        elif is_request and self.ready and method == "workspace/executeCommand":
            self._present_menu(request_id, params, self.root)
        elif is_request:
            _error(self.sink, request_id, -32601, "Method not found")
        return None

    def run(self, source: BinaryIO) -> int:
        while True:
            if self.upload is not None:
                timeout = self.upload.deadline - monotonic()
                if timeout <= 0.0 or not _input_ready(source, timeout):
                    self.supports_progress = False
                    self._finish_pending_upload(False)
                    continue
            payload = _frame(source)
            if payload is None:
                return 0
            if not payload:
                continue
            try:
                message = json.loads(
                    payload.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant,
                )
            except (ValueError, RecursionError):
                _error(self.sink, None, -32700, "Parse error")
                continue
            status = self._handle(message)
            if status is not None:
                return status


def run_lsp(
    on_save: Callable[[Path, Path], _SaveResult | SavePrompt], *,
    menu: Callable[[Path], MenuPrompt | MenuResult | str | None] | None = None,
    stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
) -> int:
    """Run the Zed Transfer LSP session and close on EOF or framing error."""
    # Unbuffered reads keep input readiness accurate while awaiting progress ACKs.
    source = stdin if stdin is not None else getattr(sys.stdin.buffer, "raw", sys.stdin.buffer)
    sink = stdout if stdout is not None else sys.stdout.buffer
    try:
        return _Session(sink, on_save, menu).run(source)
    except (_InvalidFrame, OSError):
        return 1
