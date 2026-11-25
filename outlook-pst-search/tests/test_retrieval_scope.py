from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mailsearch.store import Store


class ScopedRetrievalTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "index")

    def source(self, conn, name):
        folder = self.root / name
        folder.mkdir()
        return conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)",
                            (str(folder),)).lastrowid

    def add(self, conn, source, item, *, subject="", sender="", sent="", body="", reply=""):
        return self.store.add_mail(conn, source, 1, item, {
            "subject": subject, "sender": sender, "recipient": "", "sent": sent, "folder": "/",
            "attachments": [], "message_id": f"<{item}@example.invalid>", "reply_ids": reply, "body": body,
        })

    def test_narrow_range_can_recover_hits_beyond_global_fts_window(self):
        with self.store.connection() as conn:
            noise = self.source(conn, "noise")
            target = self.source(conn, "target")
            for n in range(1070):
                self.add(conn, noise, str(n), subject="Atlas", body="Atlas")
            self.add(conn, target, "target", sender="김 부장", sent="2026-07-15", body="Atlas")
        for filters in ({"source": target, "after": "2026-07-01"}, {"sender": "김 부장"}, {"after": "2026-07-01"}):
            with self.subTest(filters=filters):
                rows = self.store.lexical("Atlas", filters)
                self.assertEqual([row["source"] for row in rows], [target])

    def test_related_reply_selects_late_decision_and_matching_attachment(self):
        with self.store.connection() as conn:
            source = self.source(conn, "thread")
            seed = self.add(conn, source, "seed", subject="Atlas brackets", body="Please check")
            reply = self.add(conn, source, "reply", subject="RE: Atlas brackets",
                             body="회의록과 배경 설명입니다.\n" * 140 + "\nAtlas brackets 양산 승인을 철회합니다.",
                             reply="<seed@example.invalid>")
            attachment = conn.execute("INSERT INTO attachments(mail,name,status) VALUES(?,'BOM.txt','완료')",
                                      (reply,)).lastrowid
            self.store.add_passage(conn, reply, dict(subject="Atlas brackets", sender="", recipient=""), attachment,
                                   "BOM.txt", "내용", 12, "Atlas brackets BOM 3T 적용")
        rows = self.store.related([seed], {}, question="Atlas brackets BOM 승인")
        self.assertEqual(len(rows), 2)
        self.assertIn("철회합니다.", rows[0]["text"])
        self.assertGreater(rows[0]["start"], 0)
        self.assertEqual(rows[1]["attachment"], "BOM.txt")
