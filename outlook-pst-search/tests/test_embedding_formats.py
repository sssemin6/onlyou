from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mailsearch.indexer import embedding_input
from mailsearch.store import Store, contextual_model_key, pack_vector


class EmbeddingFormatTests(unittest.TestCase):
    def test_only_mail_body_embedding_depends_on_message_metadata(self):
        part = dict(id=1, subject="Bracket A17", sender="Kim", sent="2025-07-14T13:00:00+00:00",
                    name="BOM.xlsx", location="Parts!C12", text="Thickness 3T", embedding_key=b"digest")
        key, document = embedding_input(part)
        self.assertEqual(document, "[BOM.xlsx | Parts!C12]\nThickness 3T")
        self.assertEqual(key, embedding_input({**part, "id": 2, "sender": "Lee", "sent": "2026-01-01",
                                              "subject": "Re: Bracket A17"})[0])
        self.assertNotEqual(key, embedding_input({**part, "location": "Parts!C13"})[0])
        self.assertEqual(embedding_input({**part, "embedding_key": None, "name": None, "location": "본문"}),
                         (1, "[Bracket A17 | Kim | 2025-07-14 | 본문]\nThickness 3T"))

    def test_old_archive_remains_searchable_and_new_archive_uses_contextual_vectors_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "index")
            with store.connection() as conn:
                for fresh in (False, True):
                    folder = root / str(fresh)
                    folder.mkdir()
                    source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)",
                                          (str(folder),)).lastrowid
                    distinct = "new archive" if fresh else "old archive"
                    mail = store.add_mail(conn, source, 1, "m.eml", {
                        "subject": distinct, "sender": "", "recipient": "", "sent": "", "folder": "/",
                        "attachments": [], "message_id": "", "reply_ids": "", "body": "Evidence",
                    })
                    passage = conn.execute("SELECT id FROM passages WHERE mail=?", (mail,)).fetchone()[0]
                    conn.execute("INSERT INTO vectors VALUES(?,?,?)", (passage, "bge", pack_vector([1., 0.])))
                    if fresh:
                        key = contextual_model_key("bge")
                        conn.execute("INSERT INTO vectors VALUES(?,?,?)", (passage, key, pack_vector([.8, .6])))
                        conn.execute("INSERT INTO embedding_revisions VALUES(?,?,?)", (source, 1, key))
            rows = store.semantic_contextual([1., 0.], "bge", {})
            self.assertEqual([row["subject"] for row in rows], ["new archive", "old archive"])
            self.assertAlmostEqual(rows[0]["similarity"], .8)
            self.assertAlmostEqual(rows[1]["similarity"], 1.)
