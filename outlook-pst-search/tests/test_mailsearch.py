import hashlib
import http.client
import io
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from unittest.mock import MagicMock, patch
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mailsearch.api_settings import ApiSettings, user_dpapi
from mailsearch.answer import LocalAI, delivery_date_answer, validate
from mailsearch.embedding import EmbeddingAPI, GatewayRequestError, tls_context, warn_unverified
from mailsearch.external import ExternalAI, Excerpt, Reviews, pcf_batches
from mailsearch.extract import convert_legacy, extract_attachment
from mailsearch.intranet import CHAT_PATH, IntranetAI, LOCAL_EMBEDDING
from mailsearch.indexer import Indexer
from mailsearch.ollama_setup import (InstallCommandError, install_ollama, package_progress,
                                    pull_model, run_install_command, winget_binary)
from mailsearch.server import App, gateway_defaults, gateway_diagnostics
from mailsearch.store import Store
from tools.demo import generate


GATEWAY_ORIGIN = "https://gateway.example.invalid"
CHAT_URL = GATEWAY_ORIGIN + CHAT_PATH
EMBEDDING_ENDPOINT = GATEWAY_ORIGIN + "/v1/embeddings"
GATEWAY_ENV = {
    "MAILSEARCH_GATEWAY_URL": GATEWAY_ORIGIN,
    "MAILSEARCH_GATEWAY_ANSWER_MODELS": "answer-model-a,answer-model-b",
    "MAILSEARCH_GATEWAY_TLS_VERIFY": "1",
    "MAILSEARCH_GATEWAY_CA_FILE": "",
}


