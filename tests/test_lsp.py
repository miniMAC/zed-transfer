import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zed_transfer.lsp import SavePrompt, run_lsp


_PROMPT_ID = "zed-transfer-consent-" + "a" * 32
_ACCEPT = {"title": "Attiva upload automatico"}


def _packet(payload):
    return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload


def _message(method, **fields):
    return _packet(json.dumps({"jsonrpc": "2.0", "method": method, **fields}).encode())


def _save(uri):
    return _message("textDocument/didSave", params={"textDocument": {"uri": uri}})


def _response(request_id=_PROMPT_ID, **fields):
    return _packet(json.dumps({"jsonrpc": "2.0", "id": request_id, **fields}).encode())


def _responses(output):
    stream = io.BytesIO(output)
    responses = []
    while header := stream.readline():
        length = int(header.split(b":", 1)[1])
        assert stream.readline() == b"\r\n"
        responses.append(json.loads(stream.read(length)))
    return responses


class LspTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.uploads = []

    def on_save(self, root, file):
        self.uploads.append((root, file))
        return "Salvataggio ricevuto."

    def run_packets(self, *packets, on_save=None):
        destination = io.BytesIO()
        with patch("zed_transfer.lsp.token_hex", return_value="a" * 32):
            status = run_lsp(on_save or self.on_save, stdin=io.BytesIO(b"".join(packets)), stdout=destination)
        return status, _responses(destination.getvalue())

    def initialize(self):
        return _message("initialize", id=1, params={"rootUri": self.root.as_uri()})

    def test_only_did_save_after_initialize_triggers_callback(self):
        file = self.root / "file con spazi # è.php"
        uri = file.as_uri()
        status, responses = self.run_packets(
            self.initialize(),
            _save(uri),
            _message("initialized"),
            _message("textDocument/didChange", params={"textDocument": {"uri": uri}}),
            _save(uri),
            _message("shutdown", id=2),
            _save(uri),
            _message("exit"),
        )
        self.assertEqual(status, 0)
        self.assertEqual(self.uploads, [(self.root, file)])
        sync = responses[0]["result"]["capabilities"]["textDocumentSync"]
        self.assertEqual(sync, {"openClose": False, "change": 0, "save": {"includeText": False}})
        self.assertEqual(responses[1]["method"], "window/logMessage")
        self.assertEqual(responses[2], {"id": 2, "jsonrpc": "2.0", "result": None})

    def test_rejects_remote_mutated_and_outside_uris(self):
        root_uri = self.root.as_uri()
        status, _ = self.run_packets(
            self.initialize(),
            _message("initialized"),
            _save(f"{root_uri}/%2e%2e/outside.php"),
            _save(self.root.with_name(f"{self.root.name}-sibling").as_uri() + "/outside.php"),
            _save(f"{root_uri}/file%0a.php"),
            _save(f"{root_uri}/file.php?"),
            _save(f"{root_uri}/file%2.php"),
            _save(f"file://example.org{self.root}/file.php"),
            _save(f"https://example.org{self.root}/file.php"),
            _save(root_uri),
        )
        self.assertEqual(status, 0)
        self.assertEqual(self.uploads, [])

    def test_rejects_nonlocal_root(self):
        _, responses = self.run_packets(
            _message("initialize", id=1, params={"rootUri": "file://somewhere.example/root"}),
        )
        self.assertEqual(responses[0]["error"]["code"], -32602)

    def test_accepts_one_workspace_folder_when_root_uri_is_missing(self):
        status, responses = self.run_packets(
            _message("initialize", id=1, params={
                "workspaceFolders": [{"uri": self.root.as_uri() + "/", "name": "test"}],
            }),
            _message("initialized"),
            _save((self.root / "index.php").as_uri()),
        )
        self.assertEqual(status, 0)
        self.assertIn("result", responses[0])
        self.assertEqual(self.uploads, [(self.root, self.root / "index.php")])

    def test_malformed_frame_ends_without_running_callback(self):
        for invalid in (
            b"Content-Length: 99999999\r\n\r\n",
            b"Content-Length: 200\r\n\r\n{}",
            b"Content-Length: 2\r\ncontent-length: 10\r\n\r\n{}",
            b"X-Too-Long: " + b"a" * 9000 + b"\r\n",
        ):
            with self.subTest(header=invalid[:45]):
                self.assertEqual(self.run_packets(invalid), (1, []))
        self.assertEqual(self.uploads, [])

    def test_drains_large_frame_and_processes_next_save(self):
        file = self.root / "index.php"
        status, responses = self.run_packets(
            self.initialize(),
            _message("initialized"),
            _message("textDocument/didOpen", params={"textDocument": {
                "uri": file.as_uri(), "text": "a" * (1024 * 1024),
            }}),
            _save(file.as_uri()),
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(responses), 2)
        self.assertEqual(self.uploads, [(self.root, file)])

    def test_can_recover_from_a_malformed_json_body(self):
        status, responses = self.run_packets(
            _packet(b'{"method":"textDocument/didSave","method":"other"}'),
            self.initialize(),
        )
        self.assertEqual(status, 0)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertIn("result", responses[1])

    def test_prompt_accepts_only_one_matching_response_with_exact_action(self):
        file = self.root / "index.php"
        uri = file.as_uri()
        prompts = []

        def ask(root, path):
            prompts.append(path)
            return SavePrompt("Consenti invii a sftp.example.com?", lambda: self.on_save(root, path))

        status, responses = self.run_packets(
            self.initialize(),
            _message("initialized"),
            _save(uri),
            _save(uri),
            _response("wrong-id", result=_ACCEPT),
            _response(result={"title": "Attiva"}),
            _response(result=_ACCEPT, error={"code": -32800, "message": "Canceled"}),
            _response(result=_ACCEPT),
            _response(result=_ACCEPT),
            _message("shutdown", id=2),
            _message("exit"),
            on_save=ask,
        )
        self.assertEqual(status, 0)
        self.assertEqual(prompts, [file])
        self.assertEqual(self.uploads, [(self.root, file)])
        self.assertEqual(responses[1], {
            "jsonrpc": "2.0", "id": _PROMPT_ID, "method": "window/showMessageRequest",
            "params": {"type": 2, "message": "Consenti invii a sftp.example.com?",
                       "actions": [_ACCEPT, {"title": "Annulla"}]},
        })
        self.assertEqual(responses[2]["method"], "window/logMessage")

    def test_prompt_cancellation_never_calls_accept(self):
        file = self.root / "index.php"
        uri = file.as_uri()
        for response in (
            _response(result=None),
            _response(result={"title": "Annulla"}),
            _response(error={"code": -32800, "message": "Canceled"}),
        ):
            with self.subTest(response=response):
                prompts = []

                def ask(root, path):
                    prompts.append(path)
                    if len(prompts) == 1:
                        return SavePrompt("Attivare?", lambda: self.on_save(root, path))
                    return None

                status, _ = self.run_packets(
                    self.initialize(), _message("initialized"), _save(uri), response,
                    _response(result=_ACCEPT), _save(uri), on_save=ask,
                )
                self.assertEqual(status, 0)
                self.assertEqual(prompts, [file, file])
        self.assertEqual(self.uploads, [])

    def test_shutdown_discards_prompt(self):
        status, responses = self.run_packets(
            self.initialize(),
            _message("initialized"),
            _save((self.root / "index.php").as_uri()),
            _message("shutdown", id=2),
            _response(result=_ACCEPT),
            _message("exit"),
            on_save=lambda root, file: SavePrompt("Attivare?", lambda: self.on_save(root, file)),
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(responses), 3)
        self.assertEqual(self.uploads, [])
