import http.client
import json
from contextlib import nullcontext
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mailsearch.answer import LocalAI
from mailsearch.indexer import Indexer, source_signature
from mailsearch.outlook import OutlookError, OutlookStore, outlook_store
from mailsearch.server import App
from mailsearch.store import Store
from test_mailsearch import await_source


PST_HEADER = b"!BDN" + bytes(4) + b"SM" + bytes(2)
ATTACHMENT = b"Atlas inspection detail for a synthetic mail fixture."


def sharing_violation(code=32):
    exc = PermissionError("synthetic Windows sharing violation")
    exc.winerror = code
    return exc


def mapistore():
    part = SimpleNamespace(get_payload=lambda *, decode: ATTACHMENT)
    mail = {
        "folder": r"\\Fixture Archive\Inbox", "subject": "Atlas outlook fixture",
        "sender": "Fixture person", "recipient": "Fixture team", "sent": "",
        "message_id": "<atlas@example.invalid>", "reply_ids": "",
        "body": "Searchable Outlook inspection notes.",
        "attachments": [("mapidetail.txt", part)],
    }
    session = MagicMock(spec=OutlookStore)
    session.store_id = "fixture-store-id"
    def identities(check):
        check()
        yield "synthetic-entry-id", mail["folder"]

    session.identities.side_effect = identities
    session.read.side_effect = lambda *args: dict(mail)
    return session


class OutlookOpenOnly(unittest.TestCase):
    def test_open_only_never_mounts_missing_pst_or_starts_outlook(self):
        com = MagicMock(com_error=OSError)
        namespace = MagicMock()
        namespace.Stores = [SimpleNamespace(FilePath="another.pst")]
        client = MagicMock()
        client.GetActiveObject.return_value.GetNamespace.return_value = namespace
        with patch("mailsearch.outlook.outlook_modules", return_value=(com, client)):
            with self.assertRaisesRegex(OutlookError, "^outlook_pst_not_open$"):
                with outlook_store("outlook:target.pst", open_only=True):
                    self.fail("A closed PST was opened")
        client.GetActiveObject.assert_called_once_with("Outlook.Application")
        client.Dispatch.assert_not_called()
        namespace.AddStoreEx.assert_not_called()
        namespace.RemoveStore.assert_not_called()
        com.CoUninitialize.assert_called_once()

    def test_open_only_uses_the_exact_store_and_leaves_it_mounted(self):
        com = MagicMock(com_error=OSError)
        target = MagicMock(FilePath="fixtures/target.pst", StoreID="expected")
        namespace = MagicMock(Stores=[SimpleNamespace(FilePath="other/target.pst"), target])
        client = MagicMock()
        client.GetActiveObject.return_value.GetNamespace.return_value = namespace
        with patch("mailsearch.outlook.outlook_modules", return_value=(com, client)):
            with outlook_store("outlook:fixtures/target.pst", open_only=True) as session:
                self.assertEqual(session.store_id, "expected")
                self.assertIs(session.root, target.GetRootFolder.return_value)
            client.GetActiveObject.side_effect = OSError("Not in the running object table")
            with self.assertRaisesRegex(OutlookError, "^outlook_unavailable$"):
                with outlook_store("outlook:fixtures/target.pst", open_only=True):
                    self.fail("Outlook was started for a PST")
        client.Dispatch.assert_not_called()
        namespace.AddStoreEx.assert_not_called()
        namespace.RemoveStore.assert_not_called()


