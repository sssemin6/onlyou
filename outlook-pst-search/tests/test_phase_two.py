import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mailsearch.answer import LocalAI, parse_evidence_gap, supplemental_evidence, validate_markdown_answer
from mailsearch.embedding import GatewayTLSOptions, request_gateway_stream
from mailsearch.external import Excerpt, ExternalAI
from mailsearch.store import Store


class MarkdownTransportTests(unittest.TestCase):
    def setUp(self):
        self.https = self.enterContext(patch("mailsearch.embedding.http.client.HTTPSConnection"))
        self.conn = self.https.return_value
        self.enterContext(patch("mailsearch.embedding.tls_context", return_value=object()))
        self.client = ExternalAI("https://gateway.example.invalid/v1/chat/completions", "test", "token",
                                 intranet=True, gateway="https://gateway.example.invalid")
        self.excerpts = [Excerpt(1, 12, "원문 발췌")]

    def response(self, content, media_type, *, chunked=False):
        socket = MagicMock()
        headers = ("HTTP/1.1 200 OK\r\n" + f"Content-Type: {media_type}\r\n").encode()
        if chunked:
            headers += b"Transfer-Encoding: chunked\r\n\r\n"
            content = b"".join(format(len(chunk), "x").encode() + b"\r\n" + chunk + b"\r\n"
                               for chunk in (content[n:n + 5] for n in range(0, len(content), 5))) + b"0\r\n\r\n"
        else:
            headers += f"Content-Length: {len(content)}\r\n\r\n".encode()
        socket.makefile.return_value = io.BufferedReader(io.BytesIO(headers + content))
        response = http.client.HTTPResponse(socket)
        response.begin()
        self.conn.getresponse.return_value = response
        return response

    @staticmethod
    def event(text):
        return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + "\r\n\r\n"

    def test_chunked_sse_parses_utf8_emits_content_and_stops_at_done(self):
        body = (": keepalive\r\n\r\n" + self.event("## 결과\n") + self.event("답변 [1]") +
                "data: [DONE]\r\n\r\ndata: INVALID\r\n\r\n").encode()
        response = self.response(body, "text/event-stream; charset=utf-8", chunked=True)
        chunks = []
        self.assertEqual(self.client.answer_markdown("질문", self.excerpts, on_delta=chunks.append), "## 결과\n답변 [1]")
        self.assertEqual(chunks, ["## 결과\n", "답변 [1]"])
        options = json.loads(self.conn.request.call_args.args[2])
        self.assertTrue(options["stream"])
        self.assertEqual(options["messages"][0]["role"], "system")
        self.assertTrue(response.closed)
        self.conn.close.assert_called_once()

    def test_json_fallback_uses_the_existing_response_without_a_second_post(self):
        body = json.dumps({"choices": [{"message": {"content": "한 번만 생성 [1]"}}]}).encode()
        self.response(body, "application/json; charset=utf-8")
        chunks = []
        self.assertEqual(self.client.answer_markdown("질문", self.excerpts, on_delta=chunks.append), "한 번만 생성 [1]")
        self.assertEqual(chunks, ["한 번만 생성 [1]"])
        self.conn.request.assert_called_once()
        self.conn.close.assert_called_once()

    def test_generator_close_disposes_open_stream(self):
        response = self.response((self.event("시작") + self.event("다음")).encode(), "text/event-stream")
        stream = request_gateway_stream(self.client.gateway, self.client.path, {}, {}, timeout=1, tls=GatewayTLSOptions())
        self.assertEqual(next(stream)["choices"][0]["delta"]["content"], "시작")
        stream.close()
        self.assertTrue(response.closed)
        self.conn.close.assert_called_once()

    def test_sse_reads_shrink_socket_timeout_under_the_original_deadline(self):
        body = (self.event("첫 답변") + self.event("다음 답변")).encode()
        response = self.response(body, "text/event-stream")
        socket = self.conn.sock
        clock = [10.0]
        readline = response.readline

        def getresponse():
            # http.client detaches the socket if the response will close the connection.
            self.conn.sock = None
            return response

        def read_line(limit):
            line = readline(limit)
            clock[0] = next(read_times)
            return line

        read_times = iter((10.6, 10.98, 11.01))
        self.conn.getresponse.side_effect = getresponse
        with (patch("mailsearch.embedding.monotonic", side_effect=lambda: clock[0]),
              patch.object(response, "readline", side_effect=read_line)):
            stream = request_gateway_stream(self.client.gateway, self.client.path, {}, {}, timeout=1)
            self.assertEqual(next(stream)["choices"][0]["delta"]["content"], "첫 답변")
            with self.assertRaises(TimeoutError):
                next(stream)
        timeouts = [call.args[0] for call in socket.settimeout.call_args_list]
        self.assertEqual(len(timeouts), 3)
        for actual, expected in zip(timeouts, (1.0, 0.4, 0.05), strict=True):
            self.assertAlmostEqual(actual, expected)
        self.assertTrue(response.closed)
        self.conn.close.assert_called_once()

    def test_bad_event_or_callback_cancellation_closes_without_retry(self):
        def stop(_text):
            raise InterruptedError("cancelled")

        for body, callback, expected in (
            (self.event("시작"), stop, InterruptedError),
            (self.event("시작") + "data: invalid\n\n", list().append, json.JSONDecodeError),
        ):
            with self.subTest(expected=expected):
                self.conn.reset_mock()
                response = self.response(body.encode(), "text/event-stream")
                with self.assertRaises(expected):
                    self.client.answer_markdown("질문", self.excerpts, on_delta=callback)
                self.assertTrue(response.closed)
                self.conn.request.assert_called_once()
                self.conn.close.assert_called_once()


