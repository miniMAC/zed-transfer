import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zed_transfer.lsp import MenuPrompt, MenuResult, SavePrompt, TransferResult, UploadJob, run_lsp


_PROMPT_ID = "zed-transfer-consent-" + "a" * 32
_ACCEPT = {"title": "Attiva upload automatico"}
_PROGRESS_ID = "zed-transfer-upload-" + "a" * 32
_MENU_ID = "zed-transfer-menu-" + "a" * 32
_MENU_COMMAND = "zed-transfer.config"


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

    def run_packets(self, *packets, on_save=None, menu=None, tokens=()):
        destination = io.BytesIO()
        token_values = iter(tokens)
        with patch("zed_transfer.lsp.token_hex", side_effect=lambda size: next(token_values, "a" * 32)):
            status = run_lsp(
                on_save or self.on_save, menu=menu, stdin=io.BytesIO(b"".join(packets)), stdout=destination,
            )
        return status, _responses(destination.getvalue())

    def initialize(self, **window):
        return _message("initialize", id=1, params={
            "rootUri": self.root.as_uri(), "capabilities": {"window": window},
        })

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

    def _job(self, root, path, *, success=True):
        def transfer():
            self.uploads.append((root, path))
            detail = "Caricato" if success else "Caricamento non riuscito"
            return TransferResult(f"{detail} {path.name}", success)
        return UploadJob(path.name, transfer)

    def test_upload_progress_waits_for_matching_ack_and_reports_success(self):
        file = self.root / "[index].php"
        status, responses = self.run_packets(
            self.initialize(workDoneProgress=True), _message("initialized"), _save(file.as_uri()),
            _response("wrong", result=None), _response(_PROGRESS_ID, result=None),
            _response(_PROGRESS_ID, result=None), on_save=self._job,
        )
        self.assertEqual(status, 0)
        self.assertEqual(self.uploads, [(self.root, file)])
        self.assertEqual([item["method"] for item in responses[1:]], [
            "window/logMessage", "window/workDoneProgress/create", "$/progress", "$/progress",
            "window/logMessage", "window/showMessage",
        ])
        self.assertEqual(responses[2]["id"], _PROGRESS_ID)
        self.assertEqual([responses[3]["params"]["value"]["kind"], responses[4]["params"]["value"]["kind"]],
                         ["begin", "end"])
        self.assertEqual(responses[-1]["params"], {"type": 3, "message": r"✅ Caricato \[index\]\.php"})

    def test_refused_or_unsupported_progress_still_reports_failed_transfer(self):
        file = self.root / "index.php"
        for supported in (False, True):
            with self.subTest(workDoneProgress=supported):
                refusal = (_response(_PROGRESS_ID, error={"code": -32601, "message": "Not supported"}),)
                _, responses = self.run_packets(
                    self.initialize(workDoneProgress=supported), _message("initialized"), _save(file.as_uri()),
                    *(refusal if supported else ()),
                    on_save=lambda root, path: self._job(root, path, success=False),
                )
                self.assertNotIn("$/progress", [item.get("method") for item in responses])
                self.assertEqual(responses[-1]["params"]["type"], 1)
                self.assertEqual(responses[-2]["params"]["type"], 1)
        self.assertEqual(len(self.uploads), 2)

    def test_pending_progress_coalesces_repeated_saves_and_shutdown_drops_unstarted_job(self):
        first, second, third = (self.root / f"{name}.php" for name in ("first", "second", "third"))
        second_token = "zed-transfer-upload-" + "b" * 32
        third_token = "zed-transfer-upload-" + "c" * 32
        status, _ = self.run_packets(
            self.initialize(workDoneProgress=True), _message("initialized"), _save(first.as_uri()),
            _save(second.as_uri()), _save(second.as_uri()), _save(third.as_uri()),
            _response(_PROGRESS_ID, result=None), _response(second_token, result=None),
            _message("shutdown", id=2), _response(third_token, result=None), _message("exit"),
            on_save=self._job, tokens=("a" * 32, "b" * 32, "c" * 32),
        )
        self.assertEqual(status, 0)
        self.assertEqual(self.uploads, [(self.root, first), (self.root, second)])

    def test_progress_ack_timeout_runs_job_without_progress_even_if_input_is_ready(self):
        file = self.root / "index.php"
        with patch("zed_transfer.lsp.monotonic", side_effect=(0.0, 5.0)):
            _, responses = self.run_packets(
                self.initialize(workDoneProgress=True), _message("initialized"), _save(file.as_uri()),
                _response(_PROGRESS_ID, result=None), on_save=self._job,
            )
        self.assertEqual(self.uploads, [(self.root, file)])
        self.assertNotIn("$/progress", [item.get("method") for item in responses])
        self.assertEqual(responses[-1]["method"], "window/showMessage")

    def test_code_action_and_codelens_open_same_scoped_menu(self):
        file = self.root / "index.php"
        file.write_text("data")
        roots, choices = [], []

        def menu(root):
            roots.append(root)
            return MenuPrompt("Scegli", ("Apri", "Annulla"), select)

        def select(choice):
            choices.append(choice)
            return MenuResult("File aperto", file)

        document = {"uri": file.as_uri()}
        status, responses = self.run_packets(
            self.initialize(showDocument={"support": True}), _message("initialized"),
            _message("textDocument/codeAction", id=2, params={"textDocument": document}),
            _message("textDocument/codeLens", id=3, params={"textDocument": document}),
            _message("textDocument/codeAction", id=4, params={
                "textDocument": document, "context": {"triggerKind": 2},
            }),
            _message("workspace/executeCommand", id=5, params={"command": _MENU_COMMAND, "arguments": ["/tmp"]}),
            _message("workspace/executeCommand", id=6, params={"command": _MENU_COMMAND}),
            _save(file.as_uri()),
            _response(_MENU_ID, result={"title": "not offered"}), _response(_MENU_ID, result={"title": "Apri"}),
            _response(_MENU_ID, result={"title": "Apri"}), menu=menu, on_save=self._job,
        )
        self.assertEqual(status, 0)
        self.assertEqual(roots, [self.root])
        self.assertEqual(choices, ["Apri"])
        self.assertEqual(self.uploads, [(self.root, file)])
        identifiers = {item.get("id"): item for item in responses}
        command = identifiers[2]["result"][0]["command"]
        self.assertEqual(command["command"], _MENU_COMMAND)
        self.assertEqual(identifiers[3]["result"][0]["command"], command)
        self.assertEqual(identifiers[4]["result"], [])
        self.assertIn("error", identifiers[5])
        self.assertEqual(responses[-1]["method"], "window/showDocument")
        self.assertEqual(responses[-1]["params"]["uri"], file.resolve().as_uri())
