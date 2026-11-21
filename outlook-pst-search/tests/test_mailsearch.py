import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mailsearch.answer import LocalAI, delivery_date_answer, validate
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

        def staged(*args):
            index_headers(*args)
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

    @unittest.skipUnless(os.getenv("MAILSEARCH_TEST_PST") and os.getenv("MAILSEARCH_TEST_READPST"), "optional public PST fixture")
    def test_real_pst_adapter_and_original_hash(self):
        path = Path(os.environ["MAILSEARCH_TEST_PST"])
        self.indexer.readpst = os.environ["MAILSEARCH_TEST_READPST"]
        original_digest = hashlib.sha256(path.read_bytes()).digest()
        source = self.indexer.register(path)
        indexed = await_source(self.store, source, {"ready", "error"})
        self.assertEqual(indexed["state"], "ready", indexed["issue"])
        self.assertGreater(indexed["done"], 0)
        self.assertEqual(indexed["failed"], 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), original_digest)
        self.assertTrue(self.call("/api/search", {"query": "", "filters": {"source": source}})[1]["results"])


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
