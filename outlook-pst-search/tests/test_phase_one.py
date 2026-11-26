import http.client
import io
import json
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date
from unittest.mock import MagicMock, patch

from mailsearch import answer, embedding
from mailsearch.answer import LocalAI
from mailsearch.embedding import GatewayConnections, GatewayTLSOptions, request_gateway_json
from mailsearch.store import Store


class GatewayPoolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(embedding, "gateway_connections", GatewayConnections()))
        self.enterContext(patch.object(embedding, "tls_context", return_value=object()))
        self.https = self.enterContext(patch.object(http.client, "HTTPSConnection"))

    @staticmethod
    def response(data=b'{"ok":true}', *, length=None, close=False):
        sock = MagicMock()
        length = len(data) if length is None else length
        headers = f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n"
        if close:
            headers += "Connection: close\r\n"
        sock.makefile.return_value = io.BytesIO(headers.encode() + b"\r\n" + data)
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response

    def request(self, *, path="/v1/embeddings", retry_stale=True, maximum=1024):
        return request_gateway_json("https://gateway.example.invalid", path, {}, {}, timeout=2,
                                    max_bytes=maximum, tls=GatewayTLSOptions(), retry_stale=retry_stale)

    def test_reuses_only_complete_open_responses(self):
        conn = self.https.return_value
        conn.getresponse.side_effect = [self.response(), self.response(close=True),
                                        self.response(length=100), self.response(b'{"payload": 123}')]
        self.assertEqual(self.request(), {"ok": True})
        self.assertEqual(self.request(), {"ok": True})
        self.https.assert_called_once()
        self.assertEqual(conn.request.call_args.args[3]["Connection"], "keep-alive")
        conn.sock.settimeout.assert_called_once()
        conn.close.assert_called_once()
        with self.assertRaises(http.client.IncompleteRead):
            self.request()
        with self.assertRaisesRegex(ValueError, "gateway_response_size"):
            self.request(maximum=5)
        self.assertEqual(self.https.call_count, 3)
        self.assertEqual(conn.close.call_count, 3)

    def test_stale_retries_once_only_for_opted_in_calls(self):
        old, new, last = MagicMock(), MagicMock(), MagicMock()
        self.https.side_effect = [old, new, last]
        old.getresponse.side_effect = [self.response(), http.client.RemoteDisconnected()]
        new.getresponse.side_effect = [self.response(), http.client.BadStatusLine("bad status")]
        last.getresponse.side_effect = BrokenPipeError()
        self.request()
        self.assertEqual(self.request(path="/v1/rerank"), {"ok": True})
        old.close.assert_called_once()
        with self.assertRaises(http.client.BadStatusLine):
            self.request(path="/v1/chat/completions", retry_stale=False)
        self.assertEqual(self.https.call_count, 2)
        with self.assertRaises(BrokenPipeError):
            self.request()
        self.assertEqual(self.https.call_count, 3)
        new.close.assert_called_once()
        last.close.assert_called_once()

    def test_idle_capacity_expiry_and_tls_key_isolation(self):
        pool = GatewayConnections()
        key = ("gateway.example.invalid", 443, "1", "")
        connections = [MagicMock() for _ in range(5)]
        with patch.object(embedding, "monotonic", return_value=0):
            for connection in connections:
                pool.put(key, connection)
            self.assertIsNone(pool.take((*key[:2], "0", "")))
            self.assertIs(pool.take(key), connections[-1])
        connections[0].close.assert_called_once()
        with patch.object(embedding, "monotonic", return_value=60):
            self.assertIsNone(pool.take(key))
        for connection in connections[1:-1]:
            connection.close.assert_called_once()


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.provider = MagicMock(index_key="embed-model", origin="https://gateway.example.invalid",
                                  rewrite_model="rewrite-model")
        self.ai = LocalAI("", embedding_api=self.provider)
        self.ai.rewrite_model = ""
        self.enterContext(patch.dict("os.environ", {"MAILSEARCH_QUERY_REWRITE": "1"}))

    def singleflight(self, invoke, backend, value):
        started, waiting, release = threading.Event(), threading.Event(), threading.Event()

        class ObservedFuture(Future):
            def result(self, *args, **kwargs):
                if not self.done():
                    waiting.set()
                return super().result(*args, **kwargs)

        def fetch(*args, **kwargs):
            started.set()
            if not release.wait(2):
                raise AssertionError("second caller did not join")
            return value

        backend.side_effect = fetch
        with ThreadPoolExecutor(max_workers=2) as workers, patch.object(answer, "Future", ObservedFuture):
            first = workers.submit(invoke)
            try:
                self.assertTrue(started.wait(1))
                second = workers.submit(invoke)
                self.assertTrue(waiting.wait(1))
            finally:
                release.set()
            self.assertEqual(first.result(), second.result())
        backend.assert_called_once()
        backend.side_effect = None
        backend.return_value = value

    def test_embedding_singleflight_normalization_and_ttl(self):
        now = [100.]
        store = MagicMock(spec=Store)
        with patch("mailsearch.answer.time.monotonic", side_effect=lambda: now[0]):
            self.singleflight(lambda: self.ai.prefetch_query_embeddings(store, ["Atlas A-17"]),
                              self.provider.embed, [[1, 0]])
            index, vectors = self.ai.prefetch_query_embeddings(store, ["atlas  a-17"])
            self.assertEqual((index, vectors), ("embed-model", {"atlas  a-17": [1, 0]}))
            self.provider.embed.assert_called_once()
            now[0] += 900
            self.ai.prefetch_query_embeddings(store, ["Atlas A-17"])
            self.assertEqual(self.provider.embed.call_count, 2)

    def test_rewrite_singleflight_ttl_date_and_config(self):
        now = [100.]
        with (patch.object(self.ai, "retrieval_completion") as complete,
              patch("mailsearch.answer.datetime.date") as calendar,
              patch("mailsearch.answer.time.monotonic", side_effect=lambda: now[0])):
            calendar.today.return_value = date(2026, 11, 26)
            invoke = lambda: self.ai.rewrite_query("이번 주 공급사")
            self.singleflight(invoke, complete, {"sub_queries": ["공급사"]})
            invoke()
            complete.assert_called_once()
            now[0] += 900
            invoke()
            calendar.today.return_value = date(2026, 11, 27)
            invoke()
            self.assertEqual(json.loads(complete.call_args.args[1])["today"], "2026-11-27")
            self.ai.configure_embedding("", self.provider)
            invoke()
            self.assertEqual(complete.call_count, 4)


