import unittest
from unittest.mock import MagicMock, patch

from mailsearch.answer import LocalAI, evidence_for
from mailsearch.store import Store


def row(number):
    return dict(id=number, passage=number, fingerprint=str(number), kind="mail", subject=f"Part {number}",
                sender="Kim", sent="2025-08-20T07:00:00+00:00", attachment=f"BOM-{number}.xlsx",
                location="Parts!C12", text=f"Thickness notes {number}")


class HybridRerankingTests(unittest.TestCase):
    def test_reranks_a_result_beyond_rank_24_with_author_and_date(self):
        store = MagicMock(spec=Store)
        store.lexical.return_value = [row(n) for n in range(1, 81)]
        store.related.return_value = []
        store.evidence_context.return_value = []
        provider = MagicMock()
        provider.index_key = "bge"
        provider.models = {"rerank": "bge-reranker-v2-m3"}
        provider.rerank.return_value = [69, *range(69), *range(70, 80)]
        ai = LocalAI("", embedding_api=provider)
        with patch.object(ai, "semantic_search", return_value=[row(n) for n in range(1, 81)]) as semantic:
            evidence, mode = evidence_for(store, ai, "BOM approval", {}, "", limit=9, expand=True)
        self.assertEqual(mode, "hybrid")
        self.assertEqual(evidence[0]["passage"], 70)
        self.assertEqual(semantic.call_args.kwargs, {"limit": 80, "min_similarity": .18})
        query, documents = provider.rerank.call_args.args
        self.assertEqual(query, "BOM approval")
        self.assertEqual(len(documents), 80)
        self.assertIn("Sent: 2025-08-20T07:00:00+00:00\nFrom: Kim\nAttachment: BOM-70.xlsx", documents[69])