def await_source(store, source, states, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = store.source(source)
        if (row["state"] if row else None) in states:
            return row
        time.sleep(.02)
    raise AssertionError(f"Unexpected import state: {dict(row) if row else None}")


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_message({"models": [{"name": "test:latest"}, {"name": "embed:latest"}, {"name": "bge-m3:latest"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            self.send_message({"details": {"family": "test"}})
            return
        if self.path == "/api/embed":
            self.send_message({"embeddings": [[1.0, .05, .12] for _ in payload["input"]]})
            return
        context = json.loads(payload["prompt"])
        quote = "요청 주신 4월 25일 납품으로 최종 승인합니다."
        approval = next((item for item in context["evidence"] if quote in item["text"]), None)
        result = {"status": "insufficient", "claims": []}
        if approval:
            result = {"status": "answered", "claims": [
                {"text": "최종 승인 납품일은 4월 25일입니다.", "citations": [{"source": approval["source"], "quote": quote}]},
            ]}
        self.send_message({"response": json.dumps(result, ensure_ascii=False)})

    def send_message(self, payload):
        content = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
        cls.fixture_thread = threading.Thread(target=cls.model.serve_forever, daemon=True)
        cls.fixture_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.model.shutdown()
        cls.model.server_close()
        cls.fixture_thread.join()

    def setUp(self):
        self.enterContext(patch.dict(os.environ, GATEWAY_ENV))
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        root = Path(self.workspace.name)
        self.mail = root / "archives"
        generate(self.mail)
        self.store = Store(root / "index")
        self.ai = LocalAI("test", "embed", self.model.server_port)
        self.indexer = Indexer(self.store, ai=self.ai)
        self.addCleanup(self.indexer.stop)
        self.app = App(0, self.store, self.indexer, self.ai)
        worker = threading.Thread(target=self.app.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(self.app.shutdown)

    def call(self, route, payload=None, token=True, headers=None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if token:
            request_headers["X-Mail-Session"] = self.app.token
        connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=8)
        try:
            connection.request("POST" if payload is not None else "GET", route, json.dumps(payload) if payload is not None else None, request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def import_mail(self):
        status, payload = self.call("/api/sources", {"path": str(self.mail)})
        self.assertEqual(status, 202)
        source = payload["source"]
        row = await_source(self.store, source, {"ready", "error"})
        self.assertEqual(row["state"], "ready")
        self.assertIsNone(row["issue"])
        self.assertEqual((row["done"], row["omitted"]), (4, 1))
        return source

    def test_gateway_settings_probes_use_one_key_and_review_the_chosen_answer_model(self):
        source = self.import_mail()
        selections = {"answer": "answer-model-a", "embedding": "bge-m3", "rerank": "bge-reranker-v2-m3"}
        private = "synthetic-session-key"
        self.assertEqual(self.call("/api/intranet/settings", token=False)[0], 403)
        self.assertEqual(self.call("/api/intranet/settings", {"action": "apply", "key": private,
            "selections": {**selections, "answer": "outside-catalog"}})[0], 400)
        with patch("mailsearch.embedding.http.client.HTTPSConnection") as https:
            request = https.return_value.request
            response = https.return_value.getresponse.return_value
            response.status = 200

            def reply(method, path, body, headers):
                self.assertEqual(method, "POST")
                self.assertEqual(headers["Authorization"], "Bearer " + private)
                data = json.loads(body)
                model = data["model"]
                if path == "/v1/embeddings":
                    self.assertEqual(model, "bge-m3")
                    value = {"data": [{"index": 0, "embedding": [1.0, 0.05, 0.12]}]}
                elif path == "/v1/rerank":
                    self.assertEqual(model, "bge-reranker-v2-m3")
                    self.assertEqual(len(data["documents"]), data["top_n"])
                    value = {"results": [{"index": i, "relevance_score": i}
                                         for i in range(data["top_n"])]}
                else:
                    self.assertEqual(path, "/v1/chat/completions")
                    self.assertEqual(model, "answer-model-a")
                    content = '```json\n{"ok": true}\n```'
                    value = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
                response.read.return_value = json.dumps(value).encode()

            request.side_effect = reply
            status, test = self.call("/api/intranet/settings", {"action": "test", "key": private, "selections": selections})
            self.assertEqual(status, 200)
            self.assertEqual(test, {"checks": {role: {"ok": True} for role in selections}})
            self.assertFalse(self.call("/api/intranet/settings")[1]["configured"])
            status, applied = self.call("/api/intranet/settings", {"action": "apply", "key": private, "selections": selections})
            self.assertEqual(status, 200)
            self.assertTrue(applied["active_answer"])
            self.assertNotIn(private, json.dumps(applied))
            request.reset_mock()
            query = {"query": "A-17 납기", "filters": {"source": source}}
            status, preview = self.call("/api/external/preview", query)
            self.assertEqual(status, 200)
            self.assertEqual((preview["destination"], preview["model"]), (CHAT_URL, "answer-model-a"))
            self.assertTrue(preview["excerpts"])
            self.assertIn("/v1/rerank", [args.args[1] for args in request.call_args_list])
            self.assertNotIn("/v1/chat/completions", [args.args[1] for args in request.call_args_list])
            request.reset_mock()
            status, _ = self.call("/api/intranet/settings", {"action": "apply", "selections": {
                **selections, "answer": "answer-model-b"}})
            self.assertEqual(status, 200)
            status, _ = self.call("/api/external/send", {"review": preview["review"],
                                                      "excerpts": [preview["excerpts"][0]["id"]]})
            self.assertEqual(status, 409)
            request.assert_not_called()
            status, new_review = self.call("/api/external/preview", query)
            self.assertEqual(status, 200)
            self.assertEqual(new_review["model"], "answer-model-b")
            request.reset_mock(side_effect=True)
            response.read.return_value = json.dumps({"choices": [{"message": {
                "content": '{"status":"insufficient","claims":[]}'
            }}]}).encode()
            status, answer = self.call("/api/external/send", {"review": new_review["review"],
                                                           "excerpts": [new_review["excerpts"][0]["id"]]})
            self.assertEqual((status, answer["status"]), (200, "insufficient"))
            request.assert_called_once()
            _, path, body, _ = request.call_args.args
            self.assertEqual((path, json.loads(body)["model"]), ("/v1/chat/completions", "answer-model-b"))

    def test_dashboard_saves_gateway_settings_and_restores_the_key(self):
        route = "/api/intranet/settings"
        self.app.api_settings = ApiSettings(self.store.root)
        profile = {
            "gateway_url": "https://configured.example.invalid/v1",
            "answer_models": "dashboard-model-a, dashboard-model-b",
            "ca_file": "/local/company-ca.pem",
            "key": "dashboard-fixture-key",
            "selections": {"answer": "dashboard-model-b", "embedding": "bge-m3", "rerank": ""},
        }
        def protected(data, *, decrypt=False):
            return zlib.decompress(data) if decrypt else zlib.compress(data)

        with (patch.object(ApiSettings, "secure", True),
              patch("mailsearch.api_settings.user_dpapi", side_effect=protected),
              patch.dict(os.environ, {"MAILSEARCH_GATEWAY_URL": "", "MAILSEARCH_GATEWAY_ANSWER_MODELS": ""})):
            status, saved = self.call(route, {"action": "apply", **profile})
            self.assertEqual(status, 200)
            self.assertNotIn(profile["key"], json.dumps(saved))
            self.assertNotIn(profile["key"].encode(), self.app.api_settings.path.read_bytes())
            self.assertTrue(saved["persistent_keys"])
            self.assertEqual(saved["gateway_url"], "https://configured.example.invalid")
            self.assertEqual(saved["selections"], profile["selections"])
            self.assertEqual(self.ai.embedding_index, "https://configured.example.invalid/v1/embeddings#bge-m3")
            self.assertEqual(self.app.external.tls.ca_file, profile["ca_file"])
            self.assertTrue(self.app.external.tls.verify)
            new_origin = {**profile, "key": "", "gateway_url": "https://changed.example.invalid"}
            self.assertEqual(self.call(route, {"action": "apply", **new_origin})[1]["error"], "external_key_required")
            self.assertEqual(self.call(route, {"action": "apply", **profile, "test_unverified": True})[0], 400)

            restored_ai = LocalAI("test")
            with App(0, self.store, self.indexer, restored_ai, api_settings=ApiSettings(self.store.root)) as restored:
                self.assertEqual(restored.external.model, "dashboard-model-b")
                self.assertEqual(restored.external._key, profile["key"])
                self.assertTrue(restored_ai.embedding_api.tls.verify)
                self.assertEqual(restored.gateway_fields["answer_models"], "dashboard-model-a,dashboard-model-b")
            self.assertEqual(self.call(route, {"action": "disconnect"})[0], 200)
            self.assertFalse(self.app.api_settings.load()["gateway"]["key"])

    def test_gateway_probes_distinguish_local_model_tls_http_and_response_errors(self):
        route = "/api/intranet/settings"
        roles = {"answer": "", "embedding": LOCAL_EMBEDDING, "rerank": "bge-reranker-v2-m3"}
        payload = {"action": "test", "key": "test-key", "selections": roles}
        with (patch.object(self.ai, "embed_local", side_effect=RuntimeError("local_model_missing")),
              patch.object(IntranetAI, "rerank", side_effect=ssl.SSLCertVerificationError("untrusted fixture"))):
            status, result = self.call(route, payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["checks"], {
            "embedding": {"ok": False, "reason": "local_model_missing"},
            "rerank": {"ok": False, "reason": "tls_certificate"},
        })

        roles["embedding"] = "bge-m3"
        with (patch.object(IntranetAI, "embed", side_effect=GatewayRequestError(403)),
              patch.object(IntranetAI, "rerank", side_effect=ValueError("rerank_response"))):
            status, result = self.call(route, payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["checks"], {
            "embedding": {"ok": False, "http_status": 403},
            "rerank": {"ok": False, "reason": "invalid_response"},
        })

    def test_ollama_setup_checks_before_any_download_and_installs_on_request(self):
        route = "/api/ollama/setup"
        self.assertEqual(self.call(route, token=False)[0], 403)
        self.assertEqual(self.call(route, {"action": "install"}, token=False)[0], 403)
        self.assertEqual(self.call(route, {"action": "other"})[0], 400)

        def await_setup():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, state = self.call(route)
                if state["phase"] == "idle":
                    return state
                time.sleep(.02)
            self.fail("Ollama setup did not finish")

        with (patch.object(self.ai, "ensure_server", side_effect=OSError("ollama_not_found")) as ensure_server,
              patch.object(self.ai, "ensure_model") as ensure_model,
              patch.object(self.ai, "response") as model_response,
              patch("mailsearch.ollama_setup.ollama_binary", return_value=None) as binary,
              patch("mailsearch.ollama_setup.winget_binary", return_value="fixture-winget"),
              patch("mailsearch.ollama_setup.install_ollama") as install):
            self.assertEqual(self.call(route, {"action": "check"})[0], 202)
            self.assertEqual(await_setup(), {"phase": "idle", "installed": False, "running": False,
                                            "model_present": None, "can_install": True, "error": "", "progress": None})
            ensure_server.assert_called_once_with()
            install.assert_not_called()
            model_response.assert_not_called()

            def installed(_winget, report):
                report(42)
                self.assertEqual(self.app.ollama_setup.snapshot()["progress"], 42)
                binary.return_value = "fixture-ollama"
                ensure_server.side_effect = None

            install.side_effect = installed
            ensure_model.side_effect = [RuntimeError("local_model_missing"), "bge-m3:latest"]
            model_response.return_value.__enter__.return_value = io.BytesIO(b'{"status":"success"}\n')
            self.assertEqual(self.call(route, {"action": "install"})[0], 202)
            self.assertEqual(await_setup(), {"phase": "idle", "installed": True, "running": True,
                                            "model_present": True, "can_install": False, "error": "", "progress": None})
            install.assert_called_once_with("fixture-winget", self.app.ollama_setup.report_progress)
            model_response.assert_called_once_with("/api/pull", {"model": "bge-m3", "stream": True}, timeout=1800)

            ensure_model.side_effect = RuntimeError("local_model_missing")
            self.assertEqual(self.call(route, {"action": "check"})[0], 202)
            self.assertEqual(await_setup(), {"phase": "idle", "installed": True, "running": True,
                                            "model_present": False, "can_install": True, "error": "", "progress": None})
            install.assert_called_once()
            model_response.assert_called_once()

    def test_gateway_preset_button_creates_defaults_from_the_private_seed(self):
        self.app.gateway_preset = Path(self.workspace.name) / "gateway-defaults.json"
        seed = self.app.gateway_preset.with_suffix(".seed.json")
        route = "/api/intranet/defaults"
        preset = {"gateway_url": "https://test-preset.example.invalid/v1", "answer_models": "chat-a, chat-b",
                  "test_unverified": True}
        with patch.dict(os.environ, {"MAILSEARCH_GATEWAY_URL": ""}):
            self.assertEqual(self.call(route, {})[0], 404)
            self.assertEqual(self.call(route, {}, token=False)[0], 403)
            self.assertEqual(self.call(route, {"preset": preset})[0], 400)
            seed.write_text(json.dumps({**preset, "key": "forbidden"}), encoding="utf-8")
            self.assertEqual(self.call(route, {})[0], 400)
            self.assertFalse(self.app.gateway_preset.exists())
            seed.write_text(json.dumps(preset), encoding="utf-8")
            self.assertFalse(gateway_defaults(self.app.gateway_preset)["gateway_url"])
            status, values = self.call(route, {})
            self.assertEqual(status, 200)
            self.assertEqual(values, {**preset, "gateway_url": "https://test-preset.example.invalid",
                                      "answer_models": "chat-a,chat-b", "ca_file": ""})
            self.assertEqual(json.loads(self.app.gateway_preset.read_bytes()), values)
            seed.unlink()
            self.assertEqual(self.call(route, {}), (200, values))
            active = self.call("/api/intranet/settings")[1]
            self.assertEqual(active["gateway_url"], GATEWAY_ORIGIN)
            self.assertFalse(active["test_unverified"])

    def test_external_settings_are_session_scoped_and_do_not_echo_key(self):
        route = "/api/external/settings"
        status, initial = self.call(route)
        self.assertEqual(status, 200)
        self.assertIsNone(initial["configured"])
        self.assertEqual(initial["defaults"]["model"], "gpt-4.1-mini")
        options = {"destination": "https://valid.example.invalid/v1/chat/completions",
                   "model": "test-model", "key": "placeholder-token"}
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            self.assertEqual(self.call(route, {"action": "apply", **options}, token=False)[0], 403)
            status, applied = self.call(route, {"action": "apply", **options})
            self.assertEqual(status, 200)
            self.assertNotIn("placeholder-token", json.dumps(applied))
            self.assertEqual(self.call(route)[1]["configured"], applied["configured"])
            self.assertEqual(self.call("/api/sources")[1]["external"], applied["configured"])
            https.assert_not_called()
            same = {**options, "key": "", "model": "second"}
            self.assertEqual(self.call(route, {"action": "apply", **same})[0], 200)
            self.assertEqual(self.call(route, {"action": "apply", **same, "destination":
                                               "https://another.example.invalid/v1/chat/completions"})[1]["error"],
                             "external_key_required")
            self.assertEqual(self.call(route, {"action": "disconnect"})[1], {"configured": None})
            self.assertIsNone(self.call(route)[1]["configured"])
            https.assert_not_called()

    def test_external_trial_only_uses_a_fixed_message_and_leaves_configuration_unset(self):
        route = "/api/external/settings"
        options = {"action": "test", "destination": "https://trial.example.invalid/chat/completions",
                   "model": "test", "key": "placeholder-token", "query": "private search"}
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            con = https.return_value
            reply = con.getresponse.return_value
            reply.status = 401
            self.assertEqual(self.call(route, options)[1]["error"], "external_test_auth")
            reply.status = 200
            reply.read.return_value = b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'
            self.assertEqual(self.call(route, options), (200, {"ok": True}))
            sent = json.loads(con.request.call_args.args[2])
            self.assertEqual(sent["messages"][1]["content"], "Connection test")
            self.assertNotIn("private search", json.dumps(sent))
            self.assertIsNone(self.call(route)[1]["configured"])

    def test_external_settings_change_invalidates_outstanding_review(self):
        self.import_mail()
        route = "/api/external/settings"
        current = {"action": "apply", "destination": "https://first.example.invalid/chat/completions",
                   "model": "test", "key": "placeholder-token"}
        self.assertEqual(self.call(route, current)[0], 200)
        _, preview = self.call("/api/external/preview", {"query": "한빛 A-17"})
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            self.assertEqual(self.call(route, {**current, "model": "different"})[0], 200)
            status, response = self.call("/api/external/send", {"review": preview["review"], "excerpts": [1]})
            self.assertEqual((status, response["error"]), (409, "external_review_expired"))
            https.assert_not_called()

    def test_folder_registration_imports_contained_pst(self):
        folder = Path(self.workspace.name) / "pst-folder"
        folder.mkdir()
        (folder / "보관.pst").write_bytes(b"!BDN\x00\x00\x00\x00SM\x00\x00")
        (folder / "깨진.pst").write_bytes(b"not-a-pst-header")

        def fake_export(pst, stage, event):
            message = EmailMessage()
            message["From"] = "a@example.invalid"
            message["To"] = "b@example.invalid"
            message["Subject"] = "폴더 PST 시험"
            message["Message-ID"] = "<folder-pst@example.invalid>"
            message.set_content("폴더 안 PST에서 꺼낸 메일입니다.")
            message.add_attachment("공급가 320000원.".encode(), maintype="text", subtype="plain", filename="견적.txt")
            output = stage / "eml"
            output.mkdir(parents=True)
            (output / "1").write_bytes(message.as_bytes())
            (stage / "export-complete").touch()
            return output

        with patch.object(Indexer, "export", side_effect=fake_export):
            status, payload = self.call("/api/sources", {"path": str(folder)})
            self.assertEqual(status, 202)
            row = await_source(self.store, payload["source"], {"ready", "error", "empty"})
            self.assertEqual(row["state"], "ready")
            self.assertEqual(row["omitted"], 1)  # the unreadable PST is skipped, not fatal
        status, found = self.call("/api/search", {"query": "폴더 안 PST에서", "filters": {}})
        self.assertEqual(status, 200)
        self.assertTrue(found["results"])
        self.assertTrue(found["results"][0]["folder"].startswith("/보관.pst/"))
        status, attachment = self.call("/api/search", {"query": "320000원", "filters": {}})
        self.assertEqual(status, 200)
        self.assertTrue(attachment["results"])

    def test_refresh_reindexes_and_embeds_only_new_mail(self):
        source = self.import_mail()
        with self.store.connection() as conn:
            vectors_before = conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        extra = EmailMessage()
        extra["From"] = "new@example.invalid"
        extra["To"] = "buy@example.invalid"
        extra["Subject"] = "증분 색인 시험"
        extra["Message-ID"] = "<incremental@example.invalid>"
        extra.set_content("새로 도착한 증분 색인 대상 메일입니다.")
        (self.mail / "incremental.eml").write_bytes(extra.as_bytes())
        self.call("/api/source-action", {"source": source, "action": "refresh"})
        updated = await_source(self.store, source, {"ready", "error"})
        self.assertEqual((updated["state"], updated["done"]), ("ready", 5))
        with self.store.connection() as conn:
            vectors_after = conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        # The four unchanged mails keep their embeddings; only the new mail is embedded.
        self.assertEqual(vectors_after - vectors_before, 1)
        self.assertTrue(self.call("/api/search", {"query": "증분 색인 대상", "filters": {}})[1]["results"])

    def test_folder_refresh_carries_unchanged_pst_without_reexport(self):
        folder = Path(self.workspace.name) / "pst-folder"
        folder.mkdir()
        (folder / "보관.pst").write_bytes(b"!BDN\x00\x00\x00\x00SM\x00\x00")
        (folder / "깨진.pst").write_bytes(b"not-a-pst-header")
        exports = []

        def fake_export(pst, stage, event):
            exports.append(pst.name)
            message = EmailMessage()
            message["From"] = "a@example.invalid"
            message["To"] = "b@example.invalid"
            message["Subject"] = "폴더 PST 시험"
            message["Message-ID"] = "<folder-pst@example.invalid>"
            message.set_content("폴더 안 PST에서 꺼낸 메일입니다.")
            output = stage / "eml"
            output.mkdir(parents=True)
            (output / "1").write_bytes(message.as_bytes())
            (stage / "export-complete").touch()
            return output

        with patch.object(Indexer, "export", side_effect=fake_export):
            status, payload = self.call("/api/sources", {"path": str(folder)})
            source = payload["source"]
            row = await_source(self.store, source, {"ready", "error", "empty"})
            self.assertEqual((row["state"], row["done"], row["omitted"]), ("ready", 1, 1))
            listed = self.call("/api/sources")[1]["sources"]
            self.assertEqual(next(item for item in listed if item["id"] == source)["updates"], [])
            direct = EmailMessage()
            direct["From"] = "c@example.invalid"
            direct["To"] = "d@example.invalid"
            direct["Subject"] = "직접 추가한 메일"
            direct["Message-ID"] = "<direct@example.invalid>"
            direct.set_content("폴더 바로 아래에 넣은 메일입니다.")
            (folder / "direct.eml").write_bytes(direct.as_bytes())
            self.call("/api/source-action", {"source": source, "action": "refresh"})
            updated = await_source(self.store, source, {"ready", "error", "empty"})
            self.assertEqual((updated["state"], updated["done"], updated["omitted"]), ("ready", 2, 0))
            # The unchanged PST was carried over from the previous version, not re-exported.
            self.assertEqual(exports, ["보관.pst"])
            # A changed PST is listed as an update target until the next refresh.
            (folder / "보관.pst").write_bytes(b"!BDN\x00\x00\x00\x00SM\x00\x00appended")
            listed = self.call("/api/sources")[1]["sources"]
            self.assertEqual(next(item for item in listed if item["id"] == source)["updates"], ["보관.pst"])
            self.call("/api/source-action", {"source": source, "action": "refresh"})
            await_source(self.store, source, {"ready", "error", "empty"})
            listed = self.call("/api/sources")[1]["sources"]
            self.assertEqual(next(item for item in listed if item["id"] == source)["updates"], [])
            self.assertEqual(exports, ["보관.pst", "보관.pst"])
        self.assertEqual(set(self.store.pst_files(source)), {"보관.pst/", "깨진.pst/"})
        self.assertTrue(self.call("/api/search", {"query": "폴더 안 PST에서", "filters": {}})[1]["results"])
        self.assertTrue(self.call("/api/search", {"query": "직접 추가한", "filters": {}})[1]["results"])

    def test_gateway_indexes_existing_archive_and_searches_with_the_same_backend(self):
        source = self.import_mail()
        with self.store.connection() as conn:
            conn.execute("UPDATE vectors SET model='bge-m3' WHERE model='embed'")
            conn.execute("UPDATE vector_buckets SET model='bge-m3' WHERE model='embed'")
        self.ai.configure_embedding("", EmbeddingAPI("test-key"))

        def respond(_method, _path, body, _headers):
            payload = json.loads(body)
            self.assertEqual(payload["model"], "bge-m3")
            supplied = payload["input"]
            values = [supplied] if isinstance(supplied, str) else supplied
            response.read.return_value = json.dumps({"data": [
                {"index": index, "embedding": [1.0, 0.05, 0.12]}
                for index in reversed(range(len(values)))
            ]}).encode()

        with patch("mailsearch.embedding.http.client.HTTPSConnection") as https:
            connection = https.return_value
            response = connection.getresponse.return_value
            response.status = 200
            connection.request.side_effect = respond
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            self.assertIsNone(await_source(self.store, source, {"ready"})["issue"])
            with self.store.connection() as conn:
                models = {row[0] for row in conn.execute("SELECT DISTINCT model FROM vectors")}
                self.assertEqual(models, {"bge-m3", self.ai.embedding_index})
            batch = json.loads(connection.request.call_args_list[0].args[2])["input"]
            self.assertTrue(any("A-17" in passage for passage in batch))
            status, data = self.call("/api/search", {"query": "의미 검색 테스트", "filters": {"source": source}})
            self.assertEqual(status, 200)
            self.assertTrue(any("similarity" in result for result in data["results"]))
            method, path, body, headers = connection.request.call_args.args
            self.assertEqual((method, path), ("POST", "/v1/embeddings"))
            self.assertEqual(json.loads(body), {"model": "bge-m3", "input": "의미 검색 테스트"})
            self.assertEqual(headers["Authorization"], "Bearer test-key")
            self.assertEqual(https.call_args.args, ("gateway.example.invalid", 443))
            self.assertEqual(https.call_args.kwargs["timeout"], 6)

            response.status = 401
            status, data = self.call("/api/search", {"query": "A-17"})
            self.assertEqual(status, 200)
            self.assertTrue(data["results"])
            self.assertTrue(all("similarity" not in item for item in data["results"]))
            status, data = self.call("/api/sources")
            self.assertEqual(status, 200)
            self.assertEqual(data["embedding_endpoint"], EMBEDDING_ENDPOINT)
            self.assertNotIn("test-key", json.dumps(data))

    def test_settings_switch_bge_m3_between_local_and_api_indexes(self):
        route = "/api/intranet/settings"
        roles = {"answer": "", "rerank": "", "embedding": LOCAL_EMBEDDING}
        source = self.import_mail()
        refresh = {"source": source, "action": "refresh"}
        query = {"query": "의미 검색 테스트", "filters": {"source": source}}
        with (patch("mailsearch.embedding.http.client.HTTPSConnection") as https,
              patch.object(self.ai, "call", wraps=self.ai.call) as local_call):
            status, test = self.call(route, {"action": "test", "selections": roles})
            self.assertEqual((status, test), (200, {"checks": {"embedding": {"ok": True}}}))
            self.assertEqual(self.ai.embedding_index, "embed")
            status, result = self.call(route, {"action": "apply", "selections": roles})
            self.assertEqual(status, 200)
            self.assertEqual(result["selections"], roles)
            self.assertFalse(result["configured"])
            self.assertEqual(self.call("/api/source-action", refresh)[0], 202)
            self.assertIsNone(await_source(self.store, source, {"ready"})["issue"])
            self.assertTrue(any("similarity" in row for row in self.call("/api/search", query)[1]["results"]))
            self.assertEqual(local_call.call_args.args[:2], ("/api/embed", {
                "model": "bge-m3:latest", "input": [query["query"]], "truncate": True,
            }))
            https.assert_not_called()

            api_roles = {**roles, "embedding": "bge-m3"}
            self.assertEqual(self.call(route, {"action": "apply", "selections": api_roles})[0], 400)
            self.assertEqual(self.call(route, {"action": "apply", "selections": api_roles, "key": "test-key"})[0], 200)
            self.assertEqual(self.call("/api/sources")[1]["embedding_endpoint"], EMBEDDING_ENDPOINT)
            local_call.reset_mock()
            connection = https.return_value
            response = connection.getresponse.return_value
            response.status = 200

            def answer_embeddings(_method, _path, body, _headers):
                value = json.loads(body)
                self.assertEqual(value["model"], "bge-m3")
                count = len(value["input"]) if isinstance(value["input"], list) else 1
                response.read.return_value = json.dumps({"data": [
                    {"index": idx, "embedding": [1.0, 0.05, 0.12]} for idx in range(count)
                ]}).encode()

            connection.request.side_effect = answer_embeddings
            self.assertFalse(any("similarity" in row for row in self.call("/api/search", query)[1]["results"]))
            self.assertEqual(self.call("/api/source-action", refresh)[0], 202)
            self.assertIsNone(await_source(self.store, source, {"ready"})["issue"])
            self.assertTrue(any("similarity" in row for row in self.call("/api/search", query)[1]["results"]))
            local_call.assert_not_called()
            with self.store.connection() as conn:
                indexes = {row[0] for row in conn.execute("SELECT DISTINCT model FROM vectors")}
            self.assertTrue({"bge-m3", EMBEDDING_ENDPOINT + "#bge-m3"}.issubset(indexes))

            self.assertEqual(self.call(route, {"action": "apply", "selections": roles})[0], 200)
            https.reset_mock()
            self.assertTrue(any("similarity" in row for row in self.call("/api/search", query)[1]["results"]))
            https.assert_not_called()
            self.assertEqual(self.call(route, {"action": "disconnect"})[0], 200)
            self.assertEqual(self.call(route)[1]["selections"], roles)

    def test_ping_route_requires_session(self):
        self.assertEqual(self.call("/api/ping", token=False)[0], 403)
        self.assertEqual(self.call("/api/ping")[0], 200)

    def document_zip(self, documents):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            for path, data in documents.items():
                zipped.writestr(path, data)
        return buffer.getvalue()

    def test_document_folder_registration_opens_cells_and_sections(self):
        folder = Path(self.workspace.name) / "docs"
        folder.mkdir()
        (folder / "요구사항.txt").write_text("A17 브래킷 두께는 진공 성형 조건에서 4T로 확정한다.", encoding="utf-8")
        (folder / "양산BOM.xlsx").write_bytes(self.document_zip({
            "xl/workbook.xml": '<workbook xmlns:r="relationship"><sheets><sheet name="양산 BOM" r:id="sheet1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="sheet1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '<worksheet><row><c r="F18" t="inlineStr"><is><t>3T</t></is></c></row></worksheet>',
        }))
        status, payload = self.call("/api/sources", {"path": str(folder)})
        self.assertEqual(status, 202)
        row = await_source(self.store, payload["source"], {"ready", "error", "empty"})
        self.assertEqual((row["state"], row["done"]), ("ready", 2))
        found = self.call("/api/search", {"query": '"3T"', "filters": {}})[1]["results"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["attachment"], "양산BOM.xlsx")
        evidence = self.call(f"/api/evidence/{found[0]['passage']}")[1]
        self.assertEqual(evidence["location"], "양산 BOM!F18~F18")
        self.assertTrue(self.call("/api/search", {"query": "진공 성형 조건에서", "filters": {}})[1]["results"])
        # Refresh adopts unchanged documents and indexes only the new file.
        (folder / "개정.txt").write_text("전 모델 4T 적용 제안.", encoding="utf-8")
        self.call("/api/source-action", {"source": payload["source"], "action": "refresh"})
        updated = await_source(self.store, payload["source"], {"ready", "error", "empty"})
        self.assertEqual((updated["state"], updated["done"]), ("ready", 3))

    def test_single_document_file_registration(self):
        target = Path(self.workspace.name) / "기준서.txt"
        target.write_text("§4.2 공차는 ±0.05 mm.", encoding="utf-8")
        status, payload = self.call("/api/sources", {"path": str(target)})
        self.assertEqual(status, 202)
        row = await_source(self.store, payload["source"], {"ready", "error", "empty"})
        self.assertEqual((row["state"], row["done"]), ("ready", 1))
        found = self.call("/api/search", {"query": "공차", "filters": {}})[1]["results"]
        self.assertTrue(found)
        evidence = self.call(f"/api/evidence/{found[0]['passage']}")[1]
        self.assertIn("±0.05", evidence["text"])

    def mixed_corpus(self):
        """A mixed corpus: one mail plus three documents."""
        folder = Path(self.workspace.name) / "design"
        folder.mkdir()
        (folder / "A17 시험보고서.txt").write_text(
            "A17 브래킷 진동 시험 결과. 12쪽: 변형 발생 최대 0.18 mm, 허용 값 0.20 mm.", encoding="utf-8")
        approved = EmailMessage()
        approved["From"] = "review@example.invalid"
        approved["To"] = "design@example.invalid"
        approved["Subject"] = "Re: A17 브래킷 두께 변경 승인"
        approved["Date"] = "03 May 2026 10:00:00 +0900"
        approved["Message-ID"] = "<approve-a17@example.invalid>"
        approved.set_content("A17 브래킷 두께 변경, 시제품만 4T 적용을 승인합니다.")
        (folder / "승인.eml").write_bytes(approved.as_bytes())
        (folder / "양산BOM RevC.xlsx").write_bytes(self.document_zip({
            "xl/workbook.xml": '<workbook xmlns:r="relationship"><sheets><sheet name="양산 BOM" r:id="sheet1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="sheet1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '<worksheet><row><c r="F18" t="inlineStr"><is><t>3T</t></is></c></row></worksheet>',
        }))
        (folder / "개정초안.docx").write_bytes(self.document_zip({
            "word/document.xml": "<document><p><r><t>A17 전 모델 4T 적용 제안 초안입니다.</t></r></p></document>"}))
        return folder

    def test_search_tab_scopes_mail_and_documents(self):
        """메일 탭은 문서를, 문서 탭은 메일을 검색하지 않는다."""
        self.import_mail()
        folder = self.mixed_corpus()
        _, payload = self.call("/api/sources", {"path": str(folder)})
        await_source(self.store, payload["source"], {"ready", "error", "empty"})
        query = {"query": "A17", "filters": {"tab": "mail"}}
        _, mail = self.call("/api/search", query)
        self.assertTrue(mail["results"])
        self.assertTrue(all(row["kind"] == "mail" for row in mail["results"]))
        self.assertFalse(any("시험보고서" in row["subject"] for row in mail["results"]))
        _, documents = self.call("/api/search", {"query": "A17", "filters": {"tab": "document"}})
        self.assertTrue(documents["results"])
        self.assertTrue(all(row["kind"] == "document" for row in documents["results"]))
        self.assertTrue(any("시험보고서" in row["subject"] for row in documents["results"]))

    def test_pcf_tab_classifies_scopes_and_instructs(self):
        """파일명에 PCF가 들어간 선주 교신문은 PCF 탭으로 분리되고 전용 지침으로 답변한다."""
        folder = Path(self.workspace.name) / "pcf"
        folder.mkdir()
        (folder / "PCF-H2107-045.txt").write_text(
            "선주 COMMENT: 좌현 발러스트 배관 밸브 등급 상향 요구. H21070 사례 참조. "
            "빌더 회신: 규정서 기준 LR 등급으로 검토했고 현 설계 유지. 종결: 선주 동의로 Closed.", encoding="utf-8")
        (folder / "배관 기준서.txt").write_text("발러스트 배관 등급 기준 안내.", encoding="utf-8")
        _, payload = self.call("/api/sources", {"path": str(folder)})
        await_source(self.store, payload["source"], {"ready", "error", "empty"})
        _, pcf = self.call("/api/search", {"query": "밸브 등급", "filters": {"tab": "pcf"}})
        self.assertTrue(pcf["results"])
        self.assertTrue(all(row["kind"] == "pcf" for row in pcf["results"]))
        self.assertTrue(all("PCF" in row["subject"] for row in pcf["results"]))
        _, documents = self.call("/api/search", {"query": "배관", "filters": {"tab": "document"}})
        self.assertTrue(documents["results"])
        self.assertTrue(all(row["kind"] == "document" for row in documents["results"]))
        self.assertFalse(any("PCF" in row["subject"] for row in documents["results"]))
        # PCF 전용 범위(호선)는 파일명·본문 텍스트 매칭으로 좁힌다(데이터화 전 임시 동작).
        _, other_hull = self.call("/api/search", {"query": "", "filters": {"tab": "pcf", "hull": "H9999"}})
        self.assertFalse(other_hull["results"])
        _, mentioned_hull = self.call("/api/search", {"query": "", "filters": {"tab": "pcf", "hull": "H21070"}})
        self.assertFalse(mentioned_hull["results"])
        _, this_hull = self.call("/api/search", {"query": "", "filters": {"tab": "pcf", "hull": "H2107"}})
        self.assertTrue(any("PCF-H2107" in row["subject"] for row in this_hull["results"]))
        self.app.external = ExternalAI("https://api.example.invalid/v1/chat/completions",
                                       "test-model", "placeholder-token")
        _, preview = self.call("/api/pcf/preview", {"query": "밸브 등급 상향 COMMENT 처리", "filters": {"tab": "document"}})
        self.assertTrue(preview["results"])
        self.assertTrue(all(row["kind"] == "pcf" for row in preview["results"]))
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            connection = https.return_value
            reply = connection.getresponse.return_value
            reply.status = 200
            reply.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps({
                "status": "answered",
                "claims": [{"text": "과거 동일 COMMENT은 현 설계 유지로 종결되었습니다.",
                            "citations": [{"source": preview["excerpts"][0]["id"],
                                           "quote": "현 설계 유지. 종결: 선주 동의로 Closed."}]}],
            }, ensure_ascii=False)}}]}).encode()
            status, data = self.call("/api/pcf/send",
                {"review": preview["review"], "excerpts": [item["id"] for item in preview["excerpts"]]})
            self.assertEqual(status, 200)
            body = connection.request.call_args[0][2]
            if isinstance(body, str):
                body = body.encode()
            self.assertIn("선주 교신문(PCF)".encode() if isinstance(body, bytes) else "선주 교신문(PCF)", body)
        self.assertEqual(data["status"], "answered")

    def test_folder_document_body_filter_and_removed_original(self):
        path = self.mixed_corpus()
        source = self.call("/api/sources", {"path": str(path)})[1]["source"]
        await_source(self.store, source, {"ready", "error"})
        filters = {"tab": "all", "source": source, "body": "변형 발생"}
        rows = self.call("/api/search", {"filters": filters})[1]["results"]
        self.assertEqual([row["subject"] for row in rows], ["A17 시험보고서.txt"])
        self.app.external = ExternalAI("https://api.example.invalid/v1/chat/completions", "model", "key")
        preview = self.call("/api/external/preview", {"query": "변형 발생", "filters": filters})[1]
        self.assertEqual(preview["excerpts"][0]["source"]["passage"], rows[0]["passage"])
        (path / rows[0]["subject"]).unlink()
        self.assertFalse(self.call("/api/search", {"filters": filters})[1]["results"])
        self.assertEqual(self.call(f"/api/evidence/{rows[0]['passage']}")[0], 404)
        self.assertEqual(self.call("/api/external/send", {"review": preview["review"], "excerpts": [1]})[0], 409)

    def test_pcf_comment_boundaries_and_route_bound_review(self):
        path = Path(self.workspace.name) / "PCF-H2107.txt"
        path.write_text("COMMENT NO. 101\n배관압력 상향 요청.\nCOMMENT NO. 102\n도장색 변경 종결.", encoding="utf-8")
        source = self.call("/api/sources", {"path": str(path)})[1]["source"]
        await_source(self.store, source, {"ready", "error"})
        row = self.store.lexical("배관압력", {"tab": "pcf"})[0]
        self.assertIn("101", row["text"])
        self.assertNotIn("102", row["text"])
        self.app.external = ExternalAI("https://api.example.invalid/v1/chat/completions", "model", "key")
        review = self.call("/api/pcf/preview", {"query": "배관압력"})[1]["review"]
        with patch("mailsearch.external.ExternalAI.answer") as generate:
            self.assertEqual(self.call("/api/external/send", {"review": review, "excerpts": [1]})[0], 409)
            generate.assert_not_called()

    def word_pcf(self, *, repeat=False, unresolved=False):
        from xml.sax.saxutils import escape

        def cell(text="", prop=""):
            return f"<w:tc><w:tcPr>{prop}</w:tcPr><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:tc>"

        def row(*cells):
            return "<w:tr>" + "".join(cells) + "</w:tr>"

        head = row(cell("No."), cell("Owner", '<w:gridSpan w:val="2"/>'), cell("Yard", '<w:gridSpan w:val="2"/>'))
        first = row(cell("1", '<w:vMerge w:val="restart"/>'),
                    cell("a"), cell("검토 요청"), cell("a"), cell("Draft response"))
        second = row(cell(prop="<w:vMerge/>"), cell("b"), cell("접근공간 확인"), cell("b"), cell())
        tail = "추가 설명 " * 150 + "Not closed. Please revise the method."
        third = row(cell(prop="<w:vMerge/>"), cell("c"), cell(tail), cell("c"), cell("Revision pending"))
        other = row(cell("1" if repeat else "2"), cell("a"), cell("다른 건"), cell("a"), cell("Spurious response"))
        orphan = row(cell(), cell("d"), cell("연결 안 된 기타 사항"), cell("d"), cell("Orphan"))
        text = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        text += '<w:p><w:r><w:t>Rev.: B</w:t></w:r></w:p>'
        text += "<w:tbl>" + head + first + second + third + (orphan if unresolved else "") + "</w:tbl>"
        text += "<w:tbl>" + head + other + "</w:tbl></w:body></w:document>"
        path = Path(self.workspace.name) / "PCF-H2107.docx"
        path.write_bytes(self.document_zip({"word/document.xml": text}))
        source = self.call("/api/sources", {"path": str(path)})[1]["source"]
        self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        return path, {"source": source, "tab": "pcf", "hull": "H2107"}

    def test_word_pcf_resolves_case_and_sends_reviewed_cells(self):
        path, filters = self.word_pcf()
        _, result = self.call("/api/search", {"query": "접근공간 확인", "filters": filters})
        seed = next(row for row in result["results"] if "No. 1" in row["location"])
        _, evidence = self.call("/api/evidence/" + str(seed["passage"]))
        self.assertEqual(evidence["pcf"]["revision"], "B")
        self.assertEqual([r["label"] for r in evidence["pcf"]["cells"]], list("aabbcc"))
        self.assertEqual(evidence["pcf"]["cells"][3]["text"], "")
        self.assertTrue(evidence["pcf"]["cells"][4]["text"].endswith("Please revise the method."))
        self.assertNotIn("Spurious", json.dumps(evidence["pcf"]))
        self.app.external = ExternalAI("https://intranet.example.invalid/v1/chat/completions", "model", "key")
        payload = {"query": "1-b의 처리 경과", "filters": filters, "passage": seed["passage"]}
        self.assertNotIn("review", self.call("/api/pcf/preview", payload)[1])
        with patch.dict(os.environ, {"MAILSEARCH_PCF_INTERNAL_URL": self.app.external.public["destination"],
                                    "MAILSEARCH_PCF_INPUT_BYTES": "12000"}):
            preview = self.call("/api/pcf/preview", payload)[1]
            self.assertEqual((preview["pcf_coverage"]["expected"], len(preview["excerpts"])), (6, 6))
            with patch.object(self.app.external, "complete") as complete:
                quote = "Not closed. Please revise the method."
                complete.return_value = {"status": "answered", "unknowns": [], "claims": [
                    {"text": "선주는 방법 수정을 요청했습니다.", "citations": [{"source": 5, "quote": quote}]},
                    {"text": "선주가 종결했습니다.", "citations": [{"source": 5, "quote": quote}]},
                    {"text": "조선소가 방법 수정을 요청했습니다.", "citations": [{"source": 5, "quote": quote}]},
                ]}
                status, reply = self.call("/api/pcf/send", {"review": preview["review"], "excerpts": [1, 2, 3, 4, 5]})
                self.assertEqual(status, 200)
                self.assertEqual(reply["status"], "partial")
                self.assertEqual(len(reply["claims"]), 1)
                self.assertEqual(reply["claims"][0]["citations"][0]["location"], "PCF T1R2 · No. 1 · T1R4C3 · OWNER")
                sent = json.loads(complete.call_args.args[1])
                self.assertEqual([item["source"] for item in sent["evidence"]], [1, 2, 3, 4, 5])
                self.assertEqual(sent["evidence"][3]["text"], "")
                self.assertFalse(sent["coverage"]["complete"])
                self.assertEqual(reply["pcf_coverage"]["included"], 5)
                self.assertNotIn(path.name, complete.call_args.args[1])

            preview = self.call("/api/pcf/preview", payload)[1]
            path.write_bytes(path.read_bytes() + b"changed")
            with patch.object(self.app.external, "complete") as complete:
                self.assertEqual(self.call("/api/pcf/send", {"review": preview["review"], "excerpts": [1]})[0], 409)
                complete.assert_not_called()

    def test_word_pcf_combines_long_comment_with_original_citations(self):
        _, filters = self.word_pcf()
        self.app.external = ExternalAI("https://intranet.example.invalid/v1/chat/completions", "model", "key")
        with patch.dict(os.environ, {"MAILSEARCH_PCF_INTERNAL_URL": self.app.external.public["destination"],
                                    "MAILSEARCH_PCF_INPUT_BYTES": "4700"}):
            _, preview = self.call("/api/pcf/preview", {"query": "1번 경과", "filters": filters})
            self.assertGreater(preview["pcf_coverage"]["calls"], 2)
            sent = []

            def generate(instructions, content, **kwargs):
                payload = json.loads(content)
                sent.append(payload)
                for item in payload["evidence"]:
                    if "Please revise the method." in item["text"]:
                        return {"status": "answered", "unknowns": [], "claims": [{
                            "text": "선주가 방법 수정을 요청했습니다.",
                            "citations": [{"source": item["source"], "quote": "Please revise the method."}],
                        }]}
                return {"status": "insufficient", "unknowns": [], "claims": []}

            with patch.object(self.app.external, "complete", side_effect=generate):
                status, result = self.call("/api/pcf/send", {"review": preview["review"],
                                           "excerpts": [item["id"] for item in preview["excerpts"]]})
            self.assertEqual(status, 200)
            self.assertEqual(result["pcf_coverage"]["included"], 6)
            self.assertTrue(result["pcf_coverage"]["synthesized"])
            self.assertIn("verified_findings", sent[-1])
            self.assertEqual(result["claims"][0]["citations"][0]["passage"], preview["excerpts"][4]["source"]["passage"])
            self.assertTrue(all(item["text"] in preview["excerpts"][4]["text"] for item in sent[-1]["evidence"]))

    def test_word_pcf_repeated_numbers_and_orphan_rows(self):
        _, filters = self.word_pcf(repeat=True, unresolved=True)
        self.app.external = ExternalAI("https://intranet.example.invalid/v1/chat/completions", "model", "key")
        question = {"query": "1번 처리 이력", "filters": filters}
        with patch.dict(os.environ, {"MAILSEARCH_PCF_INTERNAL_URL": self.app.external.public["destination"],
                                    "MAILSEARCH_PCF_INPUT_BYTES": "12000"}):
            _, found = self.call("/api/pcf/preview", question)
            self.assertTrue(found["choose_case"])
            self.assertEqual(len(found["results"]), 2)
            seed = next(row for row in found["results"] if row["location"].startswith("PCF T1"))
            _, preview = self.call("/api/pcf/preview", {**question, "passage": seed["passage"]})
        self.assertEqual(len(preview["excerpts"]), 6)
        self.assertNotIn("Orphan", json.dumps(preview))
        self.assertIn("연결 확인 필요", preview["pcf_coverage"]["warnings"][0])

    def test_sources_report_kind_counts(self):
        """사이드바 섹션 구분을 위한 소스별 종류 카운트."""
        self.import_mail()
        folder = self.mixed_corpus()
        _, payload = self.call("/api/sources", {"path": str(folder)})
        await_source(self.store, payload["source"], {"ready", "error", "empty"})
        rows = self.call("/api/sources")[1]["sources"]
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[payload["source"]]["counts"], {"mail": 1, "document": 3})
        mail_only = next(row for row in rows if row["id"] != payload["source"])
        self.assertEqual(list(mail_only["counts"]), ["mail"])
        self.assertGreater(mail_only["counts"]["mail"], 0)

    def test_reviewed_intranet_context_and_legacy_limit(self):
        path = self.mail / "history.txt"
        tail = "검수 결과는 적합으로 최종 확정했습니다."
        path.write_text("PX-421 검수기록 " + "가" * 900 + tail)
        source = self.call("/api/sources", {"path": str(path)})[1]["source"]
        await_source(self.store, source, {"ready"})
        url = "https://intranet.example.invalid/v1/chat/completions"
        self.app.external = ExternalAI(url, "model", "key")
        query = {"query": "PX-421 검수기록", "filters": {"source": source}}
        legacy = self.call("/api/external/preview", query)[1]
        self.assertFalse(any(tail in item["text"] for item in legacy["excerpts"]))
        with patch.dict(os.environ, MAILSEARCH_INTERNAL_URL=url):
            status, wide = self.call("/api/external/preview", query)
            self.assertEqual(status, 200)
            chosen = next(item for item in wide["excerpts"] if tail in item["text"])
            self.assertEqual(chosen["context"]["subject"], path.name)
            with patch.object(self.app.external, "complete", return_value={"status": "insufficient"}) as wire:
                self.assertEqual(self.call("/api/external/send", {"review": wide["review"], "excerpts": [chosen["id"]]})[0], 200)
                sent = json.loads(wire.call_args.args[1])
                self.assertEqual(sent["evidence"], [{"source": chosen["id"], "text": chosen["text"], "context": chosen["context"]}])
                self.assertNotIn(str(self.mail), wire.call_args.args[1])

    def test_internal_review_finds_decision_at_end_of_linked_reply(self):
        self.ai.configure_embedding("", None)
        folder = self.mail / "chain"
        folder.mkdir()
        marker = "계약 갱신 대상을 전 지점으로 확정했습니다."
        for i, body in enumerate(["ZX-144 계약 갱신 검토", "회의 기록 " * 700 + marker]):
            mail = EmailMessage()
            mail["From"] = "team@example.invalid"
            mail["Subject"] = "ZX-144 계약 갱신" if not i else "후속 회신"
            mail["Message-ID"] = f"<chain{i}@example.invalid>"
            if i:
                mail["In-Reply-To"] = "<chain0@example.invalid>"
            mail.set_content(body)
            (folder / f"{i}.eml").write_bytes(mail.as_bytes())
        source = self.call("/api/sources", {"path": str(folder)})[1]["source"]
        await_source(self.store, source, {"ready"})
        url = "https://intranet.example.invalid/v1/messages"
        self.app.external = ExternalAI(url, "model", "key")
        with patch.dict(os.environ, MAILSEARCH_INTERNAL_URL=url):
            _, review = self.call("/api/external/preview", {"query": "ZX-144", "filters": {"source": source}})
            tail = next(item for item in review["excerpts"] if marker in item["text"])
            self.assertEqual(tail["context"]["subject"], "후속 회신")
        with patch.object(self.app.external, "complete") as wire:
            status, _ = self.call("/api/external/send", {"review": review["review"], "excerpts": [tail["id"]]})
            self.assertEqual(status, 409)
            wire.assert_not_called()

    def test_intranet_sheet_context_and_more_than_five_reviewed_sources(self):
        book = self.mail / "ledger.xlsx"
        rows = ["<row><c r='A1' t='inlineStr'><is><t>가격표</t></is></c></row>",
                "<row><c r='A2' t='inlineStr'><is><t>단가 (만원)</t></is></c></row>"]
        for i in range(3, 14):
            rows.append(f"<row><c r='A{i}' t='inlineStr'><is><t>INV-925 항목 {i}</t></is></c></row>")
        with zipfile.ZipFile(book, "w") as archive:
            archive.writestr("xl/workbook.xml", '<workbook xmlns:r="rel"><sheet name="예산" r:id="s"/></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><r Id="s" Target="worksheets/s.xml"/></Relationships>')
            archive.writestr("xl/worksheets/s.xml", "<worksheet>" + "".join(rows) + "</worksheet>")
        source = self.call("/api/sources", {"path": str(book)})[1]["source"]
        await_source(self.store, source, {"ready"})
        url = "https://intranet.example.invalid/v1/messages"
        self.app.external = ExternalAI(url, "model", "key")
        with patch.dict(os.environ, MAILSEARCH_INTERNAL_URL=url):
            _, preview = self.call("/api/external/preview", {"query": "INV-925 항목", "filters": {"source": source}})
            items = preview["excerpts"]
            self.assertTrue(any("만원" in item["text"] for item in items))
            self.assertTrue(any("가격표" in item["text"] for item in items))
            self.assertGreater(len(items), 5)
            approval = {"review": preview["review"], "excerpts": [item["id"] for item in items]}
            with patch.object(self.app.external, "complete", return_value={"status": "insufficient"}) as wire:
                self.assertEqual(self.call("/api/external/send", approval)[0], 200)
                self.assertEqual(len(json.loads(wire.call_args.args[1])["evidence"]), len(items))
                self.assertLessEqual(len(wire.call_args.args[1].encode()), self.app.external.internal_budget)

    def test_semantic_limits_apply_inside_selected_archive(self):
        from mailsearch.store import buckets, pack_vector
        blob = pack_vector([1., 0.])
        with self.store.connection() as conn:
            for name, count in (("wanted", 1), ("noise", 2410)):
                folder = self.mail / name
                folder.mkdir()
                source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)", (str(folder),)).lastrowid
                if name == "wanted":
                    wanted = source
                for number in range(count):
                    ident = self.store.add_mail(conn, source, 1, str(number), {
                        "subject": "same", "sender": name, "recipient": "", "sent": "", "folder": "/",
                        "attachments": [], "message_id": "", "reply_ids": "", "body": "shared"})
                    pid = conn.execute("SELECT id FROM passages WHERE mail=?", (ident,)).fetchone()[0]
                    conn.execute("INSERT INTO vectors VALUES(?,'test',?)", (pid, blob))
                    conn.executemany("INSERT INTO vector_buckets VALUES('test',?,?)", [(b, pid) for b in buckets([1., 0.])])
        for filters in ({"source": wanted}, {"sender": "wanted"}):
            result = self.store.semantic([1., 0.], "test", filters)
            self.assertEqual([row["source"] for row in result], [wanted])
        result = self.store.semantic([1., 0.], "test", {"source": source})
        self.assertTrue(result)
        self.assertTrue(all(row["source"] == source for row in result))

    def test_external_review_sends_only_selected_excerpt_once(self):
        self.import_mail()
        question = "한빛 A-17 납품일은 언제 확정됐어?"
        query = {"query": question, "context": "이전 질문은 장비 구매 정보입니다."}
        self.assertEqual(self.call("/api/external/preview", query)[0], 403)
        self.app.external = ExternalAI("https://api.openai.com/v1/chat/completions", "test-model", "placeholder-token")
        target = "요청 주신 4월 25일 납품으로 최종 승인합니다."
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            self.assertEqual(self.call("/api/answer", query)[1]["status"], "answered")
            status, preview = self.call("/api/external/preview", query)
            self.assertEqual(status, 200)
            self.assertGreater(len(preview["excerpts"]), 1)
            self.assertEqual(preview["question"], question)
            self.assertNotIn("placeholder-token", json.dumps(self.call("/api/sources")[1]))
            https.assert_not_called()
            chosen = next(item for item in preview["excerpts"] if target in item["text"])
            approval = {"review": preview["review"], "excerpts": [chosen["id"]], "query": "ignored"}
            self.assertEqual(self.call("/api/external/send", approval, token=False)[0], 403)
            https.assert_not_called()
            connection = https.return_value
            reply = connection.getresponse.return_value
            reply.status = 200
            result = {"status": "answered", "claims": [
                {"text": "최종 승인 납품일은 4월 25일입니다.", "citations": [{"source": chosen["id"], "quote": target}]},
            ]}
            reply.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode()
            status, data = self.call("/api/external/send", approval)
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "answered")
            self.assertEqual(data["provider"], "external")
            source = data["claims"][0]["citations"][0]["passage"]
            self.assertIn(target, self.call(f"/api/evidence/{source}")[1]["text"])
            method, path, body, headers = connection.request.call_args.args
            self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
            self.assertEqual(headers["Authorization"], "Bearer placeholder-token")
            sent = json.loads(body)
            self.assertFalse(sent["store"])
            self.assertEqual(sent["model"], "test-model")
            user_data = json.loads(sent["messages"][1]["content"])
            self.assertEqual(user_data, {"question": question, "evidence": [{"source": chosen["id"], "text": chosen["text"]}]})
            self.assertNotIn("buy@example.invalid", sent["messages"][1]["content"])
            self.assertEqual(self.call("/api/external/send", approval)[0], 409)
            self.assertEqual(connection.request.call_count, 1)

    def test_external_review_refuses_changed_archive_and_unreviewed_selection(self):
        self.import_mail()
        self.app.external = ExternalAI("https://gateway.example.invalid/v1/chat/completions", "test-model", "placeholder-token")
        query = {"query": "한빛 A-17"}
        _, bad = self.call("/api/external/preview", query)
        _, stale = self.call("/api/external/preview", query)
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            self.assertEqual(self.call("/api/external/send", {"review": bad["review"], "excerpts": [99]})[0], 400)
            self.mail.rename(self.mail.with_name("moved"))
            self.assertEqual(self.call("/api/external/send", {"review": stale["review"], "excerpts": [1]})[1]["error"],
                             "external_source_changed")
            https.assert_not_called()

    def test_fast_and_attachment_search(self):
        source = self.import_mail()
        status, data = self.call("/api/search", {"query": "A-17", "filters": {"sender": "buy@example.invalid"}})
        self.assertEqual(status, 200)
        self.assertIn("a-17", data["results"][0]["matched"])
        snippets = [row["snippet"] for row in data["results"] if row.get("snippet")]
        self.assertTrue(snippets)
        self.assertTrue(all("a-17" in snippet.casefold() for snippet in snippets))
        self.assertTrue(any(row.get("similarity") is not None for row in data["results"]))
        subjects = {row["subject"] for row in data["results"]}
        self.assertTrue({"한빛 A-17 출하 일정", "Re: 한빛 A-17 출하 일정"} <= subjects)
        whole = self.call("/api/search", {"query": '"A-17"', "filters": {}})[1]["results"]
        with_files = self.call("/api/search", {"query": '"A-17"', "filters": {"attachment": "yes"}})[1]["results"]
        without_files = self.call("/api/search", {"query": '"A-17"', "filters": {"attachment": "no"}})[1]["results"]
        self.assertEqual(len(with_files) + len(without_files), len(whole))
        self.assertTrue(all(row["attachment"] for row in with_files))
        self.assertFalse(any(row["attachment"] for row in without_files))
        self.assertTrue(self.call("/api/search", {"query": "납품", "filters": {"body": "승인"}})[1]["results"])
        status, data = self.call("/api/search", {"query": "냉각판", "filters": {"recipient": "hanbit@"}})
        self.assertEqual(status, 200)
        quoted = next(row for row in data["results"] if row["attachment"] == "B-19_견적.txt")
        _, evidence = self.call(f"/api/evidence/{quoted['passage']}")
        self.assertIn("320000원", evidence["text"])
        self.assertEqual(evidence["attachments"][1]["status"], "미지원 형식")
        self.assertEqual(self.call("/api/search", {"query": "납품", "filters": {"after": "2027-01-01"}})[1]["results"], [])
        self.assertEqual(self.call("/api/search", {"query": 123})[0], 400)

    def test_quoted_exact_search_with_and_or(self):
        self.import_mail()
        exact = self.call("/api/search", {"query": '"A-17"', "filters": {}})[1]["results"]
        self.assertEqual(len(exact), 3)
        self.assertTrue(all("A-17" in row["subject"] + row["preview"] for row in exact))
        both = self.call("/api/search", {"query": '"A-17" & "승인"', "filters": {}})[1]["results"]
        self.assertEqual(len(both), 1)
        self.assertIn("승인", both[0]["preview"])
        either = self.call("/api/search", {"query": '"반도체" or "승인"', "filters": {}})[1]["results"]
        self.assertEqual(len(either), 2)

    def test_grounded_question_followup_and_revocation(self):
        source = self.import_mail()
        query = {"query": "한빛 건 자재 늦어서 밀린 거, 결국 언제로 정해졌지?"}
        self.assertGreater(len(self.call("/api/candidates", query)[1]["results"]), 0)
        status, data = self.call("/api/answer", query)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "answered")
        self.assertEqual(data["retrieval"], "hybrid")
        citation = data["claims"][0]["citations"][0]
        path = f"/api/evidence/{citation['passage']}"
        self.assertIn(citation["quote"], self.call(path)[1]["body"])
        self.assertEqual(self.call("/api/answer", {"query": "그 뒤에 누가 승인했어?", "context": query["query"]})[1]["status"], "answered")
        self.mail.rename(self.mail.with_name("hidden"))
        self.assertEqual(self.call(path)[0], 404)
        self.assertFalse(self.call("/api/search", {"query": "한빛"})[1]["results"])
        self.assertEqual(self.call("/api/source-action", {"source": source, "action": "remove"})[0], 202)
        await_source(self.store, source, {None})
        with self.store.connection() as conn:
            for table in ("mails", "terms", "passages", "attachments", "vectors", "vector_buckets"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_refresh_preserves_version_and_updates_evidence(self):
        source = self.import_mail()
        _, first = self.call("/api/search", {"query": "승인"})
        old = first["results"][0]["passage"]
        (self.mail / "approved.eml").unlink()
        _, again = self.call("/api/sources", {"path": str(self.mail)})
        self.assertEqual(again["source"], source)
        self.assertEqual(self.call(f"/api/evidence/{old}")[0], 200)
        self.call("/api/source-action", {"source": source, "action": "refresh"})
        updated = await_source(self.store, source, {"ready", "error"})
        self.assertEqual(updated["done"], 3)
        self.assertEqual(self.call(f"/api/evidence/{old}")[0], 404)
        status, data = self.call("/api/answer", {"query": "한빛 A-17 승인일이 언제야?"})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "insufficient")
        self.assertFalse(data["claims"])

    def test_pause_and_resume_preserve_completed_work(self):
        entered, released = threading.Event(), threading.Event()
        index_headers = self.indexer.index_headers

        def staged(*args, **kwargs):
            index_headers(*args, **kwargs)
            entered.set()
            if not released.wait(5):
                raise RuntimeError("Pause test did not release indexing")

        self.indexer.index_headers = staged
        source = self.indexer.register(self.mail)
        self.assertTrue(entered.wait(5))
        self.indexer.command(source, "pause")
        self.assertEqual(self.store.source(source)["done"], 4)
        self.assertTrue(self.call("/api/search", {"query": "A-17"})[1]["results"])
        released.set()
        deadline = time.monotonic() + 5
        while source in self.indexer.events and time.monotonic() < deadline:
            time.sleep(.01)
        self.indexer.index_headers = index_headers
        self.indexer.command(source, "resume")
        latest = await_source(self.store, source, {"ready", "error"})
        self.assertEqual((latest["state"], latest["done"], latest["omitted"]), ("ready", 4, 1))

    def test_keyword_works_if_local_model_stops(self):
        self.import_mail()
        self.ai.model = "missing"
        status, answer = self.call("/api/answer", {"query": "한빛 A-17"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["status"], "model_unavailable")
        self.assertTrue(self.call("/api/search", {"query": "A-17"})[1]["results"])

    def test_browser_origin_and_session(self):
        self.assertEqual(self.call("/api/sources", token=False)[0], 403)
        self.assertEqual(self.call("/api/sources", headers={"Origin": "https://external.example.invalid"})[0], 403)
        self.assertEqual(self.call("/api/sources", headers={"Host": "localhost"})[0], 403)
        self.assertEqual(self.call("/api/sources", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_pst_zero_export_is_not_marked_searchable(self):
        pst = self.mail / "empty.pst"
        pst.write_bytes(b"!BDN" + bytes(4) + b"SM" + bytes(2))
        self.indexer.readpst = sys.executable
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = process.returncode = 0
        with patch("mailsearch.indexer.subprocess.Popen", return_value=process) as run:
            status, payload = self.call("/api/sources", {"path": str(pst)})
            self.assertEqual(status, 202)
            row = await_source(self.store, payload["source"], {"empty", "ready", "error"})
        self.assertEqual((row["state"], row["issue"], row["done"]), ("empty", "pst_no_mail", 0))
        self.assertIsNone(row["active"])
        self.assertFalse((Path(run.call_args.kwargs["cwd"]).parent / "export-complete").exists())
        self.assertEqual(run.call_args.args[0][-3:], ["-o", ".", str(pst)])

    def test_pst_numbered_exports_index_and_empty_refresh_keeps_previous_index(self):
        pst = self.mail / "archive.pst"
        pst.write_bytes(b"!BDN" + bytes(4) + b"SM" + bytes(2))
        self.indexer.readpst = sys.executable
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = process.returncode = 0

        def export_numbered(*args, cwd, **kwargs):
            (Path(cwd) / "1").write_bytes((self.mail / "approved.eml").read_bytes())
            return process

        with patch("mailsearch.indexer.subprocess.Popen", side_effect=export_numbered):
            _, payload = self.call("/api/sources", {"path": str(pst)})
            source = payload["source"]
            first = await_source(self.store, source, {"empty", "ready", "error"})
        self.assertEqual((first["state"], first["done"]), ("ready", 1))
        retry_at = time.monotonic() + 3
        while source in self.indexer.events and time.monotonic() < retry_at:
            time.sleep(.01)
        with patch("mailsearch.indexer.subprocess.Popen", return_value=process):
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            latest = await_source(self.store, source, {"empty", "ready", "error"})
        self.assertEqual((latest["state"], latest["active"]), ("empty", first["active"]))
        self.assertEqual(len(self.call("/api/search", {"query": "A-17", "filters": {}})[1]["results"]), 1)

    def test_wrong_file_and_legacy_empty_source_have_distinct_status(self):
        pst = self.mail / "fake.pst"
        pst.write_text("not a PST")
        _, payload = self.call("/api/sources", {"path": str(pst)})
        source = payload["source"]
        row = await_source(self.store, source, {"empty", "ready", "error"})
        self.assertEqual(row["issue"], "pst_invalid")
        self.indexer.stop()
        with self.store.connection() as conn:
            conn.execute("UPDATE sources SET state='ready',done=0,active=1,pending=NULL,issue=NULL WHERE id=?", (source,))
        reopened = Indexer(self.store)
        self.addCleanup(reopened.stop)
        row = self.store.source(source)
        self.assertEqual((row["state"], row["issue"], row["active"]), ("empty", "pst_no_mail", None))

    def test_empty_eml_folder_and_parse_failure_are_distinguished(self):
        path = self.mail / "archive-folder"
        path.mkdir()
        _, registered = self.call("/api/sources", {"path": str(path)})
        source = registered["source"]
        row = await_source(self.store, source, {"empty", "ready", "error"})
        self.assertEqual((row["state"], row["issue"]), ("empty", "eml_no_mail"))
        (path / "unreadable.eml").write_text("an invalid message")
        retry_at = time.monotonic() + 3
        while source in self.indexer.events and time.monotonic() < retry_at:
            time.sleep(.01)
        self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
        row = await_source(self.store, source, {"empty", "ready", "error"})
        self.assertEqual((row["state"], row["issue"], row["failed"]), ("error", "mail_parse_failed", 1))

    @unittest.skipUnless(os.getenv("MAILSEARCH_TEST_PST") and os.getenv("MAILSEARCH_TEST_READPST"), "optional public PST fixture")
    def test_real_pst_adapter_and_original_hash(self):
        path = Path(os.environ["MAILSEARCH_TEST_PST"])
        self.indexer.readpst = os.environ["MAILSEARCH_TEST_READPST"]
        original_digest = hashlib.sha256(path.read_bytes()).digest()
        source = self.indexer.register(path)
        indexed = await_source(self.store, source, {"ready", "empty", "error"})
        self.assertEqual(indexed["state"], "ready", indexed["issue"])
        self.assertGreater(indexed["done"], 0)
        self.assertEqual(indexed["failed"], 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), original_digest)
        self.assertTrue(self.call("/api/search", {"query": "", "filters": {}})[1]["results"])


@patch.dict(os.environ, GATEWAY_ENV)
class EmbeddingTransport(unittest.TestCase):
    def test_batch_response_is_ordered_and_requires_unique_indices(self):
        api = EmbeddingAPI("test-key")
        with patch("mailsearch.embedding.http.client.HTTPSConnection") as https:
            response = https.return_value.getresponse.return_value
            response.status = 200
            entries = [{"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}]
            response.read.return_value = json.dumps({"data": entries}).encode()
            self.assertEqual(api.embed(["first", "second"], timeout=10), [[1., 0.], [0., 1.]])
            entries[1]["index"] = 1
            response.read.return_value = json.dumps({"data": entries}).encode()
            with self.assertRaises(ValueError):
                api.embed(["first", "second"], timeout=10)


class OllamaProgress(unittest.TestCase):
    def test_winget_progress_uses_reported_percentage_or_size(self):
        self.assertEqual(package_progress("████    512 MB / 1.00 GB"), 50)
        self.assertEqual(package_progress("  32 %"), 32)
        self.assertEqual(package_progress("#####   42.8%"), 42)
        self.assertIsNone(package_progress("Installing…"))

    def test_model_progress_reports_download_and_success(self):
        ai = MagicMock()
        stream = ai.response.return_value.__enter__
        stream.return_value = io.BytesIO(
            b'{"status":"pulling manifest"}\n'
            b'{"digest":"layer-a","total":100,"completed":27}\n'
            b'{"digest":"layer-a","total":100,"completed":90}\n'
            b'{"digest":"layer-a","total":100,"completed":100}\n'
            b'{"status":"success"}\n'
        )
        reports = []
        pull_model(ai, reports.append)
        self.assertEqual(reports, [27, 90, 99, 100])
        stream.return_value = io.BytesIO(b'{"digest":"layer-a","total":100,"completed":99}\n')
        with self.assertRaises(ValueError):
            pull_model(ai, reports.append)
        stream.return_value = io.BytesIO(b'{"error":"x509: certificate signed by unknown authority"}\n')
        with self.assertRaisesRegex(RuntimeError, "registry TLS certificate verification failed"):
            pull_model(ai, reports.append)

    def test_winget_skips_broken_alias_and_finds_the_newest_app_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alias = root / "User" / "Microsoft" / "WindowsApps" / "winget.exe"
            alias.parent.mkdir(parents=True)
            alias.touch()
            packages = root / "Program Files" / "WindowsApps"
            executables = [
                packages / f"Microsoft.DesktopAppInstaller_{version}_x64__8wekyb3d8bbwe" / "winget.exe"
                for version in ("1.9.0.0", "1.29.380.0")
            ]
            for path in executables:
                path.parent.mkdir(parents=True)
                path.touch()

            def probe(command, **_kwargs):
                if command[0] == str(alias):
                    raise OSError("disabled alias")
                return MagicMock(stdout=b"v1.29.380\r\n")

            with (patch("mailsearch.ollama_setup.os") as environment,
                  patch("mailsearch.ollama_setup.shutil.which", return_value=str(alias)),
                  patch("mailsearch.ollama_setup.windows_tool", return_value=None),
                  patch("mailsearch.ollama_setup.subprocess.run", side_effect=probe) as run):
                environment.name = "nt"
                environment.environ = {"LOCALAPPDATA": str(root / "User"), "ProgramW6432": str(packages.parent),
                                       "PROCESSOR_ARCHITEW6432": "AMD64"}
                self.assertEqual(winget_binary(), str(executables[1]))
                self.assertEqual([call.args[0] for call in run.call_args_list],
                                 [[str(alias), "--version"], [str(executables[1]), "--version"]])

    def test_install_command_logs_winget_output_and_hresult(self):
        command = [sys.executable, "-c", "import sys; print('CRYPT_E_NO_REVOCATION_CHECK'); sys.exit(1)"]
        with self.assertLogs("mailsearch.gateway.ollama", level="ERROR") as logs:
            with self.assertRaises(InstallCommandError) as result:
                run_install_command(command, "fixture WinGet", lambda _value: None, 5)
        self.assertTrue(result.exception.contains(("crypt_e_no_revocation_check",)))
        self.assertIn("exit 1 (0x00000001)", logs.output[0])
        self.assertIn("CRYPT_E_NO_REVOCATION_CHECK", logs.output[0])

    def test_tls_fallback_runs_only_after_the_pinned_hash_matches(self):
        fixture = b"synthetic-installer"
        executed = []
        payload = fixture

        def command(args, label, _report, _timeout):
            executed.append((label, args))
            if args[0] == "fixture-winget":
                raise InstallCommandError(0x80092012, "CRYPT_E_NO_REVOCATION_CHECK")
            if args[0] == "fixture-curl":
                if "--insecure" not in args:
                    raise InstallCommandError(60, "SEC_E_UNTRUSTED_ROOT")
                Path(args[args.index("--output") + 1]).write_bytes(payload)

        with (patch("mailsearch.ollama_setup.ollama_binary", return_value=None),
              patch("mailsearch.ollama_setup.windows_tool", return_value="fixture-curl"),
              patch("mailsearch.ollama_setup.run_install_command", side_effect=command),
              patch("mailsearch.ollama_setup.OLLAMA_INSTALLER_SHA256", hashlib.sha256(fixture).hexdigest())):
            with self.assertLogs("mailsearch.gateway.ollama", level="WARNING"):
                install_ollama("fixture-winget", lambda _value: None)
            self.assertEqual([label for label, _ in executed], ["WinGet Ollama install", "Ollama installer download",
                                                               "Ollama installer download retry", "Verified Ollama installer"])
            self.assertNotIn("--insecure", executed[1][1])
            self.assertIn("--insecure", executed[2][1])

            executed.clear()
            payload = b"tampered"
            with self.assertLogs("mailsearch.gateway.ollama", level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                    install_ollama("fixture-winget", lambda _value: None)
            self.assertEqual(len(executed), 3)


class ApiSettingsPersistence(unittest.TestCase):
    def test_preset_supplies_only_defaults_for_its_gateway(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, GATEWAY_ENV, clear=True):
            preset = Path(directory) / "gateway-defaults.json"
            fields = {"gateway_url": "https://preset.example.invalid", "answer_models": "fixture-a, fixture-b",
                      "test_unverified": True}
            preset.write_text(json.dumps(fields), encoding="utf-8-sig")
            self.assertEqual(gateway_defaults(preset)["gateway_url"], GATEWAY_ORIGIN)
            self.assertFalse(gateway_defaults(preset)["test_unverified"])
            with patch.dict(os.environ, {"MAILSEARCH_GATEWAY_URL": "", "MAILSEARCH_GATEWAY_ANSWER_MODELS": ""}):
                self.assertEqual(gateway_defaults(preset), {**fields, "answer_models": "fixture-a,fixture-b", "ca_file": ""})
                preset.write_text(json.dumps({**fields, "key": "fixture-credential"}), encoding="utf-8")
                with self.assertLogs("mailsearch.gateway", level="WARNING"):
                    self.assertEqual(gateway_defaults(preset)["gateway_url"], "")

    def test_platform_without_dpapi_does_not_write_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ApiSettings, "secure", False):
            settings = ApiSettings(Path(directory))
            settings.save({"external": {"destination": "https://test.example.invalid", "key": "fixture-key"}})
            self.assertEqual(settings.load(), {"external": {"destination": "https://test.example.invalid"}})
            self.assertNotIn(b"fixture-key", settings.path.read_bytes())

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI is required")
    def test_windows_dpapi_protects_and_restores_current_user_data(self):
        secret = b"temporary-test-credential"
        encrypted = user_dpapi(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(user_dpapi(encrypted, decrypt=True), secret)


class GatewayTLS(unittest.TestCase):
    roles = {"answer": "answer-model-a", "embedding": "bge-m3", "rerank": "bge-reranker-v2-m3"}
    chat_response = json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode()

    def setUp(self):
        self.enterContext(patch.dict(os.environ, GATEWAY_ENV))
        tls_context.cache_clear()
        warn_unverified.cache_clear()
        self.addCleanup(tls_context.cache_clear)
        self.addCleanup(warn_unverified.cache_clear)
        self.client = IntranetAI("test-key", self.roles)

    def test_three_roles_share_verified_ca_context(self):
        trusted_context = ssl.create_default_context()
        with (patch.dict(os.environ, {"MAILSEARCH_GATEWAY_CA_FILE": "test-company-ca.pem"}),
              patch("mailsearch.embedding.ssl.create_default_context", return_value=trusted_context) as make,
              patch.object(trusted_context, "load_verify_locations") as load_ca,
              patch("mailsearch.embedding.http.client.HTTPSConnection") as https):
            response = https.return_value.getresponse.return_value
            response.status = 200
            response.read.side_effect = [
                self.chat_response,
                b'{"data":[{"index":0,"embedding":[1.0,0.0]}]}',
                b'{"results":[{"index":0,"relevance_score":1.0},{"index":1,"relevance_score":0.5}]}',
            ]
            self.assertEqual(self.client.test_models(), dict.fromkeys(self.roles, {"ok": True}))
            self.assertEqual([call.args[1] for call in https.return_value.request.call_args_list],
                             [CHAT_PATH, "/v1/embeddings", "/v1/rerank"])
            self.assertTrue(all(call.kwargs["context"] is trusted_context for call in https.call_args_list))
            self.assertEqual(trusted_context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(trusted_context.check_hostname)
            load_ca.assert_called_once_with(cafile="test-company-ca.pem")
            make.assert_called_once()

    def test_tls_and_http_errors_log_diagnostics_without_sensitive_data(self):
        with (tempfile.TemporaryDirectory() as directory, gateway_diagnostics(Path(directory)),
              patch("mailsearch.embedding.http.client.HTTPSConnection") as https):
            connection = https.return_value
            error = ssl.SSLCertVerificationError()
            error.verify_code, error.verify_message = 19, "self-signed certificate in certificate chain"
            connection.request.side_effect = error
            with self.assertRaises(ssl.SSLCertVerificationError):
                self.client.embed(["private-mail-text"], timeout=3)
            self.assertEqual(https.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
            connection.request.side_effect = None
            connection.getresponse.return_value.status = 403
            connection.getresponse.return_value.reason = "test-key private-mail-text"
            with self.assertRaises(GatewayRequestError):
                self.client.rerank("private-mail-text", ["doc"])
            log = (Path(directory) / "api-connection.log").read_text(encoding="utf-8")
            self.assertIn("SSLCertVerificationError (code=19) self-signed certificate in certificate chain", log)
            self.assertIn("/v1/rerank: HTTP 403 Forbidden", log)
            self.assertNotIn("private-mail-text", log)
            self.assertNotIn("test-key", log)

    def test_unverified_mode_requires_opt_in_and_is_gateway_only(self):
        with (patch.dict(os.environ, {"MAILSEARCH_GATEWAY_TLS_VERIFY": "0"}),
              patch("mailsearch.embedding.http.client.HTTPSConnection") as https,
              self.assertLogs("mailsearch.gateway", level="WARNING") as log):
            response = https.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = self.chat_response
            self.client.answer_client.test()
            context = https.call_args.kwargs["context"]
            self.assertEqual(context.verify_mode, ssl.CERT_NONE)
            self.assertFalse(context.check_hostname)
            self.assertIn("test only", log.output[0])
            ExternalAI("https://other.example.invalid/v1/chat/completions", "answer-model-a", "test-key").test()
            https.assert_called_with("other.example.invalid", 443, timeout=15)
            with patch.dict(os.environ, {"MAILSEARCH_GATEWAY_CA_FILE": "company-ca.pem"}):
                https.reset_mock()
                with self.assertRaises(ValueError):
                    self.client.answer_client.test()
                https.assert_not_called()


class ExternalTransport(unittest.TestCase):
    def test_https_endpoint_only_and_no_redirect(self):
        for url in ("http://api.example.invalid/v1/chat/completions",
                    "https://user@api.example.invalid/v1/chat/completions",
                    "https://api.example.invalid/v1/chat/completions?key=sample",
                    "https://api.example.invalid/v1/chat/completions#other",
                    "https://api.example.invalid/v1/embeddings"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ExternalAI(url, "test", "placeholder-token")
        ai = ExternalAI("https://gateway.example.invalid/v1/chat/completions", "test", "placeholder-token")
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            con = https.return_value
            con.getresponse.return_value.status = 307
            with self.assertRaises(RuntimeError):
                ai.answer("테스트", [Excerpt(1, 91, "테스트 근거")])
            https.assert_called_once_with("gateway.example.invalid", 443, timeout=35)
            con.request.assert_called_once()
            con.close.assert_called_once()

    def test_anthropic_messages_endpoint(self):
        ai = ExternalAI("https://api.z.ai/api/anthropic/v1/messages", "glm-5.3", "placeholder-token")
        with patch("mailsearch.external.http.client.HTTPSConnection") as https:
            con = https.return_value
            reply = con.getresponse.return_value
            reply.status = 200
            result = {"status": "insufficient", "claims": []}
            reply.read.return_value = json.dumps(
                {"content": [{"type": "text", "text": json.dumps(result)}]}
            ).encode()
            self.assertEqual(ai.answer("테스트", [Excerpt(1, 91, "테스트 근거")])["status"], "insufficient")
            method, path, body, headers = con.request.call_args.args
            self.assertEqual((method, path), ("POST", "/api/anthropic/v1/messages"))
            self.assertEqual(headers["x-api-key"], "placeholder-token")
            self.assertEqual(headers["anthropic-version"], "2023-06-01")
            sent = json.loads(body)
            self.assertEqual(sent["model"], "glm-5.3")
            self.assertEqual(sent["max_tokens"], 1500)
            self.assertIn("system", sent)
            self.assertEqual(sent["messages"][0]["role"], "user")
            self.assertNotIn("response_format", sent)

    def test_review_expiry_and_size_limits(self):
        reviews = Reviews()
        rows = [{"passage": n, "text": "A" * 800 + "불필요한 뒷부분", "location": f"{n}페이지"}
                for n in range(1, 9)]
        for row in rows:
            row["text"] = str(row["passage"]) + row["text"]
        rows.insert(0, {"passage": 99, "text": "계약서_기밀.pdf", "location": "파일명"})
        with patch("mailsearch.external.time.monotonic", return_value=100):
            token, review = reviews.create("질문", rows)
        self.assertEqual(len(review.excerpts), 5)
        self.assertEqual(review.excerpts[0].passage, 1)
        self.assertTrue(all(len(item.text) == 800 for item in review.excerpts))
        with patch("mailsearch.external.time.monotonic", return_value=401):
            self.assertIsNone(reviews.take(token))


class AttachmentsAndCitations(unittest.TestCase):
    def office_zip(self, documents):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            for path, data in documents.items():
                zipped.writestr(path, data)
        message = EmailMessage()
        message.set_content(buffer.getvalue(), maintype="application", subtype="octet-stream")
        return message

    def test_docx_and_xlsx_location(self):
        word = self.office_zip({"word/document.xml": "<document><p><r><t>합성 견적 승인</t></r></p></document>"})
        book = self.office_zip({
            "xl/workbook.xml": '<workbook xmlns:r="relationship"><sheets><sheet name="견적" r:id="sheet1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="sheet1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '<worksheet><row><c r="B4" t="inlineStr"><is><t>B-19</t></is></c><c r="C4"><v>320000</v></c></row></worksheet>',
        })
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(extract_attachment("synthetic.docx", word, temp, "pdftotext"), ("완료", [("본문", "합성 견적 승인")]))
            self.assertEqual(extract_attachment("synthetic.xlsx", book, temp, "pdftotext"), ("완료", [("견적!B4~C4", "B4 B-19 | C4 320000")]))

    def test_exact_delivery_rule_ignores_previous_quoted_mail_and_cancellations(self):
        base = {
            "id": 1, "source": 1, "path": "test.pst", "folder": "Inbox", "updated": None,
            "passage": 1, "sent": "", "sender": "테스트", "recipient": "", "subject": "한빛 A-17 일정",
            "attachment": None, "location": "본문", "start": 0,
        }
        question = "한빛 A-17 결국 납품 언제야?"
        approval = {**base, "text": "4월 25일 납품으로 최종 승인합니다."}
        self.assertIn("25일", delivery_date_answer(question, [approval])["claims"][0]["text"])
        quoted = {**base, "text": "확인했습니다.\n> 4월 25일 납품으로 최종 승인합니다."}
        self.assertIsNone(delivery_date_answer(question, [quoted]))
        cancelled = {**base, "text": "앞에서 승인한 일정을 취소합니다."}
        self.assertIsNone(delivery_date_answer(question, [approval, cancelled]))
        conflicting = {**base, "text": "4월 29일 납품으로 최종 승인합니다."}
        self.assertIsNone(delivery_date_answer(question, [approval, conflicting]))

    def test_approval_quote_must_support_scope_and_closure(self):
        row = {
            "id": 1, "source": 1, "path": "PCF.txt", "folder": "/", "updated": None,
            "passage": 1, "sent": "", "sender": "", "recipient": "", "subject": "PCF",
            "text": "시제품만 4T 적용 승인. 양산은 보류.", "attachment": None, "location": "본문",
        }
        claim = {"text": "양산 4T 적용 승인", "citations": [{"source": 1, "quote": row["text"]}]}
        result = {"status": "answered", "claims": [claim]}
        self.assertEqual(validate(result, [row], pcf=True)["status"], "insufficient")
        claim["text"] = "시제품 4T 적용 승인"
        self.assertEqual(validate(result, [row], pcf=True)["status"], "answered")
        claim["text"] = "이 건은 종결됐습니다."
        self.assertEqual(validate(result, [row], pcf=True)["status"], "insufficient")

    def test_general_claim_requires_same_actor_status_unit_and_condition(self):
        row = {"id": 1, "source": 1, "path": "fixture.pst", "folder": "Inbox", "updated": None,
               "passage": 1, "sent": "", "sender": "", "recipient": "", "subject": "부품 구매",
               "attachment": None, "location": "본문"}
        def check(original, claim, quote=None):
            value = {"text": claim, "citations": [{"source": 1, "quote": quote or original}]}
            return validate({"status": "answered", "claims": [value]}, [{**row, "text": original}])["status"]
        for original, claim, quote in [
            ("구매팀은 금요일 납품을 요청했습니다.", "구매팀은 금요일 납품을 확정했습니다.", None),
            ("구매팀이 금요일 납품 승인을 요청합니다.", "구매팀이 금요일 납품 승인", "구매팀이 금요일 납품 승인"),
            ("구매팀이 영업팀에 금요일 납품을 확정했습니다.", "영업팀이 금요일 납품을 확정했습니다.", None),
            ("구매팀이 시범점에만 20만원을 승인했습니다.", "구매팀이 20만원을 승인했습니다.", None),
            ("구매팀이 20만원을 승인했습니다.", "구매팀이 20원을 승인했습니다.", None),
            ("20만원을 요청했습니다. 30만원을 승인했습니다.", "20만원을 승인했습니다.", None),
            ("수요일 납품을 요청했습니다.", "납품일은 수요일입니다.", None),
            ("B-17 가격은 5만원입니다.", "A-17 가격은 5만원입니다.", None),
            ("기준은 최대 5개입니다.", "기준은 최소 5개입니다.", None),
            ("예산을 승인했습니다.", "예산은 미승인입니다.", None),
            ("예산은 확정됐습니다.", "예산은 미확정입니다.", None),
        ]:
            with self.subTest(claim=claim):
                self.assertEqual(check(original, claim, quote), "insufficient")
        text = "구매팀이 시범점에만 20만원을 승인했습니다."
        self.assertEqual(check(text, text), "answered")

    def test_inconsistent_decision_returns_both_verbatim_citations(self):
        base = {"id": 1, "source": 1, "path": "fixture.pst", "folder": "Inbox", "updated": None,
                "sent": "", "sender": "", "recipient": "", "subject": "AX-12 납품", "attachment": None,
                "location": "본문", "start": 0}
        rows = [{**base, "passage": i, "text": f"4월 {day}일 납품으로 확정합니다."}
                for i, day in enumerate([20, 21], 1)]
        proposed = {"status": "answered", "claims": [{"text": rows[0]["text"],
                    "citations": [{"source": 1, "quote": rows[0]["text"]}]}]}
        answer = validate(proposed, rows, "AX-12 납품 언제로 확정됐어?")
        self.assertEqual(answer["status"], "ambiguous")
        self.assertEqual([c["citations"][0]["quote"] for c in answer["claims"]], [r["text"] for r in rows])

    def test_conflicting_budget_replies_keep_hold_and_approval(self):
        base = {"id": 1, "source": 1, "path": "fixture.pst", "folder": "Inbox", "updated": None,
                "sent": "", "sender": "", "recipient": "", "subject": "AX-12 예산", "attachment": None,
                "location": "본문", "start": 0}
        rows = [{**base, "passage": i, "text": text} for i, text in enumerate(
            ["AX-12 예산을 승인합니다.", "AX-12 예산 집행을 보류합니다."], 1)]
        proposed = {"status": "answered", "claims": [{"text": rows[0]["text"],
                    "citations": [{"source": 1, "quote": rows[0]["text"]}]}]}
        question = "AX-12 예산이 승인됐어?"
        self.assertEqual(validate(proposed, rows, question)["status"], "ambiguous")
        rows[1]["subject"] = "BX-12 예산"
        self.assertEqual(validate(proposed, rows, question)["status"], "answered")

    def test_invalid_quote_unproven_amount_and_approval(self):
        row = {
            "id": 1, "source": 1, "path": "fixture.pst", "folder": "Inbox", "updated": "2026-04-09",
            "passage": 1, "sent": "2026-04-09", "sender": "테스트", "recipient": "구매팀", "subject": "일정",
            "text": "4월 25일로 일정 변경 승인 요청드립니다.", "attachment": None, "location": "본문",
        }
        result = {"status": "answered", "claims": [{"text": "4월 25일 최종 승인됐습니다.", "citations": [{"source": 1, "quote": row["text"]}]}]}
        self.assertEqual(validate(result, [row])["status"], "insufficient")
        result["claims"][0]["text"] = "4월 28일로 변경을 요청했습니다."
        self.assertEqual(validate(result, [row])["status"], "insufficient")
        result["claims"][0]["text"] = "납품일은 2026-04-09입니다."
        result["claims"][0]["citations"][0]["quote"] = "Sent: 2026-04-09"
        self.assertEqual(validate(result, [row], "납품일은 언제인가?")["status"], "insufficient")
        result["claims"][0]["citations"][0]["quote"] = "메일에 없는 문구"
        self.assertEqual(validate(result, [row])["status"], "insufficient")


class WordPCFProcessing(unittest.TestCase):
    def test_legacy_uses_extension_without_leading_dot(self):
        for old, new in ((".doc", "docx"), (".xls", "xlsx")):
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / ("source" + old)
                source.write_bytes(b"legacy bytes")

                def convert(args, **_):
                    self.assertEqual(args[args.index("--convert-to") + 1], new)
                    self.assertEqual(Path(args[-1]).read_bytes(), b"legacy bytes")
                    Path(args[-1]).with_suffix("." + new).write_bytes(b"converted bytes")

                with patch("mailsearch.extract.office_binary", return_value="soffice"), patch(
                        "mailsearch.extract.subprocess.run", side_effect=convert):
                    self.assertEqual(convert_legacy(source, old, tmp), (b"converted bytes", None))
                self.assertEqual(source.read_bytes(), b"legacy bytes")

    def test_word_pcf_split_preserves_unicode_offsets_and_empty_cell(self):
        text = "한글 \"※ " * 1400 + "마지막 회신"
        excerpts = [Excerpt(1, 10, text), Excerpt(2, 20, "")]
        case = {"expected": 2, "warnings": [], "cells": {
            10: {"row": 2, "side": "OWNER", "label": "a", "no": "1"},
            20: {"row": 2, "side": "YARD", "label": "a", "no": "1"},
        }}
        batches = pcf_batches("1번 경과", excerpts, case, 5500)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(not batch["coverage"]["complete"] for batch in batches))
        cells = [item for batch in batches for item in batch["evidence"]]
        self.assertEqual("".join(cell["text"] for cell in cells if cell["source"] == 1), text)
        self.assertEqual(cells[-1]["text"], "")
        for cell in cells[:-1]:
            self.assertEqual(text[cell["offset"]:cell["offset"] + len(cell["text"])], cell["text"])


if __name__ == "__main__":
    unittest.main()
