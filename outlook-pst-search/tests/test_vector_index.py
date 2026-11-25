from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mailsearch.store import Store, pack_vector
from mailsearch.vector_index import USearchIndex


class PersistentVectorIndexTests(unittest.TestCase):
    def test_reopens_more_than_eight_shards_and_prunes_retired_versions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "index")
            try:
                backend = USearchIndex(store)
            except ImportError:
                self.skipTest("optional USearch is unavailable")
            vector = [1., 0., 0., 0.]
            keys = []
            with store.connection() as conn:
                for n in range(10):
                    folder = root / str(n)
                    folder.mkdir()
                    source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)",
                                          (str(folder),)).lastrowid
                    mail = store.add_mail(conn, source, 1, "a.eml", {
                        "subject": f"Shard {n}", "sender": "", "recipient": "", "sent": "", "folder": "/",
                        "attachments": [], "message_id": "", "reply_ids": "", "body": "Vector evidence",
                    })
                    passage = conn.execute("SELECT id FROM passages WHERE mail=?", (mail,)).fetchone()[0]
                    conn.execute("INSERT INTO vectors VALUES(?,?,?)", (passage, "emb", pack_vector(vector)))
                    keys.append(("emb", source, 1, len(vector)))
            try:
                for key in keys:
                    backend.build(key)
                    self.assertTrue(backend.path(key).exists())
            finally:
                backend.close()
            backend = store.vector_index = USearchIndex(store)
            try:
                with patch.object(backend.pool, "submit", side_effect=AssertionError("cached shard must be mapped")):
                    self.assertEqual(len(store.semantic(vector, "emb", {}, limit=10)), 10)
                with store.connection() as conn:
                    conn.execute("UPDATE sources SET active=2 WHERE id=?", (keys[0][1],))
                    conn.execute("UPDATE sources SET state='removing' WHERE id=?", (keys[1][1],))
                backend.prune()
                self.assertFalse(backend.path(keys[0]).exists())
                self.assertFalse(backend.path(keys[1]).exists())
                self.assertTrue(backend.path(keys[2]).exists())
            finally:
                backend.close()