class LockedPstOutlook(unittest.TestCase):
    def setUp(self):
        workspace = self.enterContext(TemporaryDirectory())
        self.archive = Path(workspace) / "archives"
        self.archive.mkdir()
        self.store = Store(Path(workspace) / "data")
        self.indexer = Indexer(self.store)
        self.app = App(0, self.store, self.indexer, LocalAI(""))
        self.addCleanup(self.store.close)
        self.addCleanup(self.indexer.stop)
        self.addCleanup(self.app.server_close)
        thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.app.shutdown)

    def request(self, route, data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=8)
        try:
            headers = {"X-Mail-Session": self.app.token, "Content-Type": "application/json"}
            conn.request("GET" if data is None else "POST", route,
                         None if data is None else json.dumps(data), headers)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            conn.close()

    def completed(self, source):
        row = await_source(self.store, source, {"ready", "error", "empty"})
        deadline = time.monotonic() + 5
        while source in self.indexer.events and time.monotonic() < deadline:
            time.sleep(.01)
        return row

    def export_fixture(self, _path, stage, _event, _source):
        message = EmailMessage()
        message["Subject"] = "Atlas readpst fixture"
        message["Message-ID"] = "<atlas@example.invalid>"
        message.set_content("Searchable file export inspection notes.")
        message.add_attachment(ATTACHMENT, maintype="text", subtype="plain", filename="mapidetail.txt")
        output = stage / "eml"
        output.mkdir(parents=True, exist_ok=True)
        (output / "1").write_bytes(message.as_bytes())
        (stage / "export-complete").touch()
        return output

    def assert_attachment(self, source):
        rows = self.store.lexical("Atlas inspection detail", {"source": source})
        self.assertTrue(rows)
        hit = next(row for row in rows if row["attachment"] == "mapidetail.txt")
        opened = self.store.open_passage(hit["passage"])
        attachment = next(item for item in opened["attachments"] if item["name"] == "mapidetail.txt")
        self.assertEqual(self.request(f"/api/attachment/{attachment['id']}?inline=1"),
                         (200, "text/plain; charset=utf-8", ATTACHMENT))

    def test_single_pst_auto_fallback_handles_signature_and_readpst_locks(self):
        for where in ("signature", "stable_input", "export"):
            with self.subTest(where=where):
                pst = self.archive / (where + ".pst")
                pst.write_bytes(PST_HEADER)
                patches = {
                    "signature": patch("mailsearch.indexer.source_signature", side_effect=sharing_violation()),
                    "stable_input": patch("mailsearch.indexer.stable_input", side_effect=sharing_violation(33)),
                    "export": patch.object(self.indexer, "export", side_effect=sharing_violation()),
                }
                session = mapistore()
                with (patches[where],
                      patch("mailsearch.indexer.outlook_store", return_value=nullcontext(session)) as opened):
                    source = self.indexer.register(pst)
                    row = self.completed(source)
                    self.assertEqual((row["kind"], row["state"], row["done"]), ("pst", "ready", 1))
                    opened.assert_called_once_with("outlook:" + str(pst), open_only=True)
                    self.assert_attachment(source)

    def test_single_pst_remains_a_pst_source_when_outlook_is_unavailable(self):
        pst = self.archive / "unavailable.pst"
        pst.write_bytes(PST_HEADER)
        with (patch("mailsearch.indexer.stable_input", side_effect=sharing_violation()),
              patch("mailsearch.indexer.outlook_store", side_effect=OutlookError("outlook_pywin32")) as opened):
            source = self.indexer.register(pst)
            row = self.completed(source)
        opened.assert_called_once_with("outlook:" + str(pst), open_only=True)
        self.assertEqual((row["kind"], row["state"], row["issue"], row["active"]),
                         ("pst", "error", "pst_in_use", None))

    def test_locked_pst_can_be_registered_and_its_search_results_opened(self):
        pst = self.archive / "already-open.pst"
        pst.write_bytes(PST_HEADER)
        regular_open = Path.open

        def check_open(path, mode="r", *args, **kwargs):
            if path == pst and mode == "rb":
                raise sharing_violation()
            return regular_open(path, mode, *args, **kwargs)

        session = mapistore()
        with (patch.object(Path, "open", check_open),
              patch("mailsearch.indexer.outlook_store", return_value=nullcontext(session)),
              patch("mailsearch.store.outlook_store", return_value=nullcontext(session)) as readable):
            source = self.indexer.register(pst)
            row = self.completed(source)
            self.assertEqual((row["state"], row["done"]), ("ready", 1))
            self.assert_attachment(source)
        readable.assert_called_with("outlook:" + str(pst), open_only=True)

    def test_folder_combines_mapi_and_eml_and_replaces_mapi_when_pst_is_unlocked(self):
        nested = self.archive / "nested"
        nested.mkdir()
        pst = nested / "locked.pst"
        pst.write_bytes(PST_HEADER)
        (self.archive / "other.eml").write_text("Subject: Separate archive fixture\n\nOther search result.")
        session = mapistore()
        with (patch("mailsearch.indexer.stable_input", side_effect=sharing_violation()),
              patch("mailsearch.indexer.outlook_store", return_value=nullcontext(session)) as opened):
            source = self.indexer.register(self.archive)
            row = self.completed(source)
            self.assertEqual((row["kind"], row["state"], row["done"], row["omitted"]),
                             ("eml_dir", "ready", 2, 0))
            summary, = self.store.sources()
            self.assertEqual(summary["deferred_psts"], 0)
            label = "nested/locked.pst/"
            original = self.store.pst_files(source)[label]
            self.assertEqual(original, "outlook:" + session.store_id)
            self.assertTrue(self.store.lexical("Separate archive fixture", {"source": source}))
            mail = self.store.lexical("Outlook inspection notes", {"source": source})[0]
            self.assertTrue(mail["folder"].startswith("/" + label))
            opened.assert_called_once_with("outlook:" + str(pst), open_only=True)
            self.assert_attachment(source)

            # If the Outlook store closes during refresh, no partial new rows replace the previous archive.
            good_read = session.read.side_effect
            session.identities.side_effect = None
            session.identities.return_value = [("first", mail["folder"]), ("second", mail["folder"])]

            def interrupted(entry, *args):
                if entry == "second":
                    raise OutlookError("outlook_item_changed")
                return {**good_read(entry, *args), "subject": "Incomplete Outlook retry"}

            session.read.side_effect = interrupted
            self.indexer.command(source, "refresh")
            held = self.completed(source)
            self.assertEqual((held["state"], held["done"], held["omitted"]), ("ready", 2, 1))
            self.assertEqual(self.store.pst_files(source)[label], original)
            self.assertTrue(self.store.lexical("Outlook inspection notes", {"source": source}))
            self.assertFalse(self.store.lexical("Incomplete Outlook retry", {"source": source, "subject": "Incomplete"}))

        with patch.object(self.indexer, "export", side_effect=self.export_fixture) as export:
            self.indexer.command(source, "refresh")
            refreshed = self.completed(source)
        export.assert_called_once()
        self.assertEqual((refreshed["state"], refreshed["done"], refreshed["omitted"]), ("ready", 2, 0))
        self.assertEqual(self.store.pst_files(source)[label], source_signature(pst, "pst"))
        self.assertFalse(self.store.lexical("inspection notes", {"source": source, "subject": "Atlas outlook fixture"}))
        with self.store.connection() as conn:
            active_items = conn.execute("SELECT item FROM mails WHERE source=? AND version=? ORDER BY item",
                                        (source, refreshed["active"])).fetchall()
        self.assertEqual([row["item"] for row in active_items], [label + "1", "other.eml"])

    def test_original_attachment_from_legacy_export_uses_message_id_when_readpst_is_locked(self):
        pst = self.archive / "legacy.pst"
        pst.write_bytes(PST_HEADER)
        with patch.object(self.indexer, "export", side_effect=self.export_fixture):
            source = self.indexer.register(pst)
            row = self.completed(source)
        self.assertEqual(row["state"], "ready")
        self.indexer.original_cache(source, "", row["active_signature"]).rename(
            self.indexer.original_cache(source, "", row["active_signature"]).with_name("archived-test-export"))
        session = mapistore()
        session.find_message.side_effect = lambda _id, temporary: session.read(_id, temporary, ())
        with (patch("mailsearch.indexer.stable_input", side_effect=sharing_violation()),
              patch("mailsearch.indexer.outlook_store", return_value=nullcontext(session)) as opened):
            self.assert_attachment(source)
        self.assertEqual(session.find_message.call_args.args[0], "<atlas@example.invalid>")
        opened.assert_called_once_with("outlook:" + str(pst), open_only=True)
