"""Receive Zed's save notifications using the Language Server Protocol."""

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import BinaryIO
from urllib.parse import unquote_to_bytes, urlsplit

from .config import has_controls

_MAX_HEADERS = 8192
_MAX_BODY = 1024 * 1024
_MAX_FRAME = 64 * 1024 * 1024
_SAVE_CAPABILITIES = {"textDocumentSync": {"openClose": False, "change": 0, "save": {"includeText": False}}}
_ACCEPT_UPLOAD = "Attiva upload automatico"
_CANCEL_UPLOAD = "Annulla"


@dataclass(frozen=True)
class SavePrompt:
    """Request project consent before running the supplied action."""

    message: str
    accept: Callable[[], str | None]


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


def _save_failed(stream: BinaryIO) -> int:
    _log(stream, "Zed Transfer. Errore durante il caricamento automatico.", severity=1)
    return 1


def _prompt_answer(message: dict[str, object]) -> bool | None:
    if ("result" in message) == ("error" in message):
        return None
    if "error" in message:
        error = message["error"]
        if isinstance(error, dict) and type(error.get("code")) is int and isinstance(error.get("message"), str):
            return False
        return None
    result = message["result"]
    if result is None or result == {"title": _CANCEL_UPLOAD}:
        return False
    if result == {"title": _ACCEPT_UPLOAD}:
        return True
    return None


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


def _session(
    source: BinaryIO, sink: BinaryIO, on_save: Callable[[Path, Path], str | SavePrompt | None],
) -> int:
    root = None
    ready = False
    shutdown = False
    pending: tuple[str, SavePrompt] | None = None
    while (payload := _frame(source)) is not None:
        if not payload:
            continue
        try:
            message = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant,
            )
        except (ValueError, RecursionError):
            _error(sink, None, -32700, "Parse error")
            continue

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            _error(sink, None, -32600, "Invalid request")
            continue
        request_id = message.get("id")
        if "method" not in message:
            if pending is None or request_id != pending[0]:
                continue
            answer = _prompt_answer(message)
            if answer is None:
                continue
            _, prompt = pending
            pending = None
            if answer:
                try:
                    notice = prompt.accept()
                except Exception:
                    return _save_failed(sink)
                if notice:
                    _log(sink, notice)
            continue

        is_request = "id" in message
        method = message.get("method")
        if (request_id is not None and type(request_id) not in {int, str}) or not isinstance(method, str):
            _error(sink, None, -32600, "Invalid request")
            continue

        if method == "exit" and not is_request:
            return 0 if shutdown else 1
        if shutdown:
            if is_request:
                _error(sink, request_id, -32600, "Server already stopped")
            continue
        if method == "initialize" and is_request:
            if root is not None:
                _error(sink, request_id, -32600, "Server already initialized")
                continue
            root = _workspace_root(message.get("params"))
            if root is None:
                _error(sink, request_id, -32602, "A local workspace directory is required")
                continue
            _send(sink, id=request_id, result={"capabilities": _SAVE_CAPABILITIES})
        elif root is None:
            if is_request:
                _error(sink, request_id, -32002, "Server not initialized")
        elif method == "initialized" and not is_request:
            ready = True
        elif method == "shutdown" and is_request:
            shutdown = True
            pending = None
            _send(sink, id=request_id, result=None)
        elif method == "textDocument/didSave" and not is_request and ready and pending is None:
            path = _save_path(message.get("params"), root)
            if path is None:
                continue
            try:
                notice = on_save(root, path)
            except Exception:
                return _save_failed(sink)
            if isinstance(notice, SavePrompt):
                prompt_id = f"zed-transfer-consent-{token_hex(16)}"
                pending = (prompt_id, notice)
                _send(sink, id=prompt_id, method="window/showMessageRequest", params={
                    "type": 2,
                    "message": notice.message,
                    "actions": [{"title": _ACCEPT_UPLOAD}, {"title": _CANCEL_UPLOAD}],
                })
            elif notice:
                _log(sink, notice)
        elif is_request:
            _error(sink, request_id, -32601, "Method not found")
    return 0


def run_lsp(
    on_save: Callable[[Path, Path], str | SavePrompt | None], *,
    stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
) -> int:
    """Run a save-only LSP stdio session and close on EOF or framing error."""
    source = stdin if stdin is not None else sys.stdin.buffer
    sink = stdout if stdout is not None else sys.stdout.buffer
    try:
        return _session(source, sink, on_save)
    except (_InvalidFrame, OSError):
        return 1
