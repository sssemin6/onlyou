import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import MagicMock, patch
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mailsearch.answer import LocalAI, delivery_date_answer, validate
from mailsearch.external import ExternalAI, Excerpt, Reviews
from mailsearch.extract import extract_attachment
from mailsearch.indexer import Indexer
from mailsearch.server import App
from mailsearch.store import Store
from tools.demo import generate


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
        self.send_message({"models": [{"name": "test:latest"}, {"name": "embed:latest"}]})

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
        status, found = self.call("/api/search", {"query": "폴더 안 PST에서", "filters": {}})
        self.assertEqual(status, 200)
        self.assertTrue(found["results"])
        self.assertTrue(found["results"][0]["folder"].startswith("/보관.pst/"))
        status, attachment = self.call("/api/search", {"query": "320000원", "filters": {}})
        self.assertEqual(status, 200)
        self.assertTrue(attachment["results"])

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
        status, data = self.call("/api/search", {"query": "A-17", "filters": {"source": source, "sender": "buy@example.invalid"}})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["results"]), 3)
        status, data = self.call("/api/search", {"query": "냉각판", "filters": {"recipient": "hanbit@"}})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["attachment"], "B-19_견적.txt")
        _, evidence = self.call(f"/api/evidence/{data['results'][0]['passage']}")
        self.assertIn("320000원", evidence["text"])
        self.assertEqual(evidence["attachments"][1]["status"], "미지원 형식")
        self.assertEqual(self.call("/api/search", {"query": "납품", "filters": {"after": "2027-01-01"}})[1]["results"], [])
        self.assertEqual(self.call("/api/search", {"query": 123})[0], 400)

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
        self.assertEqual(len(self.call("/api/search", {"query": "A-17", "filters": {"source": source}})[1]["results"]), 1)

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
        self.assertTrue(self.call("/api/search", {"query": "", "filters": {"source": source}})[1]["results"])


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


if __name__ == "__main__":
    unittest.main()
