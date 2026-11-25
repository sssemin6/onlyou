"""Optional, read-only Outlook Object Model access on Windows."""
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import os

from .extract import MAX_ATTACHMENT, clean

DEFAULT_OUTLOOK = "outlook:default"
MAIL_FILTER = '@SQL="http://schemas.microsoft.com/mapi/proptag/0x001A001F" LIKE \'IPM.Note%\''


class OutlookError(Exception):
    pass


def outlook_modules():
    if os.name != "nt":
        raise OutlookError("outlook_windows")
    try:
        import pythoncom
        from win32com import client
    except ImportError as exc:
        raise OutlookError("outlook_pywin32") from exc
    return pythoncom, client


@contextmanager
def outlook_store(path):
    pythoncom, client = outlook_modules()
    pythoncom.CoInitialize()
    namespace = attached = store = root = session = None
    try:
        try:
            namespace = client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            if path == DEFAULT_OUTLOOK:
                store = namespace.DefaultStore
            else:
                path = path.removeprefix("outlook:")
                if not Path(path).is_file() or Path(path) != Path(path).resolve(strict=True):
                    raise OutlookError("outlook_pst_path")
                target = os.path.normcase(os.path.normpath(path))
                store = next((entry for entry in namespace.Stores
                              if entry.FilePath and os.path.normcase(os.path.normpath(entry.FilePath)) == target), None)
                if store is None:
                    namespace.AddStoreEx(path, 3)  # olStoreUnicode
                    store = next(entry for entry in namespace.Stores
                                 if entry.FilePath and os.path.normcase(os.path.normpath(entry.FilePath)) == target)
                    attached = store.GetRootFolder()
            root = attached if attached is not None else store.GetRootFolder()
            session = OutlookStore(namespace, store.StoreID, root, pythoncom.com_error)
        except (OSError, AttributeError, StopIteration, pythoncom.com_error) as exc:
            raise OutlookError("outlook_unavailable") from exc
        yield session
    finally:
        if attached is not None:
            try:
                namespace.RemoveStore(attached)
            except pythoncom.com_error:
                pass
        if session is not None:
            session.namespace = session.root = None
        session = root = store = attached = namespace = None
        pythoncom.CoUninitialize()


class OutlookStore:
    def __init__(self, namespace, store_id, root, com_error):
        self.namespace = namespace
        self.store_id = store_id
        self.root = root
        self.com_error = com_error

    def identities(self, check):
        folders = [self.root]
        try:
            while folders:
                check()
                folder = folders.pop()
                folders.extend(folder.Folders)
                path = folder.FolderPath
                table = folder.GetTable(MAIL_FILTER)
                columns = table.Columns
                columns.RemoveAll()
                columns.Add("EntryID")
                while not table.EndOfTable:
                    check()
                    for (entry_id,) in table.GetArray(256):
                        if not entry_id:
                            raise OutlookError("outlook_enumeration")
                        yield entry_id, path
        except self.com_error as exc:
            raise OutlookError("outlook_enumeration") from exc

    def read(self, entry_id, folder, temporary, aliases=()):
        try:
            item = self.namespace.GetItemFromID(entry_id, self.store_id)
            actual = item.Parent.FolderPath
            if folder != actual and actual not in aliases:
                raise OutlookError("outlook_item_changed")
            sent = item.SentOn
            sent = sent.astimezone(timezone.utc).isoformat(timespec="seconds") if sent else ""
            attachments = [(clean(part.FileName or "이름 없는 첨부")[:512], OutlookAttachment(part, temporary, self.com_error))
                           for part in item.Attachments]
            return {
                "folder": actual, "subject": clean(item.Subject or ""), "sender": clean(item.SenderName or ""),
                "recipient": "\n".join(filter(None, (clean(item.To or ""), clean(item.CC or "")))),
                "sent": sent, "message_id": self.property(item, "1035"),
                "reply_ids": " ".join(self.property(item, code) for code in ("1039", "1042")),
                "body": clean(item.Body or ""),
                "attachments": attachments,
            }
        except self.com_error as exc:
            raise OutlookError("outlook_item_changed") from exc

    def property(self, item, code):
        try:
            return str(item.PropertyAccessor.GetProperty(
                "http://schemas.microsoft.com/mapi/proptag/0x" + code + "001F") or "").strip().lower()
        except (AttributeError, self.com_error):
            return ""


class OutlookAttachment:
    def __init__(self, attachment, temporary, com_error):
        self.attachment = attachment
        self.temporary = temporary
        self.com_error = com_error

    def get_payload(self, *, decode):
        try:
            if self.attachment.Size > MAX_ATTACHMENT:
                raise OutlookError("outlook_attachment_size")
            with TemporaryDirectory(prefix="outlook-", dir=self.temporary) as folder:
                target = Path(folder) / "attachment.bin"
                self.attachment.SaveAsFile(str(target))
                with target.open("rb") as file:
                    return file.read(MAX_ATTACHMENT + 1)
        except self.com_error as exc:
            raise OutlookError("outlook_attachment") from exc
