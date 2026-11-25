import errno
import hashlib
import http.client
import io
import json
import logging
import os
import shutil
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date
from unittest.mock import MagicMock, patch
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

from mailsearch.api_settings import ApiSettings, user_dpapi
from mailsearch.answer import (QUERY_EMBEDDING_TIMEOUT, QUERY_REWRITE_TIMEOUT, AnswerSessions, LocalAI,
                               answer_groups, answer_plan, combine_answer_evidence, dedupe_by_content, delivery_date_answer,
                               evidence_for, latest_thread_decisions, needs_recency_boost, parse_evidence_gap, parse_rewrite,
                               reciprocal_rank, report_unconfirmed_aspects, rewritten_evidence,
                               supplemental_evidence, validate)
from mailsearch.embedding import EmbeddingAPI, GatewayRequestError, tls_context, warn_unverified
from mailsearch.external import ExternalAI, Excerpt, Reviews, pack_excerpts, pcf_batches
from mailsearch.extract import convert_legacy, extract_attachment, extract_bytes
from mailsearch.filesystem import DRIVE_REMOTE, is_unc_share, local_path, source_path
from mailsearch.intranet import CHAT_PATH, RERANK_PATH, IntranetAI, LOCAL_EMBEDDING
from mailsearch.indexer import ImportIssue, Indexer
from mailsearch.ollama_setup import (InstallCommandError, install_ollama, package_progress,
                                    pull_model, run_install_command, winget_binary)
from mailsearch.outlook import OutlookError, OutlookStore
from mailsearch.search_profile import SearchProfile
from mailsearch.server import (App, INSTANCE_FILE, SessionLock, existing_origin, gateway_defaults,
                               gateway_diagnostics, main, publish_instance)
