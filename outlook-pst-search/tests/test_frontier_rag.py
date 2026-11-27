import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from mailsearch.answer import EVIDENCE_SUFFICIENT, EvidenceGap, LocalAI, supplemental_evidence
from mailsearch.eval_harness import TIMINGS, evaluate
from mailsearch.external import ExternalAI
from mailsearch.server import App
from mailsearch.store import MAIL_INDEX_FORMAT, Store


class IndexFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / "data")
        self.addCleanup(self.store.close)

    def archive(self, name, *, available=True):
        path = self.root / name
        if available:
            path.mkdir()
        with self.store.connection() as conn:
            return conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)", (str(path),)).lastrowid

    def mail(self, source, key, subject="Atlas quotation", *, sent="2026-05-12", parent="", version=1,
             sender="Alice <alice@vendor.example>", recipient="Desk <desk@yard.example>",
             body="The Atlas quotation includes valves."):
        with self.store.connection() as conn:
            return self.store.add_mail(conn, source, version, f"{key}.eml", {
                "subject": subject, "sender": sender, "recipient": recipient, "sent": sent,
                "body": body, "message_id": f"<{key}@example.test>", "reply_ids": parent,
                "folder": "", "attachments": [],
            })


class ParticipantSummaryTests(IndexFixture):
    def test_existing_headers_and_threads_respect_every_scope(self):
        source = self.archive("archive")
        outside = self.archive("outside")
        inaccessible = self.archive("unavailable", available=False)
        first = self.mail(source, "base", recipient="Bob <bob@vendor.example>; ALICE@VENDOR.EXAMPLE; 김 부장")
        self.mail(source, "reply", "Revised schedule", sent="2026-05-20", parent="<base@example.test>",
                  recipient="Bob <bob@vendor.example>; Yard <desk@yard.example>")
        self.mail(source, "old", "Older reply", sent="2025-05-21", parent="<base@example.test>")
        self.mail(source, "inactive", "Atlas hidden", version=2)
        self.mail(outside, "excluded", "Atlas out of scope", sender="private@outside.example")
        self.mail(inaccessible, "unreadable", "Atlas unreadable", sender="private@unreadable.example")
        scope = {"source": source, "after": "2026-05-01", "before": "2026-06-01"}
        result = self.store.participant_summary(["Atlas"], scope)
        self.assertEqual(result, self.store.participant_summary(["Atlas"], scope, seed_ids=[first]))
        self.assertEqual(result["mail_count"], 2)
        people = {row["email"]: row for row in result["participants"]}
        self.assertEqual(people["alice@vendor.example"], {"name": "Alice", "email": "alice@vendor.example",
                         "domain": "vendor.example", "count": 2, "last_sent": "2026-05-20", "subject": "Revised schedule"})
        self.assertEqual(people["bob@vendor.example"]["count"], 2)
        self.assertEqual(people[""]["name"], "김 부장")
        self.assertEqual(result["domains"][0], {"domain": "vendor.example", "count": 2,
                         "subject": "Revised schedule", "last_sent": "2026-05-20"})
        self.assertEqual(self.store.participant_summary("Atlas", {**scope, "tab": "document"})["mail_count"], 0)
        self.assertEqual(self.store.participant_summary("Atlas", {"source": inaccessible})["mail_count"], 0)
        self.assertEqual(self.store.participant_summary("Atlas", {**scope, "recipient": "ALICE@vendor.example"})["mail_count"], 1)
        self.assertEqual(len(self.store.participant_summary("Atlas", scope, limit=1)["participants"]), 1)
        with self.store.connection() as conn:
            fingerprint = conn.execute("SELECT fingerprint FROM mails WHERE id=?", (first,)).fetchone()[0]
        self.assertTrue(fingerprint.startswith(f"{MAIL_INDEX_FORMAT}:"))


class AgentStepsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, MAILSEARCH_AGENTIC_RETRY="1", MAILSEARCH_QUERY_REWRITE="1",
                                     MAILSEARCH_AGENT_STEPS="2"))
        self.ai = LocalAI("")
        self.ai.rewrite_model = "fixture"
        self.store = MagicMock(spec=Store)
        self.store.search_profile = None
        self.question = "Atlas 첨부와 날짜별 변경 이력 비교"
        self.scope = {"source": 2, "after": "2026-05-01"}
        self.rows = {number: {"passage": number, "id": number + 100, "text": f"unique {number}",
                              "kind": "mail", "location": "본문"} for number in range(1, 10)}
        self.store.passages.side_effect = lambda ids, scope, **kwargs: [
            self.rows[n] for n in ids if scope == self.scope
        ]
        def related(*_args, **_options):
            time.sleep(.06)
            return [self.rows[2]]

        self.store.related.side_effect = related
        self.store.attachment_evidence.return_value = [self.rows[3]]
        self.store.evidence_attachment_names.return_value = {101: ["quote.xlsx"]}

    def run_agent(self, diagnose, search, stages):
        with (patch.object(self.ai, "diagnose_evidence_gap", side_effect=diagnose) as diagnostic,
              patch("mailsearch.answer.evidence_for", side_effect=search) as retrieve):
            result = supplemental_evidence(
                self.store, self.ai, self.question, self.scope, "", [self.rows[1]], "keyword", 2,
                expand_targets=True, searched_queries={"initial query"}, on_followup=stages.append,
            )
        return result, diagnostic, retrieve

    def test_blocks_duplicate_queries_mail_ids_and_attachment_names(self):
        stages = []
        first = EvidenceGap(("first",), ("Initial Query", "New Search"), (101,), ("quote",))
        second = EvidenceGap(("second",), (" new  search ", "Different"), (101,), ("QUOTE.XLSX",))
        (rows, mode, aspects, ran), diagnose, search = self.run_agent(
            [first, second, AssertionError("default limit")], [([self.rows[4]], "keyword"), ([self.rows[5]], "hybrid")],
            stages,
        )
        self.assertEqual(stages, [1, 2])
        self.assertEqual(diagnose.call_count, 2)
        self.assertEqual([call.kwargs["sub_queries"] for call in search.call_args_list],
                         [("New Search",), ("Different",)])
        self.assertEqual(diagnose.call_args_list[1].args[1][0]["passage"], 3)
        self.store.related.assert_called_once_with([101], self.scope, hops=3, question=self.question)
        self.store.attachment_evidence.assert_called_once()
        self.assertEqual({row["passage"] for row in rows}, {1, 2, 3, 4, 5})
        self.assertEqual((mode, aspects, ran), ("hybrid", ("second",), True))

    def test_sufficient_or_no_new_evidence_stops_before_step_three(self):
        gap = EvidenceGap(("missing",), ("additional",))
        with patch.dict(os.environ, MAILSEARCH_AGENT_STEPS="3"):
            (rows, _, aspects, _), diagnose, search = self.run_agent(
                [gap, EVIDENCE_SUFFICIENT, AssertionError("stop when sufficient")],
                [([self.rows[2]], "keyword")], [],
            )
            self.assertEqual((len(rows), aspects, diagnose.call_count, search.call_count), (2, (), 2, 1))
            (_, _, _, _), diagnose, search = self.run_agent(
                [gap, AssertionError("stop when there is no new evidence")], [([self.rows[1]], "keyword")], [],
            )
            self.assertEqual((diagnose.call_count, search.call_count), (1, 1))

    def test_step_override_is_capped_at_three(self):
        with patch.dict(os.environ, MAILSEARCH_AGENT_STEPS="20"):
            stages = []
            _, diagnose, search = self.run_agent(
                [EvidenceGap((), (f"query {n}",)) for n in range(1, 5)],
                [([self.rows[n]], "keyword") for n in range(2, 6)], stages,
            )
        self.assertEqual((diagnose.call_count, search.call_count, stages), (3, 3, [1, 2, 3]))


