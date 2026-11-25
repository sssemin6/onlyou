import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from mailsearch.answer import LocalAI, parse_rewrite, rewritten_evidence
from mailsearch.store import Store, rewritten_scope


class QueryRewriteTests(unittest.TestCase):
    question = "작년 하반기 김 부장이 보낸 A17 브래킷 승인과 BOM 변경 차이"

    def test_one_local_generation_preserves_identifier_and_filters(self):
        with patch.dict("os.environ", {"MAILSEARCH_QUERY_REWRITE_MODEL": "tiny:latest"}):
            ai = LocalAI("")
        response = {"sub_queries": ["브래킷 승인", "BOM changed"], "keywords": ["A17"],
                    "filters": {"date_from": "2025-07-01", "date_to": "2025-12-31", "sender": "김 부장",
                                "prefer_attachment": True}}
        with (patch.object(ai, "ensure_model", return_value="tiny:latest"),
              patch.object(ai, "call", return_value={"response": json.dumps(response)}) as call):
            plan = ai.rewrite_query(self.question)
        call.assert_called_once()
        self.assertEqual(json.loads(call.call_args.args[1]["prompt"])["question"], self.question)
        self.assertEqual(plan.sub_queries, ("A17 브래킷 승인", "A17 BOM changed"))
        hints = rewritten_scope({}, plan.filters)
        self.assertIn("after", hints)
        self.assertIn("before", hints)
        self.assertEqual(hints["sender"], "김 부장")
        explicit = {"after": "UI date", "sender": "UI sender", "attachment": "no", "source": 3}
        self.assertEqual(rewritten_scope(explicit, plan.filters), explicit)
        self.assertIsNone(parse_rewrite({"sub_queries": [None, "  "]}, self.question))

    def test_timeout_falls_back_without_queueing_another_generation(self):
        with patch.dict("os.environ", {"MAILSEARCH_QUERY_REWRITE_MODEL": "tiny:latest"}):
            ai = LocalAI("")
        release = threading.Event()
        started = threading.Event()
        finished = threading.Event()

        def delayed(*args, **kwargs):
            started.set()
            release.wait(1)
            finished.set()
            return {"response": "{}"}

        with (patch.object(ai, "ensure_model", return_value="tiny:latest"),
              patch.object(ai, "call", side_effect=delayed) as call,
              patch("mailsearch.answer.QUERY_REWRITE_TIMEOUT", .05)):
            try:
                self.assertIsNone(ai.rewrite_query(self.question))
                self.assertTrue(started.wait(.2))
                self.assertIsNone(ai.rewrite_query(self.question))
                call.assert_called_once()
            finally:
                release.set()
                finished.wait(.3)

    def test_relaxes_inferred_scope_and_fuses_both_retrieval_paths_before_reranking(self):
        ai = MagicMock(spec=LocalAI)
        ai.rerank_enabled = True
        ai.embedding_index = "local"
        ai.rewrite_query.return_value = parse_rewrite(
            {"sub_queries": ["A17 승인", "A17 BOM"], "keywords": ["A17"], "filters": {"sender": "김 부장"}},
            self.question,
        )
        store = MagicMock(spec=Store)

        def hit(n):
            return dict(passage=n, id=n, text=f"evidence {n}", fingerprint=str(n), location="본문",
                        kind="mail", attachment=None)

        def lexical(query, filters, **kwargs):
            return [] if "sender" in filters else [hit(1 if "승인" in query else 2)]

        store.lexical.side_effect = lexical
        store.related.return_value = []
        ai.semantic_search.side_effect = lambda _, query, scope, **options: [] if "sender" in scope else [hit(3)]
        ai.rerank_candidates.side_effect = lambda _, rows, **options: rows
        evidence, mode, applied, count = rewritten_evidence(store, ai, self.question, {"source": 7}, "")
        self.assertEqual((mode, applied, count), ("hybrid", {"source": 7}, 2))
        self.assertEqual([row["passage"] for row in evidence], [3, 1, 2])
        ai.rerank_candidates.assert_called_once()
        self.assertTrue(all(call.args[1].get("source") == 7 for call in store.lexical.call_args_list))
        self.assertTrue(any(call.kwargs.get("required") == ["A17"] for call in store.lexical.call_args_list))