from mailsearch.store import VECTOR_LOOKUP_SQL, Store, contextual_model_key, fingerprint, local_midnight_utc, mail_sections, merge_contiguous_passages, pack_vector, question_scope, read_signature, sections
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
                    self.assertEqual(path, CHAT_PATH)
                    if data["max_tokens"] == 384:
                        self.assertIn(model, ("answer-model-a", "answer-model-b"))
                        question = json.loads(data["messages"][1]["content"])
                        self.assertEqual(set(question), {"question", "today"})
                        content = '{"sub_queries":["A-17 납기"],"keywords":["A-17"],"filters":{}}'
                    else:
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
            rewrite, = [json.loads(call.args[2]) for call in request.call_args_list if call.args[1] == CHAT_PATH]
            self.assertEqual((rewrite["model"], rewrite["max_tokens"]), ("answer-model-a", 384))
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
            rewrite, = [json.loads(call.args[2]) for call in request.call_args_list if call.args[1] == CHAT_PATH]
            self.assertEqual((rewrite["model"], rewrite["max_tokens"]), ("answer-model-b", 384))
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

    def test_gateway_runtime_tls_opt_in_applies_only_to_answer_and_embeddings(self):
        route = "/api/intranet/settings"
        self.app.api_settings = ApiSettings(self.store.root)
        roles = {"answer": "answer-model-a", "embedding": "bge-m3", "rerank": "bge-reranker-v2-m3"}
        payload = {"action": "apply", "key": "tls-fixture-key", "selections": roles}
        with (patch.object(ApiSettings, "secure", True),
              patch("mailsearch.api_settings.user_dpapi",
                    side_effect=lambda data, decrypt=False: zlib.decompress(data) if decrypt else zlib.compress(data)),
              patch("mailsearch.embedding.http.client.HTTPSConnection") as https):
            self.assertFalse(self.call(route)[1]["runtime_unverified"])
            result = self.call(route, {**payload, "runtime_unverified": "true"})
            self.assertEqual(result, (400, {"error": "gateway_settings"}))
            status, settings = self.call(route, {**payload, "runtime_unverified": True})
            self.assertEqual(status, 200)
            self.assertTrue(settings["runtime_unverified"])
            self.assertEqual(self.app.api_settings.load()["gateway"]["unverified_origin"], GATEWAY_ORIGIN)
            response = https.return_value.getresponse.return_value
            response.status = 200

            def gateway_reply(method, path, body, headers):
                verify = https.call_args.kwargs["context"].verify_mode
                hostname_checked = https.call_args.kwargs["context"].check_hostname
                self.assertEqual(method, "POST")
                self.assertEqual(headers["Authorization"], "Bearer tls-fixture-key")
                request = json.loads(body)
                if path == "/v1/rerank":
                    self.assertEqual((verify, hostname_checked), (ssl.CERT_REQUIRED, True))
                    value = {"results": [{"index": i, "relevance_score": 1} for i in range(len(request["documents"]))]}
                else:
                    self.assertEqual((verify, hostname_checked), (ssl.CERT_NONE, False))
                    if path == "/v1/embeddings":
                        texts = request["input"]
                        count = len(texts) if isinstance(texts, list) else 1
                        value = {"data": [{"index": i, "embedding": [1., .05, .12]} for i in range(count)]}
                    else:
                        self.assertEqual(path, "/v1/chat/completions")
                        value = {"choices": [{"message": {"content": '{"status":"insufficient","claims":[]}'}}]}
                response.read.return_value = json.dumps(value).encode()

            request = https.return_value.request
            request.side_effect = gateway_reply
            source = self.import_mail()
            self.assertIn("/v1/embeddings", [call.args[1] for call in request.call_args_list])
            query = {"query": "A-17 납기", "filters": {"source": source}}
            status, preview = self.call("/api/external/preview", query)
            self.assertEqual(status, 200)
            status, answer = self.call("/api/external/send", {"review": preview["review"],
                                                            "excerpts": [preview["excerpts"][0]["id"]]})
            self.assertEqual((status, answer["status"]), (200, "insufficient"))
            self.assertEqual(request.call_args.args[1], "/v1/chat/completions")
            self.assertIn("/v1/rerank", [call.args[1] for call in request.call_args_list])
            with App(0, self.store, self.indexer, LocalAI("test"), api_settings=self.app.api_settings) as restored:
                self.assertEqual(restored.gateway_unverified_origin, GATEWAY_ORIGIN)
                self.assertEqual(restored.external.tls, self.app.external.tls)
            status, settings = self.call(route, {**payload, "gateway_url": "https://other.example.invalid"})
            self.assertEqual(status, 200)
            self.assertFalse(settings["runtime_unverified"])
            self.assertEqual(self.app.external.tls.unverified_paths, ())

    def test_reranker_certificate_opt_in_is_explicit_and_address_bound(self):
        route = "/api/intranet/settings"
        self.app.api_settings = ApiSettings(self.store.root)
        roles = {"answer": "", "embedding": "", "rerank": "bge-reranker-v2-m3"}
        payload = {"action": "apply", "key": "rerank-fixture-key", "selections": roles}
        with (patch.object(ApiSettings, "secure", True),
              patch("mailsearch.api_settings.user_dpapi",
                    side_effect=lambda data, decrypt=False: zlib.decompress(data) if decrypt else zlib.compress(data)),
              patch("mailsearch.embedding.http.client.HTTPSConnection") as https):
            response = https.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"results":[{"index":0,"relevance_score":1}]}'
            self.assertEqual(self.call(route, payload)[0], 200)
            self.assertFalse(self.call(route)[1]["rerank_unverified"])
            self.ai.embedding_api.rerank("test", ["one"])
            self.assertEqual(https.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
            status, settings = self.call(route, {**payload, "rerank_unverified": True})
            self.assertEqual(status, 200)
            self.assertTrue(settings["rerank_unverified"])
            self.assertFalse(settings["runtime_unverified"])
            self.ai.embedding_api.rerank("test", ["one"])
            self.assertEqual(https.call_args.kwargs["context"].verify_mode, ssl.CERT_NONE)
            with App(0, self.store, self.indexer, LocalAI("test"), api_settings=self.app.api_settings) as restored:
                self.assertEqual(restored.rerank_unverified_origin, GATEWAY_ORIGIN)
                self.assertEqual(restored.ai.embedding_api.tls.unverified_paths, ("/v1/rerank",))
            status, settings = self.call(route, {**payload, "gateway_url": "https://different.example.invalid"})
            self.assertEqual(status, 200)
            self.assertFalse(settings["rerank_unverified"])
            self.assertFalse(self.ai.embedding_api.tls.unverified_paths)

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

        def fake_export(pst, stage, event, source):
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

    def test_folder_refresh_defers_locked_pst_and_updates_the_rest(self):
        folder = Path(self.workspace.name) / "shared-archives"
        folder.mkdir()
        locked = folder / "a-locked.pst"
        other = folder / "b-readable.pst"
        header = b"!BDN" + bytes(4) + b"SM" + bytes(2)
        for pst in (locked, other):
            pst.write_bytes(header)
        blocked = set()
        exported = []

        def lock_check(pst):
            if pst in blocked:
                error = OSError("Windows sharing violation")
                error.winerror = 32
                raise error
            return nullcontext()

        def export(pst, stage, _event, _source):
            exported.append(pst.name)
            message = EmailMessage()
            message["Subject"] = pst.stem
            message["Message-ID"] = f"<{pst.stem}@example.invalid>"
            suffix = pst.read_bytes()[len(header):].decode("ascii")
            message.set_content(f"{pst.stem} {'updated' if suffix else 'original'} archive text.")
            output = stage / "eml"
            output.mkdir(parents=True)
            (output / "1").write_bytes(message.as_bytes())
            return output

        def wait_for_result(source):
            row = await_source(self.store, source, {"ready", "error", "empty"})
            deadline = time.monotonic() + 3
            while source in self.indexer.events and time.monotonic() < deadline:
                time.sleep(.01)
            return row

        def listing(source):
            return next(item for item in self.call("/api/sources")[1]["sources"] if item["id"] == source)

        with (patch.object(Indexer, "export", side_effect=export),
              patch("mailsearch.indexer.stable_input", side_effect=lock_check)):
            blocked.update((locked, other))
            status, payload = self.call("/api/sources", {"path": str(folder)})
            self.assertEqual(status, 202)
            source = payload["source"]
            row = wait_for_result(source)
            self.assertEqual((row["state"], row["issue"], row["omitted"]), ("error", "pst_in_use", 2))
            self.assertEqual(listing(source)["deferred_psts"], 2)
            blocked.clear()
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "resume"})[0], 202)
            row = wait_for_result(source)
            self.assertEqual((row["state"], row["done"], row["omitted"]), ("ready", 2, 0))
            known = self.store.pst_files(source)

            for pst in (locked, other):
                pst.write_bytes(header + b"new-data")
            blocked.add(locked)
            exported.clear()
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            row = wait_for_result(source)
            self.assertEqual((row["state"], row["done"], row["omitted"]), ("ready", 2, 1))
            current = listing(source)
            self.assertEqual(current["deferred_psts"], 1)
            self.assertEqual(current["problems"], [{"item": locked.name, "code": "pst_in_use"}])
            self.assertEqual(current["updates"], [locked.name])
            self.assertEqual(exported, [other.name])
            self.assertEqual(self.store.pst_files(source)[locked.name + "/"], known[locked.name + "/"])
            for query in ('"a-locked original"', '"b-readable updated"'):
                self.assertTrue(self.call("/api/search", {"query": query, "filters": {}})[1]["results"])

            blocked.clear()
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            row = wait_for_result(source)
            self.assertEqual((row["state"], row["done"], row["omitted"]), ("ready", 2, 0))
            self.assertEqual(exported, [other.name, locked.name])
            self.assertEqual(listing(source)["updates"], [])
            self.assertTrue(self.call("/api/search", {"query": '"a-locked updated"', "filters": {}})[1]["results"])

    def test_refresh_reuses_v3_passages_and_embeds_only_new_mail(self):
        # Seed an archive that still uses the older embedding namespace.
        with patch("mailsearch.indexer.contextual_model_keys",
                   side_effect=lambda model: ("metadata-v1:" + model, contextual_model_key(model))):
            source = self.import_mail()
        with self.store.connection() as conn:
            conn.execute("UPDATE mails SET fingerprint=replace(fingerprint,'sections-v4:','sections-v3:') WHERE source=?",
                         (source,))
            vectors_before = conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        extra = EmailMessage()
        extra["From"] = "new@example.invalid"
        extra["To"] = "buy@example.invalid"
        extra["Subject"] = "증분 색인 시험"
        extra["Message-ID"] = "<incremental@example.invalid>"
        extra.set_content("새로 도착한 증분 색인 대상 메일입니다.")
        (self.mail / "incremental.eml").write_bytes(extra.as_bytes())
        with patch.object(self.ai, "embed", wraps=self.ai.embed) as embed:
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            updated = await_source(self.store, source, {"ready", "error"})
            embed.assert_called_once()
            self.assertEqual(len(embed.call_args.args[0]), 1)
            self.assertIn("새로 도착한", embed.call_args.args[0][0])
        self.assertEqual((updated["state"], updated["done"]), ("ready", 5))
        self.assertEqual(updated["embedding_model_key"], "metadata-v1:embed")
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

        def fake_export(pst, stage, event, source):
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

    def test_folder_refresh_rebuilds_legacy_pst_from_cached_messages(self):
        folder = Path(self.workspace.name) / "legacy-archive"
        folder.mkdir()
        (folder / "old.pst").write_bytes(b"!BDN" + bytes(4) + b"SM" + bytes(2))
        (folder / "standalone.txt").write_text("Document content should be reused.", encoding="utf-8")
        document = "공통 첨부 본문입니다."

        def export(pst, stage, event, source):
            output = stage / "eml"
            output.mkdir()
            for i in (1, 2):
                mail = EmailMessage()
                mail["Subject"] = f"Legacy agenda {i}"
                mail.set_content("현재 답변입니다.\n\n> ArchivedReplyOnlyMarker")
                mail.add_attachment(document, filename="agenda.txt")
                (output / str(i)).write_bytes(mail.as_bytes())
            (stage / "export-complete").touch()
            return output

        def no_cache(name, part, temp, pdf, *, check, cache):
            return extract_attachment(name, part, temp, pdf, check=check)

        def finished(source):
            row = await_source(self.store, source, {"ready", "error"})
            for _ in range(300):
                if source not in self.indexer.events:
                    break
                time.sleep(.01)
            self.assertEqual(row["state"], "ready")
            return row

        with (patch.object(self.indexer, "export", side_effect=export) as exported,
              patch("mailsearch.store.fingerprint",
                    side_effect=lambda mail, item, **_: fingerprint(mail, item, index_format=None)),
              patch("mailsearch.store.mail_sections", side_effect=sections),
              patch("mailsearch.indexer.extract_attachment", side_effect=no_cache)):
            source = self.indexer.register(folder)
            first = finished(source)
            exported.assert_called_once()
        filters = {"source": source}
        self.assertTrue(self.store.lexical("ArchivedReplyOnlyMarker", filters))
        self.assertEqual(self.indexer.pending_updates(first), ["old.pst"])
        with self.store.connection() as conn:
            attachment = conn.execute("""SELECT a.id FROM attachments a JOIN mails m ON a.mail=m.id
                                         WHERE m.source=? AND m.kind='mail' ORDER BY a.id LIMIT 1""", (source,)).fetchone()[0]
        self.assertEqual(self.indexer.attachment_bytes(self.store.original_attachment(attachment)).decode().strip(), document)
        with patch("mailsearch.indexer.stable_input", side_effect=PermissionError("PST in use")):
            self.indexer.command(source, "refresh")
            locked = finished(source)
        self.assertTrue(self.store.lexical("ArchivedReplyOnlyMarker", filters))
        self.assertEqual(self.indexer.pending_updates(locked), ["old.pst"])

        with (patch.object(self.indexer, "export", side_effect=AssertionError("reuse cached messages")),
              patch.object(self.store, "adopt_mail", wraps=self.store.adopt_mail) as adopted,
              patch("mailsearch.extract.extract_bytes", wraps=extract_bytes) as extracts):
            self.indexer.command(source, "refresh")
            ready = finished(source)
            self.assertEqual((ready["state"], ready["done"]), ("ready", 3))
            adopted.assert_called_once()  # The standalone document still uses its old fingerprint.
            attached = [call for call in extracts.call_args_list if call.args[0].decode().strip() == document]
            self.assertEqual(len(attached), 1)
        self.assertEqual(self.indexer.pending_updates(ready), [])
        self.assertFalse(self.store.lexical("ArchivedReplyOnlyMarker", filters))
        self.assertTrue(self.store.lexical("현재 답변", filters))
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embedding_refs").fetchone()[0], 2)
            original = conn.execute("SELECT body FROM mails WHERE source=? AND kind='mail'", (source,)).fetchone()[0]
        self.assertIn("ArchivedReplyOnlyMarker", original)

    def test_gateway_indexes_existing_archive_and_searches_with_the_same_backend(self):
        source = self.import_mail()
        with self.store.connection() as conn:
            conn.execute("UPDATE vectors SET model='bge-m3' WHERE model=?", (contextual_model_key("embed"),))
            conn.execute("UPDATE vector_buckets SET model='bge-m3' WHERE model=?", (contextual_model_key("embed"),))
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
                self.assertEqual(models, {"bge-m3", contextual_model_key(self.ai.embedding_index)})
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
                "model": "bge-m3:latest", "input": [query["query"]], "truncate": True, "keep_alive": -1,
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
            self.assertTrue({contextual_model_key("bge-m3"), contextual_model_key(EMBEDDING_ENDPOINT + "#bge-m3")}.issubset(indexes))

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

    def test_missing_heartbeat_waits_for_indexing_and_the_idle_period(self):
        self.app.last_seen = 1.0
        stopped = MagicMock()
        stopped.wait.side_effect = [False, False, False, True]
        with (patch("mailsearch.server.time.monotonic", side_effect=[20.0, 23.0, 34.0]),
              patch.object(self.indexer, "has_work", side_effect=[True, False, False]),
              patch.object(self.app, "shutdown") as shutdown):
            self.app.watch_page(stopped)
        self.assertEqual(stopped.wait.call_count, 3)
        shutdown.assert_called_once_with()

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

    def test_shared_folder_can_be_registered_and_its_document_opened(self):
        shared = Path(self.workspace.name) / "shared team"
        shared.mkdir()
        original = shared / "회의록.txt"
        text = "공유 폴더의 회의에서 일정 확정."
        original.write_text(text, encoding="utf-8")
        with patch("mailsearch.filesystem.windows_drive_type", return_value=DRIVE_REMOTE):
            status, payload = self.call("/api/sources", {"path": f' "{shared}" '})
            self.assertEqual(status, 202)
            source = payload["source"]
            row = await_source(self.store, source, {"ready", "error", "empty"})
            self.assertEqual((row["state"], row["done"]), ("ready", 1))
            results = self.call("/api/search", {"query": '"일정 확정"', "filters": {"source": source}})[1]["results"]
            self.assertEqual(len(results), 1)
            status, evidence = self.call(f"/api/evidence/{results[0]['passage']}")
            self.assertEqual(status, 200)
            self.assertIn(text, evidence["text"])
            self.assertEqual(original.read_text(encoding="utf-8"), text)
            with self.assertRaises(OSError):
                local_path(shared)

    def test_registration_explains_missing_path_and_read_permission(self):
        route = "/api/sources"
        path = str(Path(self.workspace.name) / "missing")
        self.assertEqual(self.call(route, {"path": path}), (409, {"error": "source_path"}))
        with patch("mailsearch.indexer.source_path", side_effect=PermissionError):
            self.assertEqual(self.call(route, {"path": path}), (409, {"error": "source_access"}))

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
            with (patch.object(self.app.external, "complete") as complete,
                  patch("mailsearch.filesystem.windows_drive_type", return_value=DRIVE_REMOTE)):
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
        self.assertLessEqual(len(legacy["excerpts"]), 5)
        self.assertTrue(all(len(item["text"]) <= 800 and item["context"] is None for item in legacy["excerpts"]))
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

    def test_already_running_reopens_only_the_matching_app(self):
        path = self.store.root / INSTANCE_FILE
        self.assertIsNone(existing_origin(self.store.root))
        with SessionLock(self.store.root), publish_instance(self.store.root, self.app):
            with (patch("mailsearch.server.webbrowser.open") as browser,
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(main(["--data", str(self.store.root), "--port", "0"]), 0)
                browser.assert_called_once_with(self.app.origin)
                self.assertEqual(main(["--data", str(self.store.root), "--port", "0", "--no-browser"]), 1)
                browser.assert_called_once()
                path.write_text(json.dumps({"port": self.app.server_port, "id": "0" * 32}), encoding="ascii")
                self.assertEqual(main(["--data", str(self.store.root), "--port", "0"]), 1)
                browser.assert_called_once()
        self.assertFalse(path.exists())

    def test_startup_reports_the_stage_for_actual_permission_errors(self):
        failure = PermissionError(errno.EACCES, "fixture permission error")
        with (patch("mailsearch.server.SessionLock.__enter__", side_effect=failure),
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(main(["--data", str(self.store.root), "--port", "0", "--no-browser"]), 1)
        message = stderr.getvalue()
        self.assertIn("세션 잠금 파일 열기", message)
        self.assertIn("errno=13", message)
        self.assertNotIn("다른 실행이", message)

    def test_startup_logs_active_vector_backend_after_optional_backend_fails(self):
        data = Path(self.workspace.name) / "startup"
        for numpy, expected in ((None, "sqlite(pure-python) numpy=disabled"),
                                (object(), "sqlite+numpy numpy=enabled")):
            with (patch("mailsearch.vector_index.USearchIndex", side_effect=ImportError),
                  patch("mailsearch.store.optional_numpy", return_value=numpy),
                  patch.object(App, "serve_forever"), redirect_stdout(io.StringIO())):
                self.assertEqual(main(["--data", str(data), "--port", "0", "--no-browser",
                                       "--vector-index", "usearch"]), 0)
            log = (data / "app-runtime.log").read_text(encoding="utf-8")
            self.assertIn(f"Vector search backend={expected}", log)
        with patch.object(self.store, "vector_index", object()):
            self.assertEqual(self.store.vector_backend, "usearch(int8)")

    def test_external_gap_labels_are_applied_after_reviewed_generation(self):
        source = self.import_mail()
        original = self.store.lexical("A-17", {"source": source})[:1]
        question = "A-17 시제품 승인과 양산 BOM 비교"
        plan = parse_evidence_gap({"sufficient": False, "missing_aspects": ["양산 BOM 반영 여부"],
                                   "follow_up_queries": ["양산 BOM"]}, question)
        self.ai.rewrite_model = "fixture"
        self.app.external = ExternalAI("https://example.invalid/v1/chat/completions", "fixture", "key")
        with (patch("mailsearch.server.rewritten_evidence", return_value=(original, "keyword", {"source": source}, 2)),
              patch.object(self.ai, "diagnose_evidence_gap", return_value=plan) as diagnose,
              patch.object(self.app.external, "complete", return_value={"status": "insufficient", "claims": []}) as complete):
            status, preview = self.call("/api/external/preview", {"query": question, "filters": {"source": source}})
            self.assertEqual(status, 200)
            self.assertTrue(preview["excerpts"])
            diagnose.assert_called_once()
            complete.assert_not_called()
            status, response = self.call("/api/external/send", {
                "review": preview["review"], "excerpts": [preview["excerpts"][0]["id"]],
            })
            self.assertEqual(status, 200)
            self.assertEqual(response["unknowns"], ["양산 BOM 반영 여부"])
            self.assertEqual(set(json.loads(complete.call_args.args[1])), {"question", "evidence"})

    def test_answer_stream_reports_the_completed_supplemental_pass(self):
        source = self.import_mail()
        original = self.store.lexical("A-17", {"source": source})[:1]
        question = "A-17 시제품 승인과 양산 BOM 비교"
        plan = parse_evidence_gap({"sufficient": False, "missing_aspects": ["양산 BOM"],
                                   "follow_up_queries": ["양산 BOM"]}, question)
        self.ai.rewrite_model = "fixture"
        with (patch("mailsearch.server.rewritten_evidence", return_value=(original, "keyword", {"source": source}, 2)),
              patch.object(self.ai, "diagnose_evidence_gap", return_value=plan),
              patch.dict(os.environ, {"MAILSEARCH_AGENTIC_RETRY": "1"})):
            connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=8)
            try:
                connection.request("POST", "/api/answer/stream",
                                   json.dumps({"query": question, "filters": {"source": source}}),
                                   {"X-Mail-Session": self.app.token, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                events = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
            finally:
                connection.close()
        retrieval, = [event for event in events if event["stage"] == "retrieving"]
        self.assertEqual(retrieval["pass"], 2)
        self.assertEqual(events[-1]["unknowns"], ["양산 BOM"])

    def test_rewrite_scope_fallback_on_local_answer_and_external_preview(self):
        source = self.import_mail()
        query = "A-17 납품 승인 근거와 변경 이력"
        plan = parse_rewrite({"sub_queries": ["A-17 납품 승인", "A-17 변경 이력"], "keywords": ["A-17"],
                              "filters": {"date_from": "2020-01-01", "date_to": "2020-12-31"}}, query)
        self.app.external = ExternalAI("https://example.invalid/v1/chat/completions", "fixture", "key")
        payload = {"query": query, "filters": {"source": source}}
        with patch.object(self.ai, "rewrite_query", return_value=plan) as rewrite:
            status, local = self.call("/api/answer", payload)
            self.assertEqual(status, 200)
            self.assertTrue(local["results"])
            self.assertTrue(all(row["source"] == source for row in local["results"]))
            status, preview = self.call("/api/external/preview", payload)
            self.assertEqual(status, 200)
            self.assertTrue(preview["excerpts"])
            self.assertTrue(all(item["source"]["source"] == source for item in preview["excerpts"]))
            self.assertEqual(rewrite.call_count, 2)

    def test_question_filters_use_metadata_and_preserve_ui_selection(self):
        folder = Path(self.workspace.name) / "metadata"
        folder.mkdir()
        for sender, sent, attachment, subject in (
            ("Owner <owner@example.invalid>", "1 Dec 2025 01:00:00 -0800", True, "Atlas matching"),
            ("otherowner@example.invalid", "20 Dec 2025 12:00:00 +0000", True, "Atlas other address"),
            ("owner@example.invalid", "20 Dec 2025 12:00:00 +0000", False, "Atlas without file"),
            ("owner@example.invalid", "9 Jan 2026 12:00:00 +0000", True, "Atlas January"),
        ):
            msg = EmailMessage()
            msg["From"], msg["Date"], msg["Subject"] = sender, sent, subject
            msg.set_content(subject)
            if attachment:
                msg.add_attachment("Atlas revision notes", filename="notes.txt")
            (folder / (subject + ".eml")).write_bytes(msg.as_bytes())
        source = self.indexer.register(folder)
        self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        payload = {"query": "지난달 owner@example.invalid이 보낸 첨부가 있는 메일 Atlas",
                   "filters": {"source": source}}
        with patch("mailsearch.server.question_scope",
                   side_effect=lambda question, filters: question_scope(question, filters, today=date(2026, 1, 10))):
            for route in ("/api/search", "/api/answer"):
                status, response = self.call(route, payload)
                self.assertEqual(status, 200)
                self.assertEqual({row["subject"] for row in response["results"]}, {"Atlas matching"})
            payload["filters"].update(after="2026-01-01", sender="owner@example.invalid", attachment="yes")
            status, response = self.call("/api/search", payload)
            self.assertEqual(status, 200)
            self.assertEqual({row["subject"] for row in response["results"]}, {"Atlas January"})

    def test_local_answer_accepts_eight_cited_claims_and_model_context_cap(self):
        folder = Path(self.workspace.name) / "synthesis"
        folder.mkdir()
        quotes = [f"Atlas 단계 {index}의 점검을 진행했습니다." for index in range(1, 9)]
        for index, quote in enumerate(quotes, 1):
            msg = EmailMessage()
            msg["Subject"] = "Atlas"
            msg["Date"] = f"{index} Jan 2026 12:00:00 +0000"
            msg.set_content(quote)
            (folder / f"{index}.eml").write_bytes(msg.as_bytes())
        source = self.indexer.register(folder)
        self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        self.ai.configure_embedding("", None)
        actual_call = self.ai.call

        def local(path, payload=None, timeout=None):
            if path == "/api/show":
                return {"model_info": {"general.architecture": "test", "test.context_length": 12288}}
            if path != "/api/generate":
                return actual_call(path, payload, timeout)
            self.assertEqual(payload["options"]["num_ctx"], 8192)
            self.assertEqual(self.ai.model_context_limit, 12288)
            self.assertEqual(payload["format"]["properties"]["claims"]["maxItems"], 8)
            snippets = json.loads(payload["prompt"])["evidence"]
            claims = [{"text": quote, "citations": [{"source": row["source"], "quote": quote}]}
                      for row, quote in zip(snippets, quotes)]
            self.assertTrue(all(claim["text"] in row["text"] for claim, row in zip(claims, snippets)))
            return {"response": json.dumps({"status": "answered", "claims": claims})}

        with patch.object(self.ai, "call", side_effect=local):
            status, answer = self.call("/api/answer", {"query": "Atlas 종합", "filters": {"source": source}})
        self.assertEqual(status, 200)
        self.assertEqual(answer["status"], "answered")
        self.assertEqual([claim["text"] for claim in answer["claims"]], quotes)

    def test_compound_answer_retrieves_several_queries_and_uses_only_validated_map_notes(self):
        folder = Path(self.workspace.name) / "query-plan"
        folder.mkdir()
        for number, code in ((50, "A-17"), (70, "B-22")):
            for day in range(1, 6):
                msg = EmailMessage()
                msg["Subject"] = f"{code} 단가"
                msg["Date"] = f"{day} Jan 2026 12:00:00 +0000"
                msg["Message-ID"] = f"<{code}-{day}@example.invalid>"
                if day > 1:
                    msg["In-Reply-To"] = f"<{code}-{day - 1}@example.invalid>"
                msg.set_content(f"회신 기록 {day}.\n{code} 단가는 {number}만원입니다.")
                (folder / f"{code}-{day}.eml").write_bytes(msg.as_bytes())
        source = self.indexer.register(folder)
        self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        self.ai.configure_embedding("", None)
        requests = []
        original = self.ai.call

        def respond(path, payload=None, timeout=None):
            if path != "/api/generate":
                return original(path, payload, timeout)
            incoming = json.loads(payload["prompt"])
            rows = incoming["evidence"]
            requests.append(incoming)
            if "verified_notes" not in incoming:
                row = rows[0]
                quote = row["text"].splitlines()[-1]
                claims = [{"text": quote, "citations": [{"source": row["source"], "quote": quote}]},
                          {"text": "단가는 999만원입니다.", "citations": [{"source": row["source"], "quote": quote}]}]
            else:
                self.assertEqual(len(incoming["verified_notes"]), 2)
                self.assertTrue(all("999" not in note["text"] for note in incoming["verified_notes"]))
                self.assertEqual([item["group"] for item in rows], [1] * 5 + [2] * 5)
                claims = []
                for note in incoming["verified_notes"]:
                    row = next(row for row in rows if row["source"] in note["sources"])
                    claims.append({"text": note["text"], "citations": [
                        {"source": row["source"], "quote": row["text"].splitlines()[-1]},
                    ]})
                claims.append({"text": "단가는 999만원입니다.", "citations": claims[0]["citations"]})
            return {"response": json.dumps({"status": "answered", "claims": claims})}

        with (patch.object(self.ai, "call", side_effect=respond),
              patch("mailsearch.server.evidence_for", wraps=evidence_for) as retrieve):
            status, result = self.call("/api/answer", {"query": "A-17와 B-22 단가를 비교해줘", "filters": {"source": source}})
        self.assertEqual(status, 200)
        self.assertEqual(retrieve.call_count, 2)
        self.assertTrue(all(call.kwargs["expand"] and call.kwargs["thread_hops"] == 3 for call in retrieve.call_args_list))
        self.assertEqual(len(requests), 3)
        self.assertEqual(result["status"], "partial")
        self.assertEqual({claim["text"] for claim in result["claims"]},
                         {"A-17 단가는 50만원입니다.", "B-22 단가는 70만원입니다."})

    def test_live_answer_stream_matches_json_and_protects_the_endpoint(self):
        source = self.import_mail()
        payload = {"query": "한빛 A-17", "filters": {"source": source}}
        self.assertEqual(self.call("/api/answer/stream", payload, token=False)[0], 403)
        self.assertEqual(self.call("/api/answer/stream", {**payload, "query": ""})[0], 400)
        with patch("mailsearch.server.evidence_for", wraps=evidence_for) as retrieve:
            expected = self.call("/api/answer", payload)[1]
        retrieve.assert_called_once()
        self.assertNotIn("session_id", expected)
        release = threading.Event()
        actual_answer = self.ai.answer

        def generation(*args, **options):
            if not release.wait(4):
                raise RuntimeError("stream_stalled")
            return actual_answer(*args, **options)

        connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=8)
        try:
            with patch.object(self.ai, "answer", side_effect=generation):
                connection.request("POST", "/api/answer/stream", json.dumps(payload),
                                   {"X-Mail-Session": self.app.token, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
                events = []
                for stage in ("planning", "retrieving", "synthesizing"):
                    self.assertEqual(response.readline(), f"event: {stage}\n".encode())
                    events.append(json.loads(response.readline()[6:]))
                    self.assertEqual(response.readline(), b"\n")
                release.set()
                events.extend(json.loads(line[6:]) for line in response.read().decode().splitlines()
                              if line.startswith("data: "))
        finally:
            release.set()
            connection.close()
        self.assertEqual([event["stage"] for event in events],
                         ["planning", "retrieving", "synthesizing", "validating", "done"])
        self.assertGreater(events[1]["count"], 0)
        self.assertEqual(events[1]["queries"], 1)
        self.assertEqual({key: events[-1][key] for key in expected}, expected)
        self.assertTrue(events[-1]["session_id"])

    def test_answer_stream_reports_index_error_as_an_sse_result(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=8)
        try:
            with patch("mailsearch.server.evidence_for", side_effect=sqlite3.OperationalError):
                connection.request("POST", "/api/answer/stream", json.dumps({"query": "Atlas"}),
                                   {"X-Mail-Session": self.app.token, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                events = [json.loads(line[6:]) for line in response.read().decode().splitlines()
                          if line.startswith("data: ")]
        finally:
            connection.close()
        self.assertEqual([event["stage"] for event in events], ["planning", "done"])
        self.assertEqual(events[-1]["status"], "index_unavailable")
        self.assertEqual(events[-1]["claims"], [])

    def test_closed_answer_stream_stops_before_validation_and_releases_generation_slot(self):
        source = self.import_mail()
        started, release = threading.Event(), threading.Event()

        def generation(_question, evidence, _context, **_options):
            started.set()
            release.wait(4)
            return {"status": "insufficient", "claims": []}

        connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=8)
        response = None
        with (patch.object(self.ai, "answer", side_effect=generation),
              patch("mailsearch.server.validate", wraps=validate) as checked):
            try:
                connection.request("POST", "/api/answer/stream",
                                   json.dumps({"query": "한빛 A-17", "filters": {"source": source}}),
                                   {"X-Mail-Session": self.app.token, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(started.wait(3))
                response.close()
                connection.close()
            finally:
                release.set()
                if response is not None:
                    response.close()
                connection.close()
            deadline = time.monotonic() + 3
            while not self.app.generation.acquire(blocking=False):
                if time.monotonic() >= deadline:
                    self.fail("Cancelled generation did not release its slot")
                time.sleep(.01)
            self.app.generation.release()
        checked.assert_not_called()
        self.assertTrue(all(not turns for _, turns in self.app.answer_sessions.items.values()))

    def test_follow_up_prioritizes_live_scoped_citations_and_reset_clears_context(self):
        source = self.import_mail()
        base = {"filters": {"source": source}, "session_id": ""}
        first = self.call("/api/answer", {**base, "query": "한빛 A-17"})[1]
        cited = first["claims"][0]["citations"][0]["passage"]
        key = first["session_id"]
        follow = {**base, "session_id": key, "query": "그중 단가만 다시 비교해줘"}
        with patch.object(self.ai, "answer", wraps=self.ai.answer) as ask:
            result = self.call("/api/answer", follow)[1]
            self.assertEqual(result["session_id"], key)
            self.assertEqual(ask.call_args.args[1][0]["passage"], cited)
            self.assertIn("한빛 A-17", ask.call_args.args[2])
            ask.reset_mock()
            restricted = self.call("/api/answer", {**follow, "filters": {"source": 999999}})[1]
            self.assertEqual(restricted["results"], [])
            ask.assert_not_called()
        self.assertEqual(self.call("/api/answer/reset", {"session_id": key})[0], 200)
        fresh = self.call("/api/answer", follow)[1]
        self.assertNotEqual(fresh["session_id"], key)

    def test_follow_up_limits_named_replies_to_the_previous_thread(self):
        folder = Path(self.workspace.name) / "follow-up-replies"
        folder.mkdir()
        messages = [
            ("AtlasAnchor", "구매팀", "atlas", "", "견적서 검토를 요청합니다."),
            ("검토 전달", "박 대리", "review", "atlas", "담당자에게 전달했습니다."),
            ("최종 검토 회신", "김 부장", "approval", "review", "검수 조건을 승인했습니다."),
            ("Separate quote", "김 부장", "other", "", "별도 발주 건을 승인했습니다."),
        ]
        for day, (subject, sender, key, parent, body) in enumerate(messages, 1):
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = f"{sender} <sender{day}@example.invalid>"
            msg["Date"] = f"{day} Jan 2026 12:00:00 +0000"
            msg["Message-ID"] = f"<{key}@example.invalid>"
            if parent:
                msg["In-Reply-To"] = f"<{parent}@example.invalid>"
            msg.set_content(body)
            (folder / f"{day}.eml").write_bytes(msg.as_bytes())
        source = self.indexer.register(folder)
        self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        self.ai.configure_embedding("", None)

        def answer(question, evidence, _context, **_options):
            subject = "AtlasAnchor" if question == "AtlasAnchor" else "최종 검토 회신"
            row = next(row for row in evidence if row["subject"] == subject)
            quote = row["text"].strip()
            return {"status": "answered", "claims": [
                {"text": quote, "citations": [{"source": row["passage"], "quote": quote}]},
            ]}

        with patch.object(self.ai, "answer", side_effect=answer):
            first = self.call("/api/answer", {"query": "AtlasAnchor", "session_id": "", "filters": {"source": source}})[1]
            follow = {"query": "방금 메일에서 김 부장 회신만 확인해줘", "session_id": first["session_id"],
                      "filters": {"source": source}}
            status, result = self.call("/api/answer", follow)
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "answered")
            self.assertEqual([row["subject"] for row in result["results"]], ["최종 검토 회신"])
            self.assertEqual(result["claims"][0]["text"], "검수 조건을 승인했습니다.")
            self.assertEqual(self.call("/api/answer/reset", {"session_id": first["session_id"]})[0], 200)
            fresh = self.call("/api/answer", follow)[1]
            self.assertEqual(fresh["results"], [])
            self.assertNotEqual(fresh["session_id"], first["session_id"])
            legacy = {key: value for key, value in follow.items() if key != "session_id"}
            with patch("mailsearch.server.evidence_for", wraps=evidence_for) as retrieve:
                self.assertEqual(self.call("/api/answer", legacy)[0], 200)
            retrieve.assert_called_once()

    def test_validation_preserves_original_whitespace_and_checks_each_synthesis_clause(self):
        source = self.import_mail()
        row = self.store.lexical("납품", {"source": source})[0]
        first = {**row, "text": "조달팀은 예산 300만원을\n  보류했습니다."}
        second = {**row, "passage": row["passage"] + 1000,
                  "text": "품질팀은 검수 2건을 승인했습니다."}
        quotes = [{"source": first["passage"], "quote": "조달팀은 예산 300만원을 보류했습니다."},
                  {"source": second["passage"], "quote": second["text"]}]
        claim = {"text": "조달팀은 예산 300만원을 보류했습니다. 품질팀은 검수 2건을 승인했습니다.",
                 "citations": quotes}
        result = validate({"status": "answered", "claims": [claim]}, [first, second])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["claims"][0]["citations"][0]["quote"], first["text"])
        bad = {**claim, "text": "품질팀은 예산 300만원을 승인했습니다."}
        result = validate({"status": "answered", "claims": [bad]}, [first, second])
        self.assertEqual(result["status"], "insufficient")

    def test_search_access_is_deferred_without_exposing_unverified_matches(self):
        self.import_mail()
        payload = {"query": "A-17", "mode": "quick", "defer_access": True, "filters": {"tab": "mail"}}
        with patch("mailsearch.store.source_readable", return_value=True) as check:
            status, first = self.call("/api/search", payload)
            self.assertEqual(status, 200)
            self.assertEqual(first["results"], [])
            check.assert_not_called()
            self.assertEqual(self.call("/api/search-access", {"ticket": first["access_ticket"]}, token=False)[0], 403)
            status, released = self.call("/api/search-access", {"ticket": first["access_ticket"]})
            self.assertEqual((status, len(released["results"])), (200, 3))
            check.assert_called_once()
            status, warm = self.call("/api/search", payload)
            self.assertEqual((status, warm), (200, released))
            check.assert_called_once()
            self.assertEqual(self.call("/api/search-access", {"ticket": first["access_ticket"]})[0], 409)
        self.store.access_cache.clear()
        status, first = self.call("/api/search", payload)
        self.assertEqual(status, 200)
        with patch("mailsearch.store.source_readable", return_value=False):
            self.assertFalse(self.call("/api/search-access", {"ticket": first["access_ticket"]})[1]["results"])

    def test_search_access_returns_ready_sources_while_another_is_blocked(self):
        for name in ("fast", "slow"):
            path = Path(self.workspace.name) / f"{name}.eml"
            message = EmailMessage()
            message["Subject"] = "Atlas " + name
            message.set_content(f"Test {name}")
            path.write_bytes(message.as_bytes())
            source = self.indexer.register(path)
            self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        release = threading.Event()
        blocked = threading.Event()

        def probe(path, kind):
            if Path(path).stem == "slow":
                blocked.set()
                return release.wait(4)
            return True

        payload = {"query": "Atlas", "mode": "quick", "defer_access": True}
        with patch("mailsearch.store.source_readable", side_effect=probe) as checks:
            try:
                first = self.call("/api/search", payload)[1]
                started = time.monotonic()
                _, verified = self.call("/api/search-access", {"ticket": first["access_ticket"]})
                self.assertLess(time.monotonic() - started, 2)
                self.assertTrue(blocked.is_set())
                self.assertTrue(verified["access_pending"])
                self.assertEqual([row["subject"] for row in verified["results"]], ["Atlas fast"])
            finally:
                release.set()
            deadline = time.monotonic() + 2
            while self.store.access_inflight and time.monotonic() < deadline:
                time.sleep(.01)
            warm = self.call("/api/search", payload)[1]
            self.assertEqual({row["subject"] for row in warm["results"]}, {"Atlas fast", "Atlas slow"})
            self.assertEqual(checks.call_count, 2)

    def test_vector_lookup_plan_uses_model_and_passage_keys(self):
        with self.store.connection() as conn:
            lines = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + VECTOR_LOOKUP_SQL, ("embed", "[1,2,3]"))]
        for table in ("vectors", "r", "v"):
            fields = "model=? AND digest=?" if table == "v" else "model=? AND passage=?"
            self.assertTrue(any(f"SEARCH {table} USING PRIMARY KEY ({fields})" in line for line in lines), lines)

    def test_query_embeddings_are_reused_until_configuration_changes(self):
        source = self.import_mail()
        with patch.object(self.ai, "embed_local", wraps=self.ai.embed_local) as embed:
            payload = {"query": "A-17", "mode": "hybrid", "filters": {"source": source}}
            for _ in range(2):
                self.assertEqual(self.call("/api/search", payload)[0], 200)
            embed.assert_called_once()
            self.ai.configure_embedding("embed", None)
            self.assertEqual(self.call("/api/search", payload)[0], 200)
            self.assertEqual(embed.call_count, 2)

    def test_outlook_attachment_copies_get_a_shared_content_key(self):
        message = EmailMessage()
        message.set_content("테스트 회신")
        message.add_attachment(b"same attachment", maintype="text", subtype="plain", filename="Shared.txt")
        part, = message.iter_attachments()
        mail = {"subject": "Outlook 공유 첨부", "sender": "", "recipient": "", "sent": "", "folder": "/",
                "attachments": [("Shared.txt", part)], "message_id": "", "reply_ids": "", "body": "본문"}
        from mailsearch.extract import extract_bytes
        with (self.store.connection() as conn, patch("mailsearch.extract.extract_bytes", wraps=extract_bytes) as extract):
            source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)",
                                  (str(self.mail),)).lastrowid
            for item in ("mail1.eml", "mail2.eml"):
                mail_id = self.store.add_mail(conn, source, 1, item, mail)
                self.indexer.index_outlook_attachments(conn, mail_id, mail, self.mail, threading.Event())
            keys = [row[0] for row in conn.execute("SELECT embedding_key FROM passages WHERE embedding_key IS NOT NULL")]
            self.assertEqual((len(keys), len(set(keys))), (2, 1))
            extract.assert_called_once()

    def test_fifty_mails_share_one_attachment_embedding_and_keep_their_origins(self):
        folder = Path(self.workspace.name) / "duplicate-attachments"
        folder.mkdir()
        body = "오늘 송부합니다.\n\n> 오래된 검토 98765\n\n새 회신입니다.\n-- \n서명비밀 19573"
        data = "공유 첨부문단 30719".encode()
        copies = 50
        for idx in range(copies):
            msg = EmailMessage()
            msg["Subject"] = f"Message {idx}"
            msg["From"] = f"sender{idx}@example.invalid"
            msg["Date"] = f"{idx % 28 + 1} {'May' if idx < 28 else 'Jun'} 2026 12:00:00 +0000"
            msg.set_content(body)
            msg.add_attachment(data, maintype="text", subtype="plain", filename="shared.txt")
            (folder / f"mail{idx}.eml").write_bytes(msg.as_bytes())
        from mailsearch.extract import extract_bytes
        with (patch("mailsearch.extract.extract_bytes", wraps=extract_bytes) as extract,
              patch.object(self.ai, "embed", wraps=self.ai.embed) as embed):
            source = self.indexer.register(folder)
            self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
            extract.assert_called_once()
            inputs = [value for call in embed.call_args_list for value in call.args[0] if value.endswith(data.decode())]
            self.assertEqual(inputs, ["[shared.txt | 본문]\n" + data.decode()])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embedding_values").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embedding_refs").fetchone()[0], copies)
            self.assertTrue(all("98765" in row[0] for row in conn.execute("SELECT body FROM mails WHERE source=?", (source,))))
        filters = {"source": source}
        self.assertFalse(self.store.lexical("98765", filters))
        self.assertFalse(self.store.lexical("19573", filters))
        self.assertEqual(len({row["id"] for row in self.store.lexical("30719", filters)}), copies)
        rows = self.store.semantic_contextual([1, .05, .12], "embed", filters, limit=4 * copies)
        self.assertEqual(len({row["id"] for row in rows if row["attachment"] and "30719" in row["text"]}), copies)

    def test_http11_reuses_sqlite_on_one_socket(self):
        self.import_mail()
        with patch.object(self.store, "open_connection", wraps=self.store.open_connection) as opened:
            connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=5)
            try:
                for _ in range(2):
                    connection.request("GET", "/api/sources?scan=0", headers={"X-Mail-Session": self.app.token})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
            finally:
                connection.close()
            opened.assert_called_once()

    def test_local_embedding_probes_once_until_recovery(self):
        ai = LocalAI("test", "embed")
        vector = [[1., .05, .12]]
        with (patch.object(ai, "ensure_server") as server,
              patch.object(ai, "ensure_model", return_value="embed:latest") as model,
              patch.object(ai, "call", side_effect=[{"embeddings": vector}, {"embeddings": vector},
                                                     OSError("offline"), {"embeddings": vector}]) as call):
            for _ in range(2):
                self.assertEqual(ai.embed_local("embed", ["query"]), vector)
            self.assertEqual(call.call_args.args[1]["keep_alive"], -1)
            server.assert_called_once()
            model.assert_called_once_with("embed")
            with self.assertRaises(OSError):
                ai.embed_local("embed", ["query"])
            self.assertEqual(ai.embed_local("embed", ["query"]), vector)
            self.assertEqual(model.call_count, 2)

    def test_answer_expands_thread_and_delivers_sources_in_chronological_order(self):
        folder = Path(self.workspace.name) / "thread"
        folder.mkdir()
        for i, subject in enumerate(("Original need", "Changed subject", "Another subject", "UniqueTrace"), 1):
            msg = EmailMessage()
            msg["From"] = "sender@example.invalid"
            msg["Message-ID"] = f"<thread-{i}@example.invalid>"
            msg["Subject"] = subject
            msg["Date"] = f"0{i} Apr 2026 09:00:00 +0900"
            if i > 1:
                msg["In-Reply-To"] = f"<thread-{i-1}@example.invalid>"
            msg.set_content(f"본문 단계 {i}입니다.")
            (folder / f"{i}.eml").write_bytes(msg.as_bytes())
        self.ai.configure_embedding("", None)
        source = self.indexer.register(folder)
        self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        with (patch.object(self.ai, "ensure_server"),
              patch.object(self.ai, "ensure_model"),
              patch.object(self.ai, "call", return_value={"response": '{"status":"insufficient","claims":[]}'}) as generate,
              self.assertLogs("mailsearch.runtime", level="INFO") as timings):
            status, _ = self.call("/api/answer", {"query": "UniqueTrace", "filters": {"source": source}})
            self.assertEqual(status, 200)
            prompt = json.loads(generate.call_args.args[1]["prompt"])
            snippets = [item["text"] for item in prompt["evidence"]]
            self.assertEqual([text.split("\n")[2] for text in snippets],
                             ["Subject: " + name for name in ("Original need", "Changed subject", "Another subject", "UniqueTrace")])
            self.assertIn("mode=answer", timings.output[-1])
            self.assertIn("generation_ms=", timings.output[-1])

    def test_status_poll_and_unrelated_keyword_query_do_not_probe_other_roots(self):
        source = self.import_mail()
        other = Path(self.workspace.name) / "slow-share"
        other.mkdir()
        with self.store.connection() as conn:
            conn.execute("INSERT INTO sources(path,kind,state) VALUES(?, 'eml_dir','ready')", (str(other),))
        from mailsearch.store import source_readable
        with (patch.object(self.indexer, "pending_updates", side_effect=AssertionError("recursive scan")),
              patch("mailsearch.store.source_readable", wraps=source_readable) as probe):
            self.assertEqual(self.call("/api/sources?scan=0")[0], 200)
            probe.assert_not_called()
            result = self.call("/api/search", {"query": "A-17", "mode": "keyword", "filters": {"source": source}})
            self.assertTrue(result[1]["results"])
            self.assertNotIn(str(other), [check.args[0] for check in probe.call_args_list])

    def test_quick_search_uses_metadata_without_ai_and_keeps_body_search(self):
        source = self.import_mail()
        with (patch.object(self.ai, "semantic_search") as semantic,
              patch.object(self.ai, "rerank_candidates") as rerank):
            def search(query, mode="quick", **filters):
                status, result = self.call("/api/search", {
                    "query": query, "mode": mode, "filters": {"source": source, "tab": "mail", **filters},
                })
                self.assertEqual(status, 200)
                return result

            self.assertEqual(len(search("구매팀")["results"]), 4)
            self.assertEqual(len(search("A-17")["results"]), 3)
            self.assertEqual(len(search('"A-17" & "승인"')["results"]), 1)
            quick = search("도면")
            self.assertEqual(quick["mode"], "quick")
            self.assertEqual([row["attachment"] for row in quick["results"]], ["B-19_도면.hwp"])
            self.assertEqual(self.call(f"/api/evidence/{quick['results'][0]['passage']}")[0], 200)
            self.assertFalse(search("반도체냉각판")["results"])
            self.assertTrue(search("반도체냉각판", mode="keyword")["results"])
            self.assertEqual(len(search("한빛", body="최종 승인")["results"]), 1)
            self.assertFalse(search("도면", attachment="no")["results"])
            semantic.assert_not_called()
            rerank.assert_not_called()

    def test_quick_search_backfills_old_data_and_obeys_visibility(self):
        source = self.import_mail()
        with self.store.connection() as conn:
            conn.execute("DROP TRIGGER quick_terms_remove")
            conn.execute("DROP TABLE quick_terms")
        reopened = Store(self.store.root)
        filters = {"source": source, "tab": "mail"}
        self.assertEqual(len(reopened.quick_search("A-17", filters)), 3)
        with reopened.connection() as conn:
            current = conn.execute("SELECT active FROM sources WHERE id=?", (source,)).fetchone()[0]
            first = conn.execute("SELECT id FROM mails WHERE source=? AND subject LIKE '%견적%'", (source,)).fetchone()[0]
            new_id = reopened.adopt_mail(conn, current + 1, first)
            conn.execute("UPDATE sources SET pending=? WHERE id=?", (current + 1, source))
        self.assertEqual(reopened.quick_search("도면", filters)[0]["id"], first)
        with reopened.connection() as conn:
            conn.execute("UPDATE sources SET active=pending,pending=NULL WHERE id=?", (source,))
            conn.execute("DELETE FROM mails WHERE source=? AND version=?", (source, current))
            self.assertIsNone(conn.execute("SELECT rowid FROM quick_terms WHERE rowid=?", (first,)).fetchone())
        self.assertEqual(reopened.quick_search("도면", filters)[0]["id"], new_id)
        renamed = self.mail.with_name("source-revoked")
        self.mail.rename(renamed)
        self.assertFalse(reopened.quick_search("도면", filters))

    def test_keyword_search_bypasses_ai_while_hybrid_search_uses_it(self):
        self.import_mail()
        with (patch.object(self.ai, "semantic_search", return_value=[]) as semantic,
              patch.object(self.ai, "rerank_candidates", side_effect=lambda _, rows: rows) as rerank):
            query = {"query": "A-17", "filters": {"tab": "mail"}}
            status, found = self.call("/api/search", {**query, "mode": "keyword"})
            self.assertEqual(status, 200)
            self.assertTrue(found["results"])
            semantic.assert_not_called()
            rerank.assert_not_called()
            status, hybrid = self.call("/api/search", {**query, "mode": "hybrid"})
            self.assertEqual(status, 200)
            self.assertEqual({row["mail"] for row in hybrid["results"]}, {row["mail"] for row in found["results"]})
            semantic.assert_called_once()
            rerank.assert_not_called()
            self.assertEqual(self.call("/api/search", {**query, "mode": "hybrid", "rerank": True})[0], 200)
            rerank.assert_called_once()

    def test_memory_search_groups_replies_and_matches_comma_and_without_fts(self):
        folder = Path(self.workspace.name) / "memory-mail"
        folder.mkdir()
        for name, subject, body in (
            ("first", "Re: Atlas", "assembly power work"),
            ("second", "FW: 답장: Atlas", "assembly work"),
            ("third", "답장: Atlas", "power work\n\n-----Original Message-----\nassembly"),
        ):
            mail = EmailMessage()
            mail["Subject"] = subject
            mail["From"] = "Operations <ops@example.invalid>"
            mail.set_content(body)
            (folder / f"{name}.eml").write_bytes(mail.as_bytes())
        source = self.indexer.register(folder)
        self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        filters = {"source": source, "tab": "mail"}
        with (patch.object(self.store, "quick_search", side_effect=AssertionError("quick FTS")),
              patch.object(self.store, "lexical", side_effect=AssertionError("body FTS")),
              patch.object(self.store, "snippets", side_effect=AssertionError("FTS snippet"))):
            status, result = self.call("/api/search", {"mode": "keyword", "query": "ASSEMBLY, POWER", "filters": filters})
            self.assertEqual(status, 200)
            self.assertEqual([row["subject"] for row in result["results"]], ["Re: Atlas"])
            status, result = self.call("/api/search", {"mode": "quick", "query": "LAS", "filters": filters})
            self.assertEqual((status, len(result["results"])), (200, 3))
            self.assertEqual(set(self.store.memory_index.conversation(source, "전달: Re: ATLAS")),
                             {row["mail"] for row in result["results"]})
            _, result = self.call("/api/search", {"mode": "keyword", "query": "", "filters": {**filters, "body": "power,assembly"}})
            self.assertEqual([row["subject"] for row in result["results"]], ["Re: Atlas"])
        reopened = Store(self.store.root)
        rows = reopened.memory_search("assembly, power", filters, quick=False)
        self.assertEqual([row["subject"] for row in rows], ["Re: Atlas"])

    def test_eml_refresh_reuses_file_signature_and_updates_only_its_cache(self):
        from mailsearch.indexer import parse_mail

        source = self.import_mail()
        other = Path(self.workspace.name) / "unrelated.eml"
        other.write_text("Subject: Separate archive\n\nFixture body.", encoding="utf-8")
        unrelated = self.indexer.register(other)
        self.assertEqual(await_source(self.store, unrelated, {"ready", "error"})["state"], "ready")
        snapshot = self.store.memory_index.snapshot(unrelated)[0]
        fresh = self.mail / "new-snapshot.eml"
        fresh.write_text("Subject: Re: index marker\n\nFirst visible result.", encoding="utf-8")
        (self.mail / "request.eml").unlink()
        with patch("mailsearch.indexer.parse_mail", wraps=parse_mail) as read:
            self.indexer.command(source, "refresh")
            self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")
        self.assertEqual([call.args[0].name for call in read.call_args_list], [fresh.name])
        self.assertIs(self.store.memory_index.snapshot(unrelated)[0], snapshot)
        self.assertTrue(self.store.memory_search("index marker", {"source": source}, quick=True))
        self.assertEqual(len(self.store.memory_search("A-17", {"source": source}, quick=True)), 2)
        self.indexer.command(source, "remove")
        self.assertEqual(self.store.memory_index.snapshot(source), ())

    def test_mapi_refresh_opens_only_new_or_moved_items_and_keeps_passages(self):
        class MapiFailure(Exception):
            pass

        class Table:
            def __init__(self, entries):
                self.entries = entries
                self.Columns = MagicMock()

            @property
            def EndOfTable(self):
                return not self.entries

            def GetArray(self, count):
                batch, self.entries = self.entries[:count], self.entries[count:]
                return [(entry,) for entry in batch]

        def folder(path, entries=(), children=()):
            return SimpleNamespace(FolderPath=path, Folders=children, GetTable=lambda _: Table(entries))

        def item(path, subject):
            return SimpleNamespace(Parent=SimpleNamespace(FolderPath=path), Subject=subject, Body="Fixture content.",
                                   SenderName="Operations", To="Warehouse", CC="", SentOn=None, Attachments=[])

        original = {"same": item("/Inbox", "Same"), "move": item("/Inbox", "Move"),
                    "deleted": item("/Inbox", "Deleted")}
        changed = {"new": item("/Inbox", "New"), "move": item("/Archive", "Moved")}
        namespace = SimpleNamespace(GetItemFromID=MagicMock(side_effect=lambda entry, _: original[entry]))
        session = OutlookStore(namespace, "store-id", folder("/", children=[folder("/Inbox", tuple(original))]), MapiFailure)
        with (patch("mailsearch.indexer.outlook_modules"), patch("mailsearch.indexer.outlook_store", return_value=nullcontext(session)),
              patch("mailsearch.store.source_readable", return_value=True)):
            status, data = self.call("/api/sources", {"kind": "outlook_mapi"})
            self.assertEqual(status, 202)
            source = data["source"]
            first = await_source(self.store, source, {"ready", "error"})
            self.assertEqual((first["state"], first["done"]), ("ready", 3))
            before = self.store.memory_search("same", {"source": source}, quick=True)[0]

            namespace.GetItemFromID.reset_mock()
            namespace.GetItemFromID.side_effect = lambda entry, _: changed[entry]
            session.root = folder("/", children=[folder("/Inbox", ("same", "new")), folder("/Archive", ("move",))])
            self.indexer.command(source, "refresh")
            current = await_source(self.store, source, {"ready", "error"})
            self.assertEqual((current["state"], current["done"]), ("ready", 3))
            self.assertCountEqual([call.args for call in namespace.GetItemFromID.call_args_list],
                                  [("move", "store-id"), ("new", "store-id")])
            rows = {row["subject"]: row for row in self.store.memory_search("", {"source": source}, quick=True)}
            self.assertEqual(set(rows), {"Same", "Moved", "New"})
            self.assertEqual(rows["Moved"]["folder"], "/Archive")
            self.assertEqual(rows["Same"]["passage"], before["passage"])
            self.assertEqual(current["embedding_done"], 3)

            namespace.GetItemFromID.reset_mock()
            self.indexer.command(source, "refresh")
            again = await_source(self.store, source, {"ready", "error"})
            self.assertEqual(again["active"], current["active"])
            namespace.GetItemFromID.assert_not_called()

    def test_mapi_registration_reports_missing_optional_component(self):
        with patch("mailsearch.indexer.outlook_modules", side_effect=OutlookError("outlook_pywin32")):
            self.assertEqual(self.call("/api/sources", {"kind": "outlook_mapi"}), (409, {"error": "outlook_pywin32"}))

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

    def test_finalization_reports_progress_before_atomic_switch(self):
        source = self.import_mail()
        search = {"query": "A-17", "mode": "quick", "filters": {"source": source}}
        original = self.call("/api/search", search)[1]["results"]
        approved = next(row["passage"] for row in original if row["sent"].startswith("2026-04-10"))
        active = self.store.source(source)["active"]
        (self.mail / "approved.eml").unlink()
        building, built = threading.Event(), threading.Event()
        update = self.indexer.update_finalization

        def observe(source, phase, done=None, total=None):
            update(source, phase, done, total)
            if phase == "build_memory" and done == total:
                building.set()
                built.wait(5)

        def verify_status(phase, done, total):
            code, data = self.call("/api/sources?scan=0")
            self.assertEqual(code, 200)
            current = data["sources"][0]
            self.assertEqual((current["index_stage"], current["active"]), ("finalizing", active))
            progress = current["finalization"]
            self.assertEqual((progress["phase"], progress["done"], progress["total"]), (phase, done, total))
            self.assertGreaterEqual(progress["elapsed"], progress["phase_elapsed"])
            self.assertGreaterEqual(progress["phase_elapsed"], progress["idle"])
            self.assertEqual(self.call(f"/api/evidence/{approved}")[0], 200)
            self.assertEqual(self.call("/api/search", search)[1]["results"], original)

        with patch.object(self.indexer, "update_finalization", side_effect=observe):
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            try:
                self.assertTrue(building.wait(5))
                verify_status("build_memory", 3, 3)
            finally:
                built.set()
            self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")

        current = self.call("/api/sources?scan=0")[1]["sources"][0]
        self.assertNotEqual(current["active"], active)
        self.assertIsNone(current["finalization"])
        self.assertEqual(self.call(f"/api/evidence/{approved}")[0], 404)
        updated = self.call("/api/search", search)[1]["results"]
        self.assertEqual(len(updated), 2)
        self.assertFalse(any(row["sent"].startswith("2026-04-10") for row in updated))

    def test_search_switches_before_cleanup_and_restart_reclaims_retired_batches(self):
        source = self.import_mail()
        active = self.store.source(source)["active"]
        old_passage = self.call("/api/search", {"query": '"최종 승인"'})[1]["results"][0]["passage"]
        (self.mail / "approved.eml").unlink()
        cleaning, released = threading.Event(), threading.Event()
        cleanup = self.indexer.cleanup_retired

        def retired_count():
            with self.store.connection() as conn:
                return conn.execute("SELECT count(*) FROM mails WHERE source=? AND version=?",
                                    (source, active)).fetchone()[0]

        def interrupt_after_one_batch(source):
            cleanup(source)
            cleaning.set()
            released.wait(5)
            raise sqlite3.OperationalError("simulated cleanup interruption")

        with (patch("mailsearch.indexer.RETIRED_MAIL_BATCH_SIZE", 1),
              patch.object(self.indexer, "cleanup_retired", side_effect=interrupt_after_one_batch),
              patch.object(self.ai, "embed", side_effect=AssertionError("existing embeddings must be reused")),
              self.assertLogs("mailsearch.runtime", level="WARNING")):
            self.indexer.command(source, "refresh")
            worker = self.indexer.worker
            try:
                self.assertTrue(cleaning.wait(5))
                row = self.call("/api/sources?scan=0")[1]["sources"][0]
                self.assertEqual((row["state"], row["counts"]), ("ready", {"mail": 3}))
                self.assertNotEqual(row["active"], active)
                self.assertGreater(row["embedding_done"], 0)
                self.assertEqual(row["embedding_done"], row["embedding_total"])
                self.assertEqual(retired_count(), 3)
                self.assertEqual(self.call(f"/api/evidence/{old_passage}")[0], 404)
                for mode in ("quick", "keyword"):
                    self.assertEqual(len(self.call("/api/search", {"query": "A-17", "mode": mode})[1]["results"]), 2)
            finally:
                released.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

        restarted = Indexer(self.store, ai=self.ai)
        self.addCleanup(restarted.stop)
        restarted.wake()
        deadline = time.monotonic() + 5
        while restarted.has_work() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(retired_count(), 0)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT count(*) FROM searchable_vectors").fetchone()[0],
                             row["embedding_total"])

    def test_pause_during_finalization_keeps_previous_index(self):
        source = self.import_mail()
        active = self.store.source(source)["active"]
        building, released = threading.Event(), threading.Event()
        update = self.indexer.update_finalization

        def observe(source, phase, done=None, total=None):
            update(source, phase, done, total)
            if phase == "build_memory" and done == 0:
                building.set()
                released.wait(5)

        with patch.object(self.indexer, "update_finalization", side_effect=observe):
            self.indexer.command(source, "refresh")
            try:
                self.assertTrue(building.wait(5))
                response = []
                event = self.indexer.events[source]
                pause = threading.Thread(target=lambda: response.append(
                    self.call("/api/source-action", {"source": source, "action": "pause"})[0]))
                pause.start()
                self.assertTrue(event.wait(3))
            finally:
                released.set()
            pause.join(5)
            self.assertEqual(response, [202])
            current = await_source(self.store, source, {"paused", "error"})
            self.assertEqual((current["state"], current["active"]), ("paused", active))
            self.assertIsNotNone(current["pending"])
            with self.store.connection() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM mails WHERE source=? AND version=?",
                                              (source, active)).fetchone()[0], 4)
            self.assertIsNone(self.call("/api/sources?scan=0")[1]["sources"][0]["finalization"])

    def test_legacy_vector_migration_indexes_passage_deletes_without_rewriting_vectors(self):
        self.import_mail()
        with self.store.connection() as conn:
            conn.execute("DROP INDEX vector_passage")
            passage = conn.execute("SELECT min(id) FROM passages").fetchone()[0]
            conn.execute("INSERT INTO vectors(passage,model,value) VALUES(?,?,?)", (passage, "legacy-model", b"vector"))
            legacy_plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM passages WHERE id=?", (passage,))
            self.assertIn("SCAN vectors", [row["detail"] for row in legacy_plan])
        upgraded = Store(self.store.root)
        with upgraded.connection() as conn:
            plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM passages WHERE id=?", (passage,))
            self.assertTrue(any("vector_passage (passage=?)" in row["detail"] for row in plan))
            self.assertEqual(conn.execute("SELECT value FROM searchable_vectors WHERE passage=? AND model=?",
                                          (passage, "legacy-model")).fetchone()[0], b"vector")

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

    def test_folder_mtime_change_during_embedding_keeps_completed_index(self):
        embed = self.indexer.index_embeddings

        def change_folder(source, revision, event):
            result = embed(source, revision, event)
            (self.mail / "unrelated.tmp").touch()
            return result

        self.indexer.index_embeddings = change_folder
        source = self.indexer.register(self.mail)
        row = await_source(self.store, source, {"ready", "error"})
        self.assertEqual((row["state"], row["done"], row["omitted"]), ("ready", 4, 1))
        self.assertEqual(row["embedding_done"], row["embedding_total"])
        self.assertGreater(row["embedding_done"], 0)

    def test_resume_legacy_folder_changed_with_locked_pst_keeps_vectors(self):
        (self.mail / "locked.pst").write_bytes(b"!BDN" + bytes(4) + b"SM" + bytes(2))
        legacy_signature = read_signature(self.mail)
        embed = self.indexer.index_embeddings

        def previous_version(source, revision, event):
            embed(source, revision, event)
            with self.store.connection() as conn:
                conn.execute("UPDATE sources SET signature=? WHERE id=?", (legacy_signature, source))
            info = self.mail.stat()
            os.utime(self.mail, ns=(info.st_atime_ns, info.st_mtime_ns - 10**9))
            raise ImportIssue("changed")

        with patch("mailsearch.indexer.stable_input", side_effect=PermissionError("simulated lock")):
            self.indexer.index_embeddings = previous_version
            source = self.indexer.register(self.mail)
            interrupted = await_source(self.store, source, {"error"})
        self.assertEqual((interrupted["issue"], interrupted["done"], interrupted["omitted"]), ("changed", 4, 2))
        self.assertGreater(interrupted["embedding_done"], 0)
        deadline = time.monotonic() + 5
        while source in self.indexer.events and time.monotonic() < deadline:
            time.sleep(.01)
        self.indexer.index_embeddings = embed
        with patch.object(self.ai, "embed", side_effect=AssertionError("already embedded")):
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "resume"})[0], 202)
            ready = await_source(self.store, source, {"ready", "error"})
        self.assertEqual((ready["state"], ready["active"], ready["omitted"], ready["embedding_done"]),
                         ("ready", interrupted["pending"], 2, interrupted["embedding_done"]))
        visible = self.call("/api/sources?scan=0")[1]["sources"][0]
        self.assertEqual(visible["deferred_psts"], 1)

    def test_restart_recovers_committed_index_work_and_keeps_manual_pause(self):
        started, release = threading.Event(), threading.Event()
        index_headers = self.indexer.index_headers

        def staged(*args, **kwargs):
            index_headers(*args, **kwargs)
            started.set()
            release.wait(5)

        self.indexer.index_headers = staged
        source = self.indexer.register(self.mail)
        try:
            self.assertTrue(started.wait(5))
            manual = self.indexer.register(self.mail / "approved.eml")
            self.indexer.command(manual, "pause")
            with self.store.connection() as conn:
                committed = conn.execute(
                    "SELECT id FROM mails WHERE source=? ORDER BY id", (source,),
                ).fetchall()
            self.assertEqual(len(committed), 4)
        finally:
            stopper = threading.Thread(target=self.indexer.stop)
            stopper.start()
            if source in self.indexer.events:
                self.indexer.events[source].wait(5)
            release.set()
            stopper.join(5)
        row = self.store.source(source)
        self.assertEqual((row["state"], row["done"]), ("queued", 4))

        # A hard exit leaves the last active stage in SQLite. Startup queues that
        # same pending revision too.
        with self.store.connection() as conn:
            conn.execute("UPDATE sources SET state='headers' WHERE id=?", (source,))
        reopened = Indexer(self.store, ai=self.ai)
        self.addCleanup(reopened.stop)
        reopened.wake()
        ready = await_source(self.store, source, {"ready", "error"})
        self.assertEqual((ready["state"], ready["active"], ready["done"], ready["omitted"]),
                         ("ready", row["pending"], 4, 1))
        self.assertEqual(self.store.source(manual)["state"], "paused")
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT id FROM mails WHERE source=? ORDER BY id", (source,)).fetchall(),
                             committed)

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

    def test_pst_read_reports_output_before_indexing_for_files_and_folders(self):
        self.indexer.readpst = sys.executable
        mail = (self.mail / "approved.eml").read_bytes()
        header = b"!BDN" + bytes(4) + b"SM" + bytes(2)

        for in_folder in (False, True):
            with self.subTest(in_folder=in_folder):
                folder = Path(self.workspace.name) / ("folder" if in_folder else "single")
                folder.mkdir()
                nested = folder / "archive"
                nested.mkdir()
                target = nested / "outlook.pst"
                target.write_bytes(header)
                if in_folder:
                    (folder / "a-invalid.pst").write_bytes(b"not a PST")

                started, finish = threading.Event(), threading.Event()
                process = MagicMock()
                process.__enter__.return_value = process
                process.returncode = 0
                process.poll.side_effect = lambda: 0 if finish.is_set() else None

                def launch(*args, cwd, **kwargs):
                    started.set()
                    return process

                def listing(source):
                    status, result = self.call("/api/sources")
                    self.assertEqual(status, 200)
                    return next(item for item in result["sources"] if item["id"] == source)

                with (patch("mailsearch.indexer.subprocess.Popen", side_effect=launch) as run,
                      patch("mailsearch.indexer.PST_PROGRESS_INTERVAL", .01)):
                    status, result = self.call("/api/sources", {"path": str(folder if in_folder else target)})
                    self.assertEqual(status, 202)
                    source = result["source"]
                    try:
                        self.assertTrue(started.wait(5))
                        row = listing(source)
                        self.assertEqual((row["state"], row["done"], row["updates"]), ("extracting", 0, []))
                        reading = row["pst_read"]
                        self.assertEqual(reading["item"], "archive/outlook.pst" if in_folder else "outlook.pst")
                        position = 2 if in_folder else 1
                        self.assertEqual((reading["position"], reading["total"], reading["files"]),
                                         (position, position, 0))
                        out = Path(run.call_args.kwargs["cwd"]) / "mailbox"
                        out.mkdir()
                        (out / "1").write_bytes(mail)
                        (out / "readpst.log").touch()
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            row = listing(source)
                            if row["pst_read"]["files"] == 1 and row["pst_read"]["elapsed"] >= 1:
                                break
                            time.sleep(.05)
                        self.assertEqual((row["state"], row["done"], row["pst_read"]["files"]), ("extracting", 0, 1))
                        self.assertGreaterEqual(row["pst_read"]["elapsed"], 1)
                    finally:
                        finish.set()
                    row = await_source(self.store, source, {"ready", "error", "empty"})
                    self.assertEqual((row["state"], row["done"]), ("ready", 1))
                    self.assertIsNone(listing(source)["pst_read"])

    def test_pst_embedding_reports_committed_batches_and_reuses_them_after_disconnect(self):
        target = self.mail / "progress.pst"
        target.write_bytes(b"!BDN" + bytes(4) + b"SM" + bytes(2))
        extracted = Path(self.workspace.name) / "pst-export"
        extracted.mkdir()
        for item in range(1, 31):
            message = EmailMessage()
            message["Subject"] = f"PST progress fixture {item}"
            message["Message-ID"] = f"<fixture-{item}@example.invalid>"
            message.set_content(f"Sample data for embedding progress item {item}.")
            (extracted / str(item)).write_bytes(message.as_bytes())

        second_batch = threading.Event()
        release = threading.Event()
        sizes = []

        def embed(texts, timeout):
            sizes.append(len(texts))
            if len(sizes) == 2:
                second_batch.set()
                release.wait(timeout=8)
                error = ConnectionAbortedError("private diagnostic text")
                error.winerror = 10053
                raise error
            return [[1., .05, .12] for _ in texts]

        with (patch.object(self.indexer, "export", return_value=extracted),
              patch.object(self.ai, "embed", side_effect=embed),
              self.assertLogs("mailsearch.gateway", level="ERROR") as logs):
            status, result = self.call("/api/sources", {"path": str(target)})
            self.assertEqual(status, 202)
            source = result["source"]
            try:
                self.assertTrue(second_batch.wait(timeout=8))
                status, result = self.call("/api/sources")
                self.assertEqual(status, 200)
                row = next(item for item in result["sources"] if item["id"] == source)
                self.assertEqual((row["state"], row["embedding_done"], row["embedding_total"]), ("embeddings", 24, 30))
                self.assertEqual(row["index_stage"], "embeddings")
                self.assertEqual(row["index_counts"], {"mail": 30})
                self.assertTrue(row["embedding_model_current"])
            finally:
                release.set()
            row = await_source(self.store, source, {"ready", "error"})
            self.assertEqual((row["state"], row["issue"]), ("ready", "embedding_connection_aborted"))
            self.assertEqual((row["embedding_done"], row["embedding_total"]), (24, 30))
            self.assertEqual(row["index_stage"], "complete")
            self.assertTrue(self.call("/api/search", {"query": '"Sample data"', "filters": {"source": source}})[1]["results"])
            self.assertIn("winerror=10053", logs.output[0])
            self.assertNotIn("private diagnostic text", logs.output[0])
            deadline = time.monotonic() + 3
            while source in self.indexer.events and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            row = await_source(self.store, source, {"ready", "error"})
            self.assertEqual((row["state"], row["issue"], row["embedding_done"], row["embedding_total"]),
                             ("ready", None, 30, 30))
            self.assertEqual(sizes, [24, 6, 6])
            self.ai.configure_embedding("another-model", None)
            status, result = self.call("/api/sources?scan=0")
            self.assertEqual(status, 200)
            self.assertFalse(result["sources"][0]["embedding_model_current"])

    def test_source_status_reports_header_and_attachment_progress_by_revision(self):
        from mailsearch.indexer import extract_attachment, parse_mail

        folder = Path(self.workspace.name) / "index-progress"
        folder.mkdir()
        for number in range(40):
            message = EmailMessage()
            message["Subject"] = f"Progress fixture {number}"
            message.set_content("Fixture mail body.")
            message.add_attachment(f"Attachment {number}.", filename=f"{number}.txt")
            (folder / f"{number:02d}.eml").write_bytes(message.as_bytes())

        header_waiting, release_headers = threading.Event(), threading.Event()
        attachment_waiting, release_attachments = threading.Event(), threading.Event()
        seen = 0

        def read(path, *args, **kwargs):
            if path.name == "32.eml" and not release_headers.is_set():
                header_waiting.set()
                release_headers.wait(5)
            return parse_mail(path, *args, **kwargs)

        def extract(*args, **kwargs):
            nonlocal seen
            seen += 1
            if seen == 25:
                attachment_waiting.set()
                release_attachments.wait(5)
            return extract_attachment(*args, **kwargs)

        def status():
            code, data = self.call("/api/sources?scan=0")
            self.assertEqual(code, 200)
            return data["sources"][0]

        with (patch("mailsearch.indexer.parse_mail", side_effect=read),
              patch("mailsearch.indexer.extract_attachment", side_effect=extract)):
            source = self.indexer.register(folder)
            try:
                self.assertTrue(header_waiting.wait(5))
                current = status()
                self.assertEqual((current["index_stage"], current["index_done"], current["index_total"]),
                                 ("headers", 32, 40))
                self.assertEqual(current["index_counts"], {"mail": 32})
                release_headers.set()
                self.assertTrue(attachment_waiting.wait(5))
                current = status()
                self.assertEqual((current["index_stage"], current["index_done"], current["index_total"]),
                                 ("attachments", 24, 40))
            finally:
                release_headers.set()
                release_attachments.set()
            self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "ready")

        message = EmailMessage()
        message["Subject"] = "One new file"
        message.set_content("New body.")
        message.add_attachment("New attachment.", filename="new.txt")
        (folder / "new.eml").write_bytes(message.as_bytes())
        with (patch("mailsearch.indexer.extract_attachment", side_effect=OSError),
              self.assertLogs("mailsearch.runtime", "ERROR")):
            self.indexer.command(source, "refresh")
            self.assertEqual(await_source(self.store, source, {"ready", "error"})["state"], "error")
        current = status()
        self.assertEqual((current["index_stage"], current["issue"], current["index_done"], current["index_total"]),
                         ("attachments", "disk_or_source", 40, 41))
        self.assertNotEqual(current["active"], current["pending"])
        self.assertEqual(current["index_counts"], {"mail": 41})
        self.assertEqual(current["counts"], {"mail": 81})

    def test_source_status_lists_skipped_pst_reason_beside_successful_mail(self):
        folder = Path(self.workspace.name) / "source-status"
        folder.mkdir()
        (folder / "mail.eml").write_text("Subject: A readable mail\n\nFixture body.", encoding="utf-8")
        (folder / "invalid.pst").write_bytes(b"invalid PST")
        source = self.indexer.register(folder)
        row = await_source(self.store, source, {"ready", "error"})
        self.assertEqual((row["state"], row["done"], row["omitted"]), ("ready", 1, 1))
        status, result = self.call("/api/sources?scan=0")
        self.assertEqual(status, 200)
        self.assertIn({"item": "invalid.pst", "code": "pst_invalid"}, result["sources"][0]["problems"])

    def test_background_embedding_uses_configured_timeout_and_reduces_timed_out_batch(self):
        folder = Path(self.workspace.name) / "embedding-batches"
        folder.mkdir()
        for i in range(37):
            message = EmailMessage()
            message["Subject"] = f"Batch fixture {i}"
            message.set_content("Text for background embedding.")
            (folder / f"{i}.eml").write_bytes(message.as_bytes())
        self.indexer.embedding_batch_size = 32
        self.indexer.embedding_batch_timeout = 50
        requests = []

        def slow_embed(texts, timeout):
            requests.append((len(texts), timeout))
            if len(texts) > 8:
                raise TimeoutError()
            return [[1., .05, .12] for _ in texts]

        with (patch.object(self.ai, "embed", side_effect=slow_embed), self.assertLogs("mailsearch.gateway", "WARNING")):
            source = self.indexer.register(folder)
            row = await_source(self.store, source, {"ready", "error"})
        self.assertEqual((row["state"], row["issue"], row["embedding_done"], row["embedding_total"]),
                         ("ready", None, 37, 37))
        self.assertEqual(requests, [(size, 50) for size in (32, 16, 8, 8, 8, 8, 5)])

    def test_raw_attachment_download_survives_missing_text_and_old_pst_indexes(self):
        pst = self.mail / "source.pst"
        pst.write_bytes(b"!BDN" + bytes(4) + b"SM" + bytes(2))
        message = EmailMessage()
        message["From"] = "local@example.invalid"
        message["Subject"] = "Original binary test"
        message.set_content("fixture")
        image = b"\xff\xd8\xff\xd9"
        pdf = b"%PDF-1.7\ninvalid fixture PDF\n"
        message.add_attachment(image, maintype="image", subtype="jpeg", filename="image004.jpg")
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename="도면.pdf")
        self.indexer.readpst = sys.executable
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = process.returncode = 0

        def export_numbered(*args, cwd, **kwargs):
            (Path(cwd) / "1").write_bytes(message.as_bytes())
            return process

        def download(attachment, token=True):
            connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=5)
            try:
                headers = {"X-Mail-Session": self.app.token} if token else {}
                connection.request("GET", f"/api/attachment/{attachment}", headers=headers)
                response = connection.getresponse()
                return response.status, response.getheader("Content-Disposition"), response.read()
            finally:
                connection.close()

        with (patch("mailsearch.indexer.subprocess.Popen", side_effect=export_numbered) as launcher,
              patch("mailsearch.indexer.extract_attachment", side_effect=[
                    ("미지원 형식", []), ("추출 실패 또는 암호화", [])])):
            source = self.indexer.register(pst)
            row = await_source(self.store, source, {"ready", "error"})
            self.assertEqual((row["state"], row["omitted"]), ("ready", 2))
            result = self.call("/api/search", {"query": "binary", "mode": "keyword"})[1]["results"][0]
            info = self.call(f"/api/evidence/{result['passage']}")[1]
            picture, document = info["attachments"]
            self.assertIsNone(picture["passage"])
            self.assertIsNone(document["passage"])
            self.assertEqual(download(picture["id"], token=False)[0], 403)
            self.assertEqual(download(picture["id"]), (200, "attachment; filename*=UTF-8''image004.jpg", image))
            self.assertEqual(download(document["id"]), (200, "attachment; filename*=UTF-8''%EB%8F%84%EB%A9%B4.pdf", pdf))
            self.assertEqual(launcher.call_count, 1)

            # Earlier versions removed the local EML after indexing.
            cache = self.indexer.original_cache(source, "", row["signature"])
            shutil.rmtree(cache)
            self.assertEqual(download(document["id"])[0], 202)
            deadline = time.monotonic() + 5
            while self.indexer.has_work() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(download(document["id"])[2], pdf)
            self.assertEqual(launcher.call_count, 2)
            pst.unlink()
            self.assertEqual(download(picture["id"])[0], 404)

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
        with patch("mailsearch.indexer.subprocess.Popen", side_effect=AssertionError("unchanged PST was exported")):
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            unchanged = await_source(self.store, source, {"empty", "ready", "error"})
        self.assertEqual((unchanged["state"], unchanged["done"]), ("ready", 1))
        pst.write_bytes(pst.read_bytes() + b"changed")
        with patch("mailsearch.indexer.subprocess.Popen", return_value=process):
            self.assertEqual(self.call("/api/source-action", {"source": source, "action": "refresh"})[0], 202)
            latest = await_source(self.store, source, {"empty", "ready", "error"})
        self.assertEqual((latest["state"], latest["active"]), ("empty", unchanged["active"]))
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


