import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mailsearch.answer import LocalAI
from mailsearch.extract import extract_bytes, extract_document
from mailsearch.indexer import Indexer
from mailsearch.server import App
from mailsearch.store import MAIL_INDEX_FORMAT, Store


class ReadPoolTests(unittest.TestCase):
    def test_reader_is_reused_across_threads_and_writes_keep_their_own_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            self.addCleanup(store.close)
            with store.thread_connection():
                with store.connection() as writer:
                    writer.execute("INSERT INTO sources(path,kind) VALUES('archive','eml_dir')")
                with store.search_session(), store.connection() as reader:
                    self.assertIsNot(reader, writer)
                    self.assertEqual(reader.execute("PRAGMA query_only").fetchone()[0], 1)
                    self.assertEqual(reader.execute("PRAGMA cache_size").fetchone()[0], -131072)
                    self.assertEqual(reader.execute("PRAGMA mmap_size").fetchone()[0], 536870912)
                    self.assertEqual(reader.execute("SELECT embedding_text_eligible('Text with enough letters')").fetchone()[0], 1)
                    reader.execute("BEGIN")
                    self.assertEqual(reader.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                with store.connection() as same_writer:
                    self.assertIs(same_writer, writer)
                    same_writer.execute("INSERT INTO sources(path,kind) VALUES('second-archive','eml_dir')")

            def read():
                with store.search_session(), store.connection() as connection:
                    return connection, connection.in_transaction, connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

            with ThreadPoolExecutor(max_workers=1) as executor:
                reused, in_transaction, count = executor.submit(read).result()
            self.assertIs(reused, reader)
            self.assertFalse(in_transaction)
            self.assertEqual(count, 2)

            with self.assertRaises(sqlite3.OperationalError), store.search_session(), store.connection() as connection:
                connection.execute("DELETE FROM sources")
            with self.assertRaises(sqlite3.ProgrammingError):
                reader.execute("SELECT 1")
            with store.search_session(), store.connection() as idle:
                self.assertIsNot(idle, reader)
            store.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                idle.execute("SELECT 1")

    def test_pool_caps_idle_connections_and_closes_outstanding_leases_on_return(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            self.addCleanup(store.close)
            barrier = threading.Barrier(8, timeout=5)

            def borrow():
                with store.search_session(), store.connection() as conn:
                    barrier.wait()
                return conn

            with ThreadPoolExecutor(max_workers=8) as executor:
                readers = list(executor.map(lambda _: borrow(), range(8)))
            self.assertEqual(len(store.read_pool), 6)
            for conn in readers:
                if conn not in store.read_pool:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        conn.execute("SELECT 1")
            with store.search_session(), store.connection() as leased:
                store.close()
                self.assertEqual(leased.execute("SELECT 1").fetchone()[0], 1)
            for conn in readers:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")


class SuggestionAPITests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(temp)
        paths = [root / name for name in ("own", "other", "missing")]
        for path in paths[:2]:
            path.mkdir()
        self.store = Store(root / "state")
        indexer = Indexer(self.store)
        self.addCleanup(indexer.stop)
        with self.store.connection() as conn:
            self.source, other, missing = [
                conn.execute("INSERT INTO sources(path,kind,active,state) VALUES(?,'eml_dir',1,'ready')",
                             (str(path),)).lastrowid for path in paths
            ]
            for i in range(12):
                self.add_mail(conn, self.source, f"quote-{i}.eml", f"Quartz quote {i}")
            self.add_mail(conn, self.source, "duplicate.eml", "QUARTZ QUOTE 11")
            self.add_mail(conn, self.source, "calendar.eml", "Planning calendar",
                          sender="Quartz Labs <orders@example.test>",
                          recipient='Team <team@example.test>; "Quartz Parts" <quartz.parts@example.test>')
            self.add_mail(conn, self.source, "draft.eml", "Quartz unreleased", version=2)
            self.add_mail(conn, other, "other.eml", "Quartz outside archive")
            self.add_mail(conn, missing, "missing.eml", "Quartz inaccessible")
            title = "Quartz specification.txt"
            (paths[0] / title).write_text("Quartz specification contents", encoding="utf-8")
            self.store.add_document(conn, self.source, 1, title, {
                "name": title, "folder": "/", "sent": "2026-11-26T10:00:00+00:00",
                "status": "완료", "sections": [("본문", "Quartz specification contents")],
            })
        self.app = App(0, self.store, indexer, LocalAI(None, None, 0))
        self.addCleanup(self.app.server_close)
        server = threading.Thread(target=self.app.serve_forever, daemon=True)
        server.start()
        self.addCleanup(server.join)
        self.addCleanup(self.app.shutdown)

    def add_mail(self, conn, source, item, subject, *, version=1, sender="Lee", recipient="Kim"):
        self.store.add_mail(conn, source, version, item, {
            "subject": subject, "sender": sender, "recipient": recipient, "body": "Planning calendar notes",
            "folder": "/", "message_id": "", "reply_ids": "", "attachments": [],
            "sent": "2026-11-26T10:00:00+00:00",
        })

    def suggest(self, prefix, filters):
        connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=5)
        try:
            connection.request("POST", "/api/suggest", json.dumps({"prefix": prefix, "filters": filters}), {
                "Content-Type": "application/json",
                "X-Mail-Session": self.app.token,
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())["suggestions"]
        finally:
            connection.close()

    def test_short_input_duplicates_and_limit(self):
        self.assertEqual(self.suggest(" q ", {}), [])
        matches = self.suggest("QuArTz qu", {"tab": "mail", "source": self.source})
        self.assertEqual(len(matches), 8)
        self.assertEqual(len({row["label"].casefold() for row in matches}), 8)
        self.assertTrue(all(row["kind"] == "subject" and row["query"] == row["label"] for row in matches))
        self.assertEqual(len(self.store.suggest("quartz qu", {"source": self.source}, limit=3)), 3)

    def test_scope_filters_and_sender_recipient_document_candidates(self):
        self.assertEqual(self.suggest("quart", {"tab": "document", "source": self.source}), [
            {"label": "Quartz specification.txt", "query": "Quartz specification.txt", "kind": "document"}
        ])
        scope = {"tab": "mail", "source": self.source, "subject": "planning", "after": "2026-11-25"}
        self.assertEqual({item["label"] for item in self.suggest("quartz", scope)},
                         {"Quartz Labs", "Quartz Parts", "quartz.parts@example.test"})
        self.assertEqual(self.suggest("quartz", {**scope, "after": "2026-11-28"}), [])
        self.assertEqual(self.suggest("quartz unre", {}), [])
        self.assertEqual(self.suggest("quartz inaccess", {}), [])
        self.assertEqual(self.suggest("quartz outside", {"source": self.source}), [])
        self.assertEqual(self.suggest("quartz\x00quote", {}), [])

    def test_fts_prefix_finds_candidates_older_than_the_recent_window(self):
        with self.store.connection() as conn:
            self.add_mail(conn, self.source, "old.eml", "별빛 프로젝트")
            for i in range(100):
                self.add_mail(conn, self.source, f"new-{i}.eml", f"Unrelated report {i}")
        self.assertEqual(self.suggest("별빛 프", {"tab": "mail", "source": self.source}), [
            {"label": "별빛 프로젝트", "query": "별빛 프로젝트", "kind": "subject"}
        ])

    def test_server_close_releases_idle_readers(self):
        self.suggest("quartz", {"source": self.source})
        self.assertTrue(self.store.read_pool)
        reader = self.store.read_pool[0]
        self.app.shutdown()
        self.app.server_close()
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.execute("SELECT 1")


class ExtractionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.archive = self.root / "archive"
        self.archive.mkdir()
        for i in range(4):
            (self.archive / f"{i}.txt").write_text(f"Document text {i} for search", encoding="utf-8")
            mail = EmailMessage()
            mail["Subject"] = f"Proposal {i}"
            mail.set_content(f"Email body {i} for search")
            mail.add_attachment(f"Attachment text {i} for search", filename=f"Attachment-{i}.txt")
            mail.add_attachment("Shared attachment text", filename="shared.txt")
            (self.archive / f"{i}.eml").write_bytes(mail.as_bytes())

    def make_indexer(self, workers):
        store = Store(self.root / f"state-{workers}")
        self.addCleanup(store.close)
        with patch.dict(os.environ, {"MAILSEARCH_EXTRACT_WORKERS": str(workers)}):
            indexer = Indexer(store)
        self.addCleanup(indexer.stop)
        return store, indexer

    def snapshot(self, workers):
        store, indexer = self.make_indexer(workers)
        source = indexer.register(self.archive)
        with indexer.lock:
            worker = indexer.worker
        if worker:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        row = store.source(source)
        self.assertEqual((row["state"], row["done"], row["failed"], row["omitted"]), ("ready", 8, 0, 0))
        with store.connection() as conn:
            records = conn.execute("""
                SELECT m.item,m.kind,m.fingerprint,a.name,a.status,a.content_key,p.location,p.text
                FROM mails m JOIN passages p ON p.mail=m.id LEFT JOIN attachments a ON a.id=p.attachment
                WHERE m.source=? AND m.version=? ORDER BY m.item,p.id
            """, (source, row["active"])).fetchall()
            cache = conn.execute("SELECT * FROM attachment_extracts ORDER BY digest").fetchall()
        self.assertTrue(all(record["fingerprint"].startswith(MAIL_INDEX_FORMAT + ":") for record in records))
        self.assertTrue(any(record["text"] == "Attachment text 3 for search" for record in records))
        self.assertEqual(len(cache), 5)
        return [tuple(record) for record in records], [tuple(row) for row in cache]

    def test_parallel_documents_and_attachments_have_the_same_index_as_sequential_extraction(self):
        sequential = self.snapshot(1)
        documents = threading.Barrier(2, timeout=5)
        attachments = threading.Barrier(2, timeout=5)
        seen = {"documents": set(), "attachments": set()}
        lock = threading.Lock()

        def observe(stage, barrier):
            with lock:
                names = seen[stage]
                synchronize = threading.current_thread().name not in names
                names.add(threading.current_thread().name)
            if synchronize:
                barrier.wait()

        standalone = threading.local()

        def document(*args, **kwargs):
            observe("documents", documents)
            standalone.active = True
            try:
                return extract_document(*args, **kwargs)
            finally:
                standalone.active = False

        def attachment(*args, **kwargs):
            if not getattr(standalone, "active", False):
                observe("attachments", attachments)
            return extract_bytes(*args, **kwargs)

        with (patch("mailsearch.indexer.extract_document", side_effect=document),
              patch("mailsearch.extract.extract_bytes", side_effect=attachment)):
            self.assertEqual(sequential, self.snapshot(2))
        self.assertEqual({stage: len(workers) for stage, workers in seen.items()}, {"documents": 2, "attachments": 2})

    def test_stop_waits_for_extraction_workers_to_exit(self):
        self.interrupt_extraction("stop", "queued")

    def test_pause_waits_for_active_workers_and_keeps_the_revision_resumable(self):
        self.interrupt_extraction("pause", "paused")

    def interrupt_extraction(self, action, expected_state):
        store, indexer = self.make_indexer(2)
        started = threading.Barrier(3, timeout=5)
        active = set()
        lock = threading.Lock()

        def interruptible_extract(*args, check, **kwargs):
            worker = threading.current_thread()
            with lock:
                active.add(worker)
            started.wait()
            while True:
                check()
                time.sleep(.005)

        with patch("mailsearch.indexer.extract_document", side_effect=interruptible_extract):
            source = indexer.register(self.archive)
            started.wait()
            if action == "stop":
                indexer.stop()
            else:
                worker = indexer.worker
                indexer.command(source, action)
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(len(active), 2)
        self.assertTrue(all(not worker.is_alive() for worker in active))
        row = store.source(source)
        self.assertEqual(row["state"], expected_state)
        self.assertIsNotNone(row["pending"])