class MarkdownSourcesTests(unittest.TestCase):
    def test_grouped_source_numbers_are_split_validated_and_mapped(self):
        excerpts = [Excerpt(number, 10 + number, f"원문 {number}", mail=20 + number) for number in (1, 2, 3)]
        result = validate_markdown_answer("확정 [1, 2]. 비교 [1, 2, 3] 및 [3, 99, 2].", excerpts)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["markdown"], "확정 [1][2]. 비교 [1][2][3] 및 [3][2].")
        self.assertEqual([(item["index"], item["passage"]) for item in result["sources"]], [(1, 11), (2, 12), (3, 13)])

    def test_valid_inline_sources_keep_original_excerpt_and_drop_unknown_indices(self):
        excerpts = [Excerpt(1, 10, "승인 회신 원문", {"subject": "승인", "sender": "Lee", "sent": "2026-11-23"},
                            window_start=0, mail=20)]
        result = validate_markdown_answer("## 요약\n확정했습니다. [1][99][1]", excerpts, gap_aspects=("별도 요청",))
        self.assertEqual((result["status"], result["format"], result["claims"]), ("partial", "markdown", []))
        self.assertEqual(result["markdown"], "## 요약\n확정했습니다. [1][1]")
        self.assertEqual(result["sources"], [{
            "index": 1, "passage": 10, "mail_id": 20, "subject": "승인", "sender": "Lee",
            "sent": "2026-11-23", "quote": "승인 회신 원문", "window": True,
        }])
        for text in ("", "## 요약\n확인되지 않습니다. [1]", "허용 범위 밖입니다. [99]"):
            with self.subTest(text=text):
                insufficient = validate_markdown_answer(text, excerpts)
                self.assertEqual((insufficient["status"], insufficient["sources"]), ("insufficient", []))


class SupplementalTargetsTests(unittest.TestCase):
    def test_targets_expand_thread_and_attachment_within_original_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive"
            other = root / "other"
            archive.mkdir()
            other.mkdir()
            store = Store(root / "data")
            original = {"subject": "Atlas drawing", "sender": "Lee", "recipient": "", "sent": "2026-11-23",
                        "folder": "/", "attachments": [("specification.pdf", None)], "message_id": "<atlas@example.test>",
                        "reply_ids": "", "body": "기존 도면을 검토했습니다."}
            reply = {**original, "attachments": [], "message_id": "<reply@example.test>",
                     "reply_ids": original["message_id"], "body": "도면 개정 B를 승인했습니다."}
            with store.connection() as conn:
                source, outside = [conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)",
                                               (str(path),)).lastrowid for path in (archive, other)]
                mail = store.add_mail(conn, source, 1, "atlas.eml", original)
                attachment = conn.execute("SELECT id FROM attachments WHERE mail=?", (mail,)).fetchone()[0]
                store.add_passage(conn, mail, original, attachment, "specification.pdf", "2페이지", 0, "두께는 15 mm입니다.")
                store.add_mail(conn, source, 1, "reply.eml", reply)
                excluded = store.add_mail(conn, outside, 1, "private.eml",
                                          {**reply, "message_id": "<private@example.test>", "body": "다른 보관함 자료"})
                passage = conn.execute("SELECT id FROM passages WHERE mail=? AND location='본문'", (mail,)).fetchone()[0]
            filters = {"source": source, "regular_only": True}
            first = store.passages([passage], filters)
            ai = LocalAI("")
            ai.rewrite_model = "fixture"
            question = "Atlas 첨부 사양과 회신을 확인해줘"
            gap = parse_evidence_gap({"sufficient": False, "expand_mails": [passage, excluded],
                                      "read_attachments": ["specification", "missing", "ignored"]}, question,
                                     expand_targets=True)
            self.assertEqual(gap.queries, ())
            self.assertEqual(gap.read_attachments, ("specification", "missing"))
            with (patch.dict("os.environ", MAILSEARCH_AGENTIC_RETRY="1", MAILSEARCH_QUERY_REWRITE="1"),
                  patch.object(ai, "diagnose_evidence_gap", return_value=gap) as diagnose,
                  patch("mailsearch.answer.evidence_for") as text_search):
                rows, mode, _, ran = supplemental_evidence(
                    store, ai, question, filters, "", first, "keyword", 1, expand_targets=True,
                )
            self.assertTrue(ran)
            self.assertEqual(mode, "keyword")
            self.assertEqual({row["text"] for row in rows},
                             {original["body"], reply["body"], "두께는 15 mm입니다."})
            self.assertTrue(all(row["source"] == source for row in rows))
            self.assertEqual(diagnose.call_args.args[1][0]["attachment_names"], ["specification.pdf"])
            text_search.assert_not_called()
