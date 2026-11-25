import unittest
from unittest.mock import patch

from mailsearch.external import ExternalAI, Reviews, compact_json, evidence_payload, pack_excerpts


class EvidenceBudgetTests(unittest.TestCase):
    def test_packing_uses_reviewed_contiguous_excerpts_and_includes_different_dates(self):
        rows = []
        for number, decision in enumerate(("검토", "검토", "승인", "철회"), 1):
            text = "  " + "회의 기록입니다. " * 130 + f"\nAtlas {decision}합니다."
            rows.append(dict(passage=number, source=5, subject="Atlas", sent=f"2025-08-0{number}",
                             sender="Kim", text=text, location="본문"))
        excerpts = pack_excerpts("Atlas 진행 이력", rows, 4000, with_context=True)
        self.assertLessEqual(len(compact_json(evidence_payload("Atlas 진행 이력", excerpts)).encode()), 4000)
        self.assertEqual({item.passage for item in excerpts}, {1, 3, 4})
        self.assertTrue(all(item.original_in(rows[item.passage - 1]) for item in excerpts))
        self.assertTrue(any(item.start > 0 and "철회합니다" in item.text for item in excerpts))

    def test_character_profiles_and_external_metadata_boundary(self):
        provider = ExternalAI("https://api.example.invalid/v1/chat/completions", "model", "key")
        rows = [dict(passage=n, subject=f"Project {n}", sender="Kim", text=str(n) + "가" * 1099,
                     location="본문") for n in range(1, 26)]
        lengths = []
        for profile in ("4000", "12000", "24000"):
            with patch.dict("os.environ", {"MAILSEARCH_EXTERNAL_EVIDENCE_CHARS": profile}, clear=True):
                budget = provider.review_budget
                _, review = Reviews().create("Question", rows, review_budget=budget, review_chars=provider.review_chars)
            lengths.append(sum(len(item.text) for item in review.excerpts))
            self.assertLessEqual(lengths[-1], int(profile))
            self.assertTrue(all(item.context is None for item in review.excerpts))
            self.assertLessEqual(len(compact_json(evidence_payload("Question", review.excerpts)).encode()), budget)
        self.assertEqual(lengths, [4000, 12000, 24000])
