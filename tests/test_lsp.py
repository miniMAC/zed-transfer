import io
import json
import tempfile
import unittest
from pathlib import Path

from zed_transfer.lsp import run_lsp


def _packet(payload):
    return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload


def _message(method, **fields):
    return _packet(json.dumps({"jsonrpc": "2.0", "method": method, **fields}).encode())


def _save(uri):
    return _message("textDocument/didSave", params={"textDocument": {"uri": uri}})


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

    def run_packets(self, *packets):
        destination = io.BytesIO()
        status = run_lsp(self.on_save, stdin=io.BytesIO(b"".join(packets)), stdout=destination)
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