class SpeculativeTests(unittest.TestCase):
    def test_parallel_candidates_use_narrowed_period_and_sender(self):
        store = MagicMock(spec=Store)
        ai = MagicMock(spec=LocalAI, embedding_index="", rerank_enabled=True)
        ai.retrieval_model_available = True
        question = "Atlas 발신자 Lee 이번 주"
        filters = {"source": 7}
        plan = answer.RewritePlan((question, "Atlas 도면"), (), {"date_from": "2026-11-23", "sender": "Lee"})
        lexical_started = threading.Event()
        rewrite_started = threading.Event()

        def rewrite(_question):
            rewrite_started.set()
            if not lexical_started.wait(2):
                raise AssertionError("lexical search did not start in parallel")
            return plan

        def hit(number):
            return dict(passage=number, id=number, text=f"evidence {number}", fingerprint=str(number),
                        location="본문", kind="mail", attachment=None)

        def lexical(query, scope, **_options):
            if "sender" not in scope:
                lexical_started.set()
                if not rewrite_started.wait(2):
                    raise AssertionError("rewrite did not start in parallel")
                return [hit(1)]
            self.assertEqual(scope["sender"], "Lee")
            self.assertIn("after", scope)
            return [hit(2 if query == question else 3)]

        ai.rewrite_query.side_effect = rewrite
        ai.rerank_candidates.side_effect = lambda _, rows, **opts: rows
        store.lexical.side_effect = lexical
        store.related.return_value = []
        with ThreadPoolExecutor(max_workers=4) as workers:
            evidence, mode, applied, _ = answer.rewritten_evidence(store, ai, question, filters, "", workers=workers)
        self.assertEqual({row["passage"] for row in evidence}, {2, 3})
        self.assertEqual((mode, applied["source"]), ("keyword", 7))
        self.assertEqual(store.lexical.call_count, 3)
        ai.rerank_candidates.assert_called_once()
        self.assertEqual(store.search_session.call_count, 2)