class SourcePaths(unittest.TestCase):
    def test_unc_share_is_recognized_even_when_drive_type_is_unknown(self):
        for root in (r"\\server\team\archive", r"\\?\UNC\server\team\archive"):
            path = PureWindowsPath(root)
            self.assertTrue(is_unc_share(path.drive))
            fake = MagicMock()
            fake.expanduser.return_value.resolve.return_value = path
            with (patch("mailsearch.filesystem.Path", return_value=fake),
                  patch("mailsearch.filesystem.windows_drive_type", return_value=1)):
                self.assertEqual(source_path(root), path)
                with self.assertRaises(OSError):
                    local_path(root)
        self.assertFalse(is_unc_share(r"\\.\PhysicalDrive0"))
        self.assertFalse(is_unc_share("Z:"))


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


class RAGStability(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            **GATEWAY_ENV, "MAILSEARCH_QUERY_REWRITE": "1", "MAILSEARCH_QUERY_REWRITE_MODEL": "",
            "MAILSEARCH_GATEWAY_REWRITE_MODEL": "",
        }))

    def gateway(self, *, answer="", embedding="", rerank=""):
        return IntranetAI("test-key", {"answer": answer, "embedding": embedding, "rerank": rerank})

    def test_duplicate_attachments_leave_rerank_and_answer_slots_for_other_mail(self):
        ai = MagicMock(spec=LocalAI)
        ai.embedding_index = ""
        ai.rerank_enabled = True
        ai.rerank_candidates.side_effect = lambda query, rows, **options: rows
        store = MagicMock(spec=Store)

        def row(number, name, subject, text):
            return {"id": number, "passage": number, "fingerprint": str(number), "attachment": name,
                    "subject": subject, "text": text, "location": "본문", "kind": "mail"}

        other = row(100, None, "A17 결정", "양산 범위를 별도로 확인했습니다.")
        for count in (10, 90):
            with self.subTest(copies=count):
                copies = [row(i, "C:/shared/Atlas.PDF" if i else "atlas.pdf", f"mail {i}", "Atlas  approved")
                          for i in range(count)]
                store.lexical.return_value = [*copies, other]
                store.related.return_value = [row(101, "Atlas.pdf", "Re: Atlas", " Atlas approved ")]
                self.assertEqual(dedupe_by_content([*copies, other]), [copies[0], other])
                evidence, _ = evidence_for(store, ai, "Atlas", {}, "")
                self.assertEqual(ai.rerank_candidates.call_args.args[1], [copies[0], other])
                self.assertEqual(evidence, [copies[0], other])
        unrelated = row(102, None, "다른 프로젝트", other["text"])
        self.assertEqual(dedupe_by_content([other, unrelated]), [other, unrelated])

    def test_korean_reply_boundaries_remove_only_the_quoted_tail(self):
        current = "현재 회신입니다.\r\n"
        for divider in ("----- 원본 메일 -----", "----- 전달된 메일 -----", "----- Forwarded Message -----",
                        "발신: old@example.invalid\r\n받는 사람: team\r\n제목: 이전 안\r\n"
                        "회사: 테스트\r\n부서: 테스트\r\n일시: 2025-12-01",
                        "발신자: 테스트\r\n발송일시: 2025-12-01",
                        "발신: 테스트\r\n보낸 시각: 2025-12-01"):
            with self.subTest(divider=divider):
                body = current + divider + "\r\n이전 초안만 담긴 인용문"
                self.assertEqual(list(mail_sections(body)), [(0, current)])
        body = current + "발신: 담당자의 안내입니다.\r\n일시는 추후 정합니다.\r\n계속 확인합니다."
        self.assertEqual(list(mail_sections(body)), [(0, body)])

    def test_latest_decision_is_retained_without_boosting_historical_questions(self):
        today = date.today().isoformat()
        draft = {"passage": 1, "source": 1, "subject": "A17 브래킷", "location": "본문",
                 "sent": "2024-03-01", "text": "초안 검토 중입니다."}
        approved = {**draft, "passage": 2, "sent": today, "text": "A17 양산 적용을 승인합니다."}
        unrelated = {**approved, "passage": 3, "subject": "B23 브래킷"}
        question = "A17 최종 승인일"
        self.assertTrue(needs_recency_boost(question, {}))
        for past in ("A17 2024년 초기안 승인", "A17 승인 타임라인", "A17 지난해 최종 승인"):
            self.assertFalse(needs_recency_boost(past, {}))
        self.assertFalse(needs_recency_boost(question, {"before": "2024-06-01"}))
        self.assertEqual(reciprocal_rank((([draft, approved], 1),), 2, recent=True), [approved, draft])
        self.assertEqual(latest_thread_decisions(question, [draft], [draft, unrelated, approved], 1), [approved])

        # Reranking can favor the older copy even within the same day.
        older = {**approved, "passage": 10, "id": 10, "attachment": None, "fingerprint": "10",
                 "sent": f"{today}T06:00:00+00:00", "subject": "브래킷 사양"}
        newest = {**older, "passage": 11, "id": 11, "fingerprint": "11", "sent": f"{today}T17:00:00+00:00"}
        other = {**older, "passage": 12, "id": 12, "fingerprint": "12", "text": "A17 예외를 검토 중입니다."}
        ai = MagicMock(spec=LocalAI)
        ai.embedding_index = ""
        ai.rerank_enabled = True
        ai.rerank_candidates.side_effect = lambda query, rows, **options: rows
        store = MagicMock(spec=Store)
        store.lexical.return_value = [older, newest, other]
        store.related.return_value = []
        rows, _ = evidence_for(store, ai, question, {}, "")
        self.assertTrue(ai.rerank_candidates.called)
        self.assertEqual([row["passage"] for row in rows], [11, 12])
        rows, _ = evidence_for(store, ai, "A17 승인 타임라인", {}, "")
        self.assertEqual([row["passage"] for row in rows], [10, 12])

    def test_contiguous_child_passages_support_cross_boundary_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "index")
            path = root / "Atlas.txt"
            quote = "검수기록" + "가나다라마바사" * 30 + " 시제품에만 적용합니다."
            original = "A" * 920 + quote + "Z" * 1600
            path.write_text(original, encoding="utf-8")
            chunks = list(sections(original))
            self.assertEqual(len(chunks[0][1]), 1100)
            self.assertEqual(chunks[1][0], 980)
            self.assertEqual(merge_contiguous_passages(chunks), (0, original))
            self.assertIsNone(merge_contiguous_passages([(0, "abcdefgh"), (6, "xxghi")]))
            self.assertFalse(any(quote in text for _, text in chunks))
            with store.connection() as conn:
                source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'doc_file',1)",
                                      (str(path),)).lastrowid
                doc = {"name": path.name, "folder": "", "sent": "", "status": "완료", "sections": [("내용", original)]}
                mail = store.add_document(conn, source, 1, path.name, doc)
                anchor = conn.execute("SELECT id FROM passages WHERE mail=? ORDER BY start LIMIT 1 OFFSET 1", (mail,)).fetchone()[0]
            hits = store.lexical("검수기록", {"source": source}, fuzzy=True)
            self.assertTrue(hits)
            self.assertTrue(all(len(hit["text"]) <= 1100 for hit in hits))
            raw, = store.passages([anchor], {"source": source})
            window = store.expand_passage_window(raw)
            self.assertTrue(window["window"])
            self.assertLess(window["start"], raw["start"])
            self.assertEqual(len(window["text"]), 1800)
            self.assertEqual(window["text"], original[window["start"]:window["start"] + len(window["text"])])
            self.assertEqual(store.open_passage(anchor, window=True)["text"], window["text"])
            result = {"status": "answered", "claims": [{"text": quote, "citations": [{"source": anchor, "quote": quote}]}]}
            checked = validate(result, [window])
            self.assertEqual(checked["claims"][0]["citations"][0]["quote"], quote)
            excerpt, = pack_excerpts("검수기록", [window], 12000)
            self.assertFalse(excerpt.original_in(raw))
            self.assertTrue(excerpt.original_in(store.passages([anchor], windows={anchor})[0]))
            with patch.object(store, "lexical", return_value=[raw]):
                evidence, _ = evidence_for(store, None, "검수기록", {"source": source}, "", semantic=False, expand=True)
            self.assertEqual(evidence, [window])

    def test_legacy_sections_v2_and_metadata_v1_still_search_and_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "index")
            mail = {"subject": "Legacy Atlas", "sender": "", "recipient": "", "sent": "", "folder": "/",
                    "attachments": [], "message_id": "", "reply_ids": "", "body": "Legacy approval evidence"}
            with store.connection() as conn:
                source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)", (str(root),)).lastrowid
                item = store.add_mail(conn, source, 1, "old.eml", mail)
                conn.execute("UPDATE mails SET fingerprint=? WHERE id=?",
                             (fingerprint(mail, "old.eml", index_format="sections-v2"), item))
                passage = conn.execute("SELECT id FROM passages WHERE mail=?", (item,)).fetchone()[0]
                conn.execute("INSERT INTO vectors VALUES(?,?,?)", (passage, "metadata-v1:embed", pack_vector([1., 0.])))
                conn.execute("INSERT INTO embedding_revisions VALUES(?,?,?)", (source, 1, "metadata-v1:embed"))
            reopened = Store(root / "index")
            self.assertEqual(reopened.lexical("Atlas", {"source": source})[0]["passage"], passage)
            self.assertEqual(reopened.semantic_contextual([1., 0.], "embed", {})[0]["passage"], passage)
            self.assertEqual(reopened.open_passage(passage)["text"], mail["body"])
            self.assertEqual(reopened.outdated_pst_labels(source, 1, ("",)), set())

    def test_supplemental_search_recovers_bom_in_same_scope_and_checks_access(self):
        question = "A17 시제품 승인과 양산 BOM 반영 여부 비교"
        prototype = "A17 시제품 4 mm를 승인합니다."
        production = "A17 양산 BOM 두께는 3 mm입니다."
        gap = parse_evidence_gap({"sufficient": False, "missing_aspects": ["양산 BOM 반영 여부"],
                                  "follow_up_queries": ["양산 BOM Rev.C 두께"]}, question)
        self.assertEqual(gap.queries, ("A17 양산 BOM Rev.C 두께",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "index")
            approved = root / "approved"
            excluded = root / "excluded"
            approved.mkdir()
            excluded.mkdir()
            (approved / "bom.txt").write_text(production, encoding="utf-8")
            subject = "A17 시제품 승인"
            mail = {"subject": subject, "sender": "", "recipient": "", "sent": "2026-05-03", "folder": "/",
                    "attachments": [], "message_id": "", "reply_ids": "", "body": prototype}
            with store.connection() as conn:
                source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)",
                                      (str(approved),)).lastrowid
                outside = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)",
                                       (str(excluded),)).lastrowid
                initial = store.add_mail(conn, source, 1, "initial.eml", mail)
                for item, summary, target, sent in (
                    ("bom.txt", production, source, "2026-05-11"),
                    ("unreadable.txt", "A17 양산 BOM 비인가 초안", source, "2026-05-11"),
                    ("old.eml", "A17 양산 BOM 2024 외부 기간", source, "2024-05-11"),
                    ("unauthorized.eml", "A17 양산 BOM 타 보관함", outside, "2026-05-11"),
                ):
                    if item.endswith(".txt"):
                        doc = {"name": item, "folder": "", "sent": sent, "status": "완료", "sections": [("내용", summary)]}
                        store.add_document(conn, target, 1, item, doc)
                    else:
                        store.add_mail(conn, target, 1, item, {**mail, "subject": "A17 양산 BOM", "sent": sent, "body": summary})
                passage = conn.execute("SELECT id FROM passages WHERE mail=?", (initial,)).fetchone()[0]

            scope = {"source": source, "after": "2026-05-01", "before": "2026-06-01"}
            raw = store.passages([passage], scope)
            ai = LocalAI("")
            ai.rewrite_model = "fixture"
            profile = SearchProfile("answer")
            with (store.search_session(profile), patch.dict(os.environ, {"MAILSEARCH_AGENTIC_RETRY": "1"}),
                  patch.object(ai, "diagnose_evidence_gap", return_value=gap) as diagnose):
                rows, mode, missing, ran = supplemental_evidence(store, ai, question, scope, "", raw, "keyword", 2)
                self.assertTrue(ran)
                diagnose.assert_called_once()
                self.assertEqual(diagnose.call_args.args[1], raw)
                self.assertEqual({row["text"] for row in rows}, {prototype, production})
                self.assertEqual([row["source"] for row in rows], [source, source])
                self.assertTrue(all(scope["after"] <= row["sent"] < scope["before"] for row in rows))
                self.assertEqual((mode, missing), ("keyword", ("양산 BOM 반영 여부",)))
                self.assertGreater(profile.ms["followup"], 0)
                self.assertGreater(profile.ms["fts"], 0)
                profile.log(logging.getLogger("mailsearch.runtime"), "supplemental_fixture")
                with patch.dict(os.environ, {"MAILSEARCH_AGENTIC_RETRY": "0"}):
                    self.assertEqual(supplemental_evidence(store, ai, question, scope, "", raw, "keyword", 2),
                                     (raw, "keyword", (), False))
                profile.started -= 39
                self.assertEqual(supplemental_evidence(store, ai, question, scope, "", raw, "keyword", 2),
                                 (raw, "keyword", (), False))
                diagnose.assert_called_once()
            missing_only = report_unconfirmed_aspects({"status": "answered", "claims": [
                {"text": prototype, "citations": [{"quote": prototype}]}]}, missing)
            self.assertEqual((missing_only["status"], missing_only["unknowns"]), ("partial", list(missing)))
            verified = {"status": "answered", "claims": [{"text": production, "citations": [{"quote": production}]}]}
            self.assertEqual(report_unconfirmed_aspects(verified, missing), verified)
            self.assertEqual(report_unconfirmed_aspects(verified, ("양산 BOM Rev.C",))["status"], "partial")

    def test_supplemental_pass_reserves_slots_for_unique_content(self):
        question = "A17 시제품 승인과 양산 BOM 비교"
        first = [{"passage": n, "text": f"primary result {n}", "attachment": "BOM.pdf"} for n in range(9)]
        duplicate = {**first[0], "passage": 90, "attachment": r"C:\files\bom.pdf"}
        new = [{"passage": n, "text": f"new fact {n}", "attachment": "BOM.pdf"} for n in range(100, 104)]
        registered = {row["passage"]: row for row in [*first, duplicate, *new]}
        store = MagicMock(spec=Store)
        store.search_profile = None
        store.passages.side_effect = lambda ids, scope, **options: [registered[pid] for pid in ids]
        ai = LocalAI("")
        ai.rewrite_model = "fixture"
        scope = {"source": 3, "before": "2026-06-01"}
        gap = parse_evidence_gap({"sufficient": False, "follow_up_queries": ["양산 BOM"]}, question)
        with (patch.dict(os.environ, {"MAILSEARCH_AGENTIC_RETRY": "1"}),
              patch.object(ai, "diagnose_evidence_gap", return_value=gap) as diagnose,
              patch("mailsearch.answer.evidence_for", return_value=([duplicate, *new], "keyword")) as search):
            evidence, _, _, ran = supplemental_evidence(store, ai, question, scope, "", first, "keyword", 2, ceiling=9)
            self.assertTrue(ran)
            self.assertEqual(evidence, [*first[:4], *new, first[4]])
            search.assert_called_once()
            self.assertEqual(search.call_args.args[3], scope)
            self.assertEqual(search.call_args.kwargs["sub_queries"], ("A17 양산 BOM",))
            preview, _, _, _ = supplemental_evidence(
                store, ai, question, scope, "", first, "keyword", 2, ceiling=9, review_slots=5,
            )
            self.assertEqual(preview[:5], [*first[:3], *new[:2]])
            search.assert_called_with(store, ai, question, scope, "", sub_queries=("A17 양산 BOM",),
                                      limit=9, semantic=True, expand=True, max_evidence=12,
                                      check=unittest.mock.ANY, allow_rerank=False, embedding_timeout=unittest.mock.ANY)
            with patch.dict(os.environ, {"MAILSEARCH_QUERY_REWRITE": "0"}):
                self.assertEqual(supplemental_evidence(store, ai, question, scope, "", first, "keyword", 2),
                                 (first, "keyword", (), False))
                self.assertIsNone(ai.rewrite_query(question))
            self.assertEqual(diagnose.call_count, 2)
            self.assertEqual(search.call_count, 2)

    def test_gateway_gap_diagnosis_transmits_coverage_without_mail_content(self):
        gateway = self.gateway(answer="answer-model-a")
        ai = LocalAI("", embedding_api=gateway)
        question = "A17 시제품 승인과 양산 BOM 비교"
        evidence = [{"text": "Private body 시제품 승인합니다.", "sender": "Private sender", "subject": "Private subject"}]
        raw = {"sufficient": False, "missing_aspects": ["양산 BOM"], "follow_up_queries": ["양산 BOM"]}
        reply = {"choices": [{"message": {"content": json.dumps(raw)}}]}
        with patch.object(gateway, "request", return_value=reply) as request:
            result = ai.diagnose_evidence_gap(question, evidence)
        self.assertEqual(result.queries, ("A17 양산 BOM",))
        path, payload, timeout = request.call_args.args
        self.assertEqual((path, payload["max_tokens"], payload["temperature"]), (CHAT_PATH, 320, 0))
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 2.5)
        data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(set(data), {"question", "coverage"})
        self.assertEqual((data["coverage"]["prototype"], data["coverage"]["production"]), (1, 0))
        self.assertNotIn("Private", json.dumps(payload))
        self.assertIsNone(parse_evidence_gap({**raw, "sufficient": True}, question))

    def test_rerank_timeout_and_redacted_fallback_for_80_candidates(self):
        client = self.gateway(rerank="bge-reranker-v2-m3")
        ai = LocalAI("", embedding_api=client)
        rows = [{"subject": "Private email subject", "text": "Private email body"} for _ in range(80)]
        with (patch.object(client, "request", side_effect=TimeoutError("Private failure details")) as request,
              self.assertLogs("mailsearch.runtime", "WARNING") as logs):
            self.assertIs(ai.rerank_candidates("Private query", rows, limit=80), rows)
        path, payload, timeout = request.call_args.args
        self.assertEqual((path, payload["top_n"], len(payload["documents"]), timeout), (RERANK_PATH, 80, 80, 15))
        self.assertEqual([record.getMessage() for record in logs.records], ["Rerank skipped (docs=80, error=TimeoutError)"])

    def test_gateway_only_rewrite_uses_small_budget_and_falls_back_on_timeout(self):
        gateway = self.gateway(answer="answer-model-a")
        ai = LocalAI("", embedding_api=gateway)
        self.assertIsNotNone(gateway.answer_client)
        self.assertFalse(ai.embedding_index)
        question = "Atlas A17 승인과 BOM 비교"
        raw = {"sub_queries": ["Atlas 승인", "Atlas BOM"], "keywords": ["A17"], "filters": {}}
        reply = {"choices": [{"message": {"content": "```json\n" + json.dumps(raw) + "\n```"}}]}
        with (patch.object(ai, "ensure_model", side_effect=AssertionError("No local model")),
              patch.object(gateway, "request", return_value=reply) as request):
            plan = ai.rewrite_query(question)
            self.assertEqual(plan.sub_queries, ("A17 Atlas 승인", "A17 Atlas BOM"))
            path, payload, timeout = request.call_args.args
            self.assertEqual((path, payload["model"], payload["max_tokens"]), (CHAT_PATH, "answer-model-a", 384))
            self.assertTrue(0 < timeout <= QUERY_REWRITE_TIMEOUT)
            self.assertEqual(set(json.loads(payload["messages"][1]["content"])), {"question", "today"})
            request.reset_mock()
            with patch.dict(os.environ, {"MAILSEARCH_QUERY_REWRITE": "0"}):
                self.assertIsNone(ai.rewrite_query(question))
            request.assert_not_called()
            request.side_effect = TimeoutError
            self.assertIsNone(rewritten_evidence(MagicMock(spec=Store), ai, question, {}, ""))
            request.assert_called_once()
        with patch.dict(os.environ, {"MAILSEARCH_GATEWAY_REWRITE_MODEL": "lightweight-rewrite"}):
            override = self.gateway(embedding="bge-m3")
        with patch.object(override, "request", return_value=reply) as request:
            self.assertEqual(LocalAI("", embedding_api=override).rewrite_query(question), plan)
            self.assertEqual(request.call_args.args[1]["model"], "lightweight-rewrite")

    def test_three_subqueries_use_one_batch_and_reuse_the_cached_vectors(self):
        client = self.gateway(embedding="bge-m3")
        ai = LocalAI("", embedding_api=client)
        store = MagicMock(spec=Store)
        hit = dict(id=1, passage=1, text="Atlas evidence", fingerprint="1", attachment=None, location="본문")
        store.lexical.return_value = []
        store.semantic_contextual.return_value = [hit]
        store.related.return_value = []
        queries = ("Atlas 승인", "Atlas BOM", "Atlas 누락")

        with patch.object(client, "embed", return_value=[[1, 0], [0, 1], [1, 1]]) as embed:
            for _ in range(2):
                self.assertEqual(evidence_for(store, ai, "Atlas 종합", {}, "", sub_queries=queries), ([hit], "hybrid"))
            embed.assert_called_once_with(list(queries), QUERY_EMBEDDING_TIMEOUT)
            self.assertEqual([call.args[0] for call in store.semantic_contextual.call_args_list[:3]],
                             [[1, 0], [0, 1], [1, 1]])
        with patch.object(client, "embed", side_effect=TimeoutError) as embed:
            revised = (queries[0], "Atlas 일정", "Atlas 발신자")
            store.lexical.return_value = [hit]
            self.assertEqual(evidence_for(store, ai, "Atlas 종합", {}, "", sub_queries=revised), ([hit], "keyword_only"))
            embed.assert_called_once_with(list(revised[1:]), QUERY_EMBEDDING_TIMEOUT)

    def test_related_passages_prioritize_keyword_count_and_use_decisions_for_ties(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            store = Store(folder / "index")
            mail = {"subject": "Atlas project", "sender": "", "recipient": "", "sent": "", "folder": "/",
                    "attachments": [], "message_id": "<seed@example.invalid>", "reply_ids": "", "body": "질문입니다."}
            with store.connection() as conn:
                source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)", (str(folder),)).lastrowid
                seed = store.add_mail(conn, source, 1, "seed.eml", mail)
                reply = {**mail, "message_id": "<reply@example.invalid>", "reply_ids": mail["message_id"],
                         "body": "검토 부탁드립니다."}
                reply_id = store.add_mail(conn, source, 1, "reply.eml", reply)
                store.add_passage(conn, reply_id, reply, None, "", "본문", 20, "Atlas bracket torque 값은 12 N·m입니다.")
                store.add_passage(conn, reply_id, reply, None, "", "본문", 80, "Atlas bracket 승인 회신입니다.")
            rows = store.related([seed], {}, question="Atlas bracket torque")
            self.assertEqual([row["start"] for row in rows], [20, 80])
            rows = store.related([seed], {}, question="Atlas bracket")
            self.assertEqual([row["start"] for row in rows], [80, 20])


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
    def test_groups_follow_explicit_replies_even_when_subject_changes(self):
        store = MagicMock()
        store.reply_links.return_value = [(1, 10, "<one>", None), (2, 10, "<two>", "<one>"), (3, 10, "<three>", None)]
        evidence = [{"id": index, "passage": index, "kind": "mail", "source": 10, "subject": subject,
                     "sent": f"2026-01-0{index}"} for index, subject in ((3, "Zeta"), (2, "Changed"), (1, "Atlas"))]
        groups = answer_groups(store, evidence)
        self.assertEqual([[row["id"] for row in group] for group in groups], [[1, 2], [3]])

    def test_simple_questions_keep_one_query_and_timelines_use_three(self):
        self.assertEqual(answer_plan("A-17 승인일은 언제였어?"), ["A-17 승인일은 언제였어?"])
        self.assertEqual(len(answer_plan("Atlas 변경 타임라인")), 3)
        self.assertEqual(len(answer_plan("한빛과 대성의 단가 비교")), 2)
        self.assertEqual(len(answer_plan("한빛과 대성의 단가 차이")), 2)
        self.assertEqual(len(answer_plan("Atlas 견적 종합해줘")), 2)
        self.assertEqual(answer_plan("Atlas 견적 단가, 납기, 결제 조건을 알려줘"),
                         ["Atlas 견적 단가 알려줘", "Atlas 견적 납기 알려줘", "Atlas 견적 결제 조건 알려줘"])

    def test_duplicate_passage_keeps_first_anchor_and_does_not_displace_other_hits(self):
        anchor = {"passage": 1, "fingerprint": "a", "location": "본문", "attachment": "", "text": "Anchor"}
        match = {**anchor, "text": "Repeated", "fingerprint": "b"}
        second = {**match, "passage": 2}
        self.assertEqual(combine_answer_evidence([[match, second], [second]], [anchor]), [anchor, second])

    def test_sessions_expire_and_keep_only_three_turns(self):
        sessions = AnswerSessions()
        with patch("mailsearch.answer.time.monotonic", return_value=100):
            key, handle, _ = sessions.begin("")
            result = {"status": "answered", "claims": [{"text": "결과", "citations": [{"passage": 123}]}]}
            for question in ("첫 질문", "둘째 질문", "셋째 질문", "넷째 질문"):
                sessions.record(key, handle, question, result)
            _, _, turns = sessions.begin(key)
            self.assertEqual([turn[0] for turn in turns], ["둘째 질문", "셋째 질문", "넷째 질문"])
        with patch("mailsearch.answer.time.monotonic", return_value=1901):
            new, _, turns = sessions.begin(key)
        self.assertNotEqual(new, key)
        self.assertFalse(turns)

    def test_reset_prevents_in_flight_answer_from_restoring_session(self):
        sessions = AnswerSessions()
        key, handle, _ = sessions.begin("")
        sessions.reset(key)
        sessions.record(key, handle, "이전 질문", {"status": "insufficient", "claims": []})
        new, _, turns = sessions.begin(key)
        self.assertNotEqual(new, key)
        self.assertEqual(turns, ())

    def test_context_grows_for_large_evidence_without_exceeding_model_limit(self):
        ai = LocalAI("test")
        ai.model_context_limit = 12288
        rows = [{"id": index, "passage": index, "sent": "", "sender": "", "subject": "Atlas",
                 "text": "배관 연결부를 점검했습니다. " * 46} for index in range(1, 19)]
        with (patch.object(ai, "ensure_server"), patch.object(ai, "ensure_model"),
              patch.object(ai, "call", return_value={"response": '{"status":"answered","claims":[]}'}) as generate):
            self.assertEqual(ai.answer("Atlas 종합", rows, "")["status"], "partial")
        self.assertEqual(generate.call_args.args[1]["options"]["num_ctx"], 12288)

    def test_map_keeps_cited_passage_that_would_be_truncated_from_reduce(self):
        ai = LocalAI("test")
        quote = "검수 조건을 승인했습니다."
        metadata = {"source": 1, "path": "Atlas", "folder": "", "sent": "", "sender": "",
                    "recipient": "", "updated": "", "attachment": "", "location": "본문"}
        rows = [{**metadata, "id": n, "passage": n, "subject": f"Atlas {n}",
                 "text": "X" * 2400 + (f"\n{quote}" if n == 8 else "")} for n in range(1, 9)]
        proposed = {"status": "answered", "claims": [
            {"text": quote, "citations": [{"source": 8, "quote": quote}]},
        ]}
        reductions = []

        def generate(_path, payload, timeout=None):
            incoming = json.loads(payload["prompt"])
            if payload["options"]["num_predict"] > 640:
                reductions.append(incoming)
                return {"response": '{"status":"answered","claims":[]}'}
            result = proposed if any(item["source"] == 8 for item in incoming["evidence"]) else {
                "status": "insufficient", "claims": [],
            }
            return {"response": json.dumps(result)}

        with (patch.object(ai, "ensure_server"), patch.object(ai, "ensure_model"),
              patch.object(ai, "call", side_effect=generate)):
            result = ai.answer("Atlas 검수 종합", rows, "", groups=[rows[:6], rows[6:]])
        self.assertEqual(result["status"], "partial")
        reduce = reductions[0]
        note, = reduce["verified_notes"]
        self.assertEqual(note["text"], quote)
        row = next(item for item in reduce["evidence"] if item["source"] == note["sources"][0])
        self.assertIn(quote, row["text"])
        self.assertEqual(row["group"], 2)

    def test_natural_scope_only_infers_clear_calendar_windows(self):
        today = date(2026, 1, 1)
        windows = {"최근": (date(2025, 12, 3), date(2026, 1, 2)),
                   "이번 주": (date(2025, 12, 29), date(2026, 1, 5)),
                   "지난달": (date(2025, 12, 1), date(2026, 1, 1)),
                   "올해": (date(2026, 1, 1), date(2027, 1, 1))}
        for phrase, window in windows.items():
            search, filters = question_scope(f"{phrase} Atlas 회신", {}, today=today)
            self.assertEqual(search, "Atlas 회신")
            self.assertEqual(filters, dict(zip(("after", "before"), map(local_midnight_utc, window))))
        self.assertEqual(question_scope('지난달 "Atlas" 회신', {}, today=today)[0], '"Atlas" 회신')
        for question in ('"올해" 회신', "올해 Atlas 납기", "이번 주와 지난달 회신 비교",
                         "최근 3개월 메일", "지난달 대비 회신", "첨부가 있는 메일이 아닌 자료"):
            self.assertEqual(question_scope(question, {}, today=today), (question, {}))
        filters = {"before": "explicit", "sender": "chosen", "attachment": "no"}
        question = '지난달 발신자: "홍길동" 첨부파일 내용 비교'
        self.assertEqual(question_scope(question, filters, today=today), (question, filters))
        self.assertEqual(question_scope("첨부파일 내용 정리", {})[1], {"prefer_attachment": True})

    def test_context_expansion_keeps_twelve_ranked_passages_and_caps_at_eighteen(self):
        def row(passage):
            return {"passage": passage, "id": passage, "fingerprint": str(passage), "kind": "document",
                    "location": "본문", "attachment": None, "text": f"Atlas passage {passage}"}
        store = MagicMock()
        store.lexical.return_value = [row(i) for i in range(12)]
        store.related.return_value = []
        store.evidence_context.side_effect = lambda match, _: [row(100 + match["passage"])]
        evidence, _ = evidence_for(store, None, "Atlas", {}, "", semantic=False, limit=12, expand=True,
                                   max_evidence=18)
        self.assertEqual(len(evidence), 18)
        self.assertEqual([match["passage"] for match in evidence[:12]], list(range(12)))

    def test_small_context_keeps_complete_excerpts_and_marks_partial(self):
        ai = LocalAI("test", answer_num_ctx=4096)
        text = "배관 상태를 점검했습니다. " * 40
        rows = [{"sent": "", "sender": "", "subject": "Atlas", "text": text, "id": index, "passage": index}
                for index in range(1, 19)]
        with (patch.object(ai, "ensure_server"),
              patch.object(ai, "ensure_model", return_value="test:latest"),
              patch.object(ai, "call", return_value={"response": '{"status":"answered","claims":[]}'}) as generate):
            self.assertEqual(ai.answer("Atlas 종합", rows, "")["status"], "partial")
        payload = generate.call_args.args[1]
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        excerpts = json.loads(payload["prompt"])["evidence"]
        self.assertTrue(0 < len(excerpts) < len(rows))
        self.assertTrue(all(excerpt["text"].endswith(text) for excerpt in excerpts))

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