class ConversationApiTests(IndexFixture):
    @contextmanager
    def app(self, store, ai):
        with App(0, store, None, ai) as app:
            thread = threading.Thread(target=app.serve_forever)
            thread.start()
            try:
                yield app
            finally:
                app.shutdown()
                thread.join()

    @staticmethod
    def call(app, route, payload=None, *, method=None, authorized=True):
        client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["X-Mail-Session"] = app.token
        try:
            client.request(method or ("POST" if payload is not None else "GET"), route,
                           None if payload is None else json.dumps(payload), headers)
            response = client.getresponse()
            return response.status, json.load(response)
        finally:
            client.close()

    @staticmethod
    def generate(_question, rows, _context, **_options):
        row = rows[0]
        quote = row["text"].strip()
        return {"status": "answered", "claims": [{"text": quote, "citations": [
            {"source": row["passage"], "quote": quote},
        ]}]}

    def test_sessions_survive_server_restart_and_delete_checks_token(self):
        source = self.archive("archive")
        self.mail(source, "atlas")
        ai = LocalAI("fixture")
        payload = {"query": "Atlas quotation", "session_id": "", "filters": {"source": source}}
        with patch.object(ai, "answer", side_effect=self.generate) as generate:
            with self.app(self.store, ai) as app:
                status, first = self.call(app, "/api/answer", payload)
                self.assertEqual((status, first["status"]), (200, "answered"))
                key = first["session_id"]
                url = f"/api/conversations/{key}"
                self.assertEqual(self.call(app, "/api/conversations")[1]["conversations"][0]["title"], payload["query"])
            with self.app(Store(self.root / "data"), ai) as restarted:
                self.assertEqual(self.call(restarted, url, authorized=False)[0], 403)
                self.assertEqual(self.call(restarted, url, method="DELETE", authorized=False)[0], 403)
                status, saved = self.call(restarted, url)
                self.assertEqual(status, 200)
                self.assertEqual(saved["filters"]["source"], source)
                self.assertIn("Atlas quotation includes valves", saved["turns"][0]["answer"]["markdown"])
                self.assertTrue(saved["turns"][0]["answer"]["results"])
                follow = {**payload, "query": "그중 검사 규칙만 다시", "session_id": key}
                status, second = self.call(restarted, "/api/answer", follow)
                self.assertEqual((status, second["session_id"]), (200, key))
                self.assertIn("Atlas quotation", generate.call_args.args[2])
                self.assertEqual(len(self.call(restarted, url)[1]["turns"]), 2)
                (self.root / "archive").rename(self.root / "revoked")
                self.assertIsNone(self.call(restarted, url)[1]["turns"][0]["answer"])
                self.assertEqual(self.call(restarted, url, method="DELETE"), (200, {"ok": True}))
                self.assertEqual(self.call(restarted, url)[0], 404)
                self.assertFalse(self.call(restarted, "/api/conversations")[1]["conversations"])
        with Store(self.root / "data").connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0], 0)

    def test_migration_preserves_index_and_retains_latest_thirty_conversations(self):
        source = self.archive("archive")
        self.mail(source, "atlas")
        with self.store.connection() as conn:
            conn.execute("DROP TABLE conversation_turns")
            conn.execute("DROP TABLE conversations")
        with self.app(Store(self.root / "data"), LocalAI("")) as app:
            self.assertEqual(self.call(app, "/api/conversations"), (200, {"conversations": []}))
            for number in range(31):
                app.store.save_conversation_turn(str(number), f"Question {number}", {}, {})
            saved = self.call(app, "/api/conversations")[1]["conversations"]
            self.assertEqual([item["id"] for item in saved], [str(n) for n in range(30, 0, -1)])
            self.assertEqual(self.call(app, "/api/search", {"query": "Atlas", "filters": {"source": source}})[0], 200)
            self.assertFalse(app.store.conversation_turns("0"))


class EvalHarnessTests(IndexFixture):
    def test_retrieval_only_module_reports_recall_and_timing(self):
        source = self.archive("archive")
        self.mail(source, "atlas")
        cases = self.root / "cases.json"
        cases.write_text(json.dumps([{"question": "Atlas quotation", "expected_keywords": ["valves", "not present"],
                                     "expected_senders": ["ALICE@vendor.example"],
                                     "expected_subjects": ["Atlas quotation"]}]), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "mailsearch.eval_harness", "--data", str(self.store.root),
                                 "--cases", str(cases), "-k", "1", "--retrieval-only"], capture_output=True,
                                text=True, encoding="utf-8", check=True, timeout=10)
        report = json.loads(result.stdout)
        row, = report["cases"]
        self.assertEqual((row["recall_at_k"], row["expected_found"], row["expected_total"]), (.75, 3, 4))
        self.assertEqual(set(row["latency_ms"]), {*TIMINGS, "TTFT", "total"})
        self.assertGreater(row["latency_ms"]["fts"], 0)
        self.assertGreater(row["latency_ms"]["total"], 0)
        self.assertEqual([row["latency_ms"][name] for name in ("rewrite", "followup", "generation")], [0, 0, 0])
        self.assertIsNone(row["latency_ms"]["TTFT"])
        self.assertIsNone(row["valid_sources"])
        self.assertIn("Recall@1", result.stderr)

    def test_full_evaluation_times_stream_and_counts_valid_sources(self):
        source = self.archive("archive")
        self.mail(source, "atlas")
        external = ExternalAI("https://gateway.example.invalid/v1/chat/completions", "fixture", "key",
                              intranet=True, gateway="https://gateway.example.invalid")

        def answer(_question, _excerpts, *, on_delta, **_options):
            markdown = "The Atlas quotation includes valves. [1, 999]"
            on_delta(markdown)
            return markdown

        with patch.object(external, "answer_markdown", side_effect=answer):
            report = evaluate(self.store, LocalAI(""), [{"question": "Atlas quotation"}], 10,
                              retrieval_only=False, external=external)
        row, = report["cases"]
        self.assertEqual((report["citation_pass_rate"], row["valid_sources"]), (1, 1))
        self.assertGreater(row["latency_ms"]["TTFT"], 0)
        self.assertGreater(row["latency_ms"]["total"], row["latency_ms"]["TTFT"])
