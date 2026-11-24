"""Background imports with persistent progress and atomic version switches."""
import datetime
import hashlib
import http.client
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from pathlib import Path

from .embedding import logger
from .extract import CREATE_FLAGS, DOCUMENT_SUFFIXES, extract_attachment, extract_document, parse_mail
from .filesystem import source_path, stable_input
from .pcf import WORD_SUFFIXES
from .store import buckets, document_kind, document_identity, now, pack_vector, read_signature, sections, vector_array


NO_MAIL = {"pst_no_mail", "eml_no_mail"}
PST_PROGRESS_INTERVAL = 5
runtime_logger = logging.getLogger("mailsearch.runtime")


def pst_access_error(exc):
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33, 65, 86, 1326}


def document_date(path):
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""


class Paused(Exception):
    pass


class ImportIssue(Exception):
    pass


class Indexer:
    def __init__(self, store, readpst="readpst", pdf="pdftotext", ai=None):
        self.store, self.readpst, self.pdf, self.ai = store, readpst, pdf, ai
        self.lock = threading.RLock()
        self.events = {}
        self.pst_reads = {}
        self.worker = None
        self.stopping = False
        with store.connection() as conn:
            recovered = conn.execute(
                "UPDATE sources SET state='queued',issue=NULL WHERE pending IS NOT NULL "
                "AND state IN ('extracting','headers','attachments','embeddings')"
            ).rowcount
            if recovered:
                runtime_logger.info("Recovered %d interrupted indexing job(s)", recovered)
            # Older versions completed an import even when no message was indexed.
            conn.execute("""
                UPDATE sources SET active=NULL, updated=NULL,
                    state=CASE WHEN failed>0 THEN 'error' ELSE 'empty' END,
                    issue=CASE WHEN failed>0 THEN 'mail_parse_failed'
                               WHEN kind='pst' THEN 'pst_no_mail' ELSE 'eml_no_mail' END
                WHERE state='ready' AND done=0
            """)
        self.spool = store.root / "work"
        self.spool.mkdir(mode=0o700, exist_ok=True)

    def register(self, path):
        if not isinstance(path, (str, os.PathLike)):
            raise ImportIssue("source_path")
        path = str(path).strip()
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if not path:
            raise ImportIssue("source_path")
        try:
            path = source_path(path)
            if path == self.store.root or path.is_relative_to(self.store.root) or self.store.root.is_relative_to(path):
                raise ImportIssue("source_path")
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                with os.scandir(path):
                    pass
                kind = "eml_dir"
            elif stat.S_ISREG(mode) and path.suffix.lower() in {".pst", ".eml"} | DOCUMENT_SUFFIXES:
                with path.open("rb"):
                    pass
                suffix = path.suffix.lower()
                kind = "pst" if suffix == ".pst" else ("eml" if suffix == ".eml" else "doc")
            else:
                raise ImportIssue("source_format")
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror in {32, 33} and Path(path).suffix.casefold() == ".pst":
                raise ImportIssue("pst_in_use") from exc
            if isinstance(exc, PermissionError) or winerror in {65, 86, 1326}:
                raise ImportIssue("source_access") from exc
            raise ImportIssue("source_path") from exc
        except (RuntimeError, ValueError) as exc:
            raise ImportIssue("source_path") from exc
        with self.lock:
            with self.store.connection() as conn:
                cursor = conn.execute("INSERT OR IGNORE INTO sources(path,kind) VALUES(?,?)", (str(path), kind))
                source = conn.execute("SELECT id FROM sources WHERE path=?", (str(path),)).fetchone()[0]
                if cursor.rowcount:
                    conn.execute("UPDATE sources SET pending=revision+1,revision=revision+1 WHERE id=?", (source,))
            self.wake()
        return source

    def command(self, source, action):
        with self.lock:
            row = self.store.source(source)
            if row is None or row["state"] == "removing":
                raise ImportIssue("source_missing")
            if action == "pause":
                event = self.events.get(source)
                if event is not None:
                    event.set()
                with self.store.connection() as conn:
                    conn.execute("UPDATE sources SET state='paused',issue=NULL WHERE id=? AND state<>'ready'", (source,))
            elif action in {"resume", "refresh"}:
                if source in self.events:
                    raise ImportIssue("source_busy")
                if action == "resume" and row["pending"] is None:
                    raise ImportIssue("source_busy")
                with self.store.connection() as conn:
                    if row["state"] == "remove_failed":
                        raise ImportIssue("source_action")
                    if action == "refresh" and row["state"] == "queued":
                        raise ImportIssue("source_busy")
                    deferred_psts = conn.execute(
                        "SELECT 1 FROM problems WHERE source=? AND version=? AND code='pst_in_use' LIMIT 1",
                        (source, row["pending"]),
                    ).fetchone()
                    if action == "refresh" or deferred_psts or row["issue"] in NO_MAIL | {"changed", "mail_parse_failed"}:
                        self.discard_pending(conn, row)
                        conn.execute(
                            """UPDATE sources SET pending=revision+1,revision=revision+1,signature=NULL,
                               done=0,failed=0,omitted=0,partial=0,embedding_done=NULL,embedding_total=NULL
                               WHERE id=?""", (source,)
                        )
                    conn.execute("UPDATE sources SET state='queued',issue=NULL WHERE id=?", (source,))
            elif action == "remove":
                event = self.events.get(source)
                if event is not None:
                    event.set()
                with self.store.connection() as conn:
                    conn.execute("UPDATE sources SET state='removing',active=NULL,pending=NULL,issue=NULL WHERE id=?", (source,))
            else:
                raise ImportIssue("source_action")
            self.wake()

    def discard_pending(self, conn, row):
        if row["pending"] is not None:
            conn.execute("DELETE FROM mails WHERE source=? AND version=?", (row["id"], row["pending"]))
            conn.execute("DELETE FROM problems WHERE source=? AND version=?", (row["id"], row["pending"]))
            shutil.rmtree(self.spool / str(row["id"]) / str(row["pending"]), ignore_errors=True)

    def wake(self):
        with self.lock:
            if not self.stopping and (self.worker is None or not self.worker.is_alive()):
                self.worker = threading.Thread(target=self.run, name="archive-indexer", daemon=True)
                self.worker.start()

    def has_work(self):
        worker = self.worker
        return worker is not None and worker.is_alive()

    def stop(self):
        with self.lock:
            self.stopping = True
            for event in self.events.values():
                event.set()
            worker = self.worker
        if worker is not None:
            worker.join()

    def run(self):
        while True:
            with self.lock:
                if self.stopping:
                    self.worker = None
                    return
                try:
                    with self.store.connection() as conn:
                        task = conn.execute(
                            "SELECT * FROM sources WHERE state IN ('queued','removing') ORDER BY id LIMIT 1"
                        ).fetchone()
                except sqlite3.Error as exc:
                    runtime_logger.error("Cannot read indexing queue (%s, sqlite=%s)",
                                         type(exc).__name__, getattr(exc, "sqlite_errorname", None))
                    self.worker = None
                    return
                if task is None:
                    self.worker = None
                    return
                source = task["id"]
                event = self.events[source] = threading.Event()
            try:
                if task["state"] == "removing":
                    with self.store.connection() as conn:
                        conn.execute("DELETE FROM mails WHERE source=?", (source,))
                        conn.execute("DELETE FROM problems WHERE source=?", (source,))
                    directory = self.spool / str(source)
                    if directory.exists():
                        shutil.rmtree(directory)
                    with self.store.connection() as conn:
                        conn.execute("DELETE FROM sources WHERE id=?", (source,))
                        conn.commit()
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                else:
                    self.process(task, event)
            except Paused:
                # A manual pause already changed the row. During shutdown, retain
                # the current revision and queue it for the next process.
                try:
                    with self.store.connection() as conn:
                        saved = conn.execute(
                            "UPDATE sources SET state='queued',issue=NULL "
                            "WHERE id=? AND state IN ('extracting','headers','attachments','embeddings')",
                            (source,),
                        ).rowcount
                    if saved:
                        runtime_logger.info("Saved index progress at shutdown (source=%d)", source)
                except sqlite3.Error as exc:
                    runtime_logger.error("Cannot save index status (source=%d, sqlite=%s)",
                                         source, getattr(exc, "sqlite_errorname", None))
            except Exception as exc:
                if isinstance(exc, ImportIssue):
                    code = str(exc)
                elif isinstance(exc, (OSError, sqlite3.Error)):
                    code = "disk_or_source"
                else:
                    code = "index_unexpected"
                runtime_logger.error(
                    "Indexing job failed (source=%d, issue=%s, type=%s, errno=%s, winerror=%s, sqlite=%s)",
                    source, code, type(exc).__name__, getattr(exc, "errno", None),
                    getattr(exc, "winerror", None), getattr(exc, "sqlite_errorname", None),
                )
                try:
                    with self.store.connection() as conn:
                        if task["state"] == "removing":
                            conn.execute("UPDATE sources SET state='remove_failed',issue=? WHERE id=?", (code, source))
                        else:
                            state = "empty" if code in NO_MAIL else "error"
                            conn.execute(
                                "UPDATE sources SET state=?,issue=? WHERE id=? AND state NOT IN ('removing','ready','paused')",
                                (state, code, source),
                            )
                except sqlite3.Error as write_error:
                    runtime_logger.error("Cannot record index failure (source=%d, sqlite=%s)",
                                         source, getattr(write_error, "sqlite_errorname", None))
                    # The last committed index remains available even if the disk is full.
                    with self.lock:
                        self.worker = None
                    return
            finally:
                with self.lock:
                    self.events.pop(source, None)
                    self.pst_reads.pop(source, None)

    @staticmethod
    def check(event):
        if event.is_set():
            raise Paused()

    def state(self, source, state, issue=None):
        with self.store.connection() as conn:
            conn.execute("UPDATE sources SET state=?,issue=? WHERE id=? AND state NOT IN ('paused','removing')", (state, issue, source))

    @staticmethod
    def pst_header(path):
        with path.open("rb") as stream:
            header = stream.read(12)
        return len(header) == 12 and header[:4] == b"!BDN" and header[8:10] == b"SM"

    def start_pst_read(self, source, item, position, total):
        started = time.monotonic()
        with self.lock:
            self.pst_reads[source] = {
                "item": item, "position": position, "total": total, "files": 0,
                "started": started, "last_output": started,
            }

    def pst_progress(self, row):
        if row["state"] != "extracting":
            return None
        with self.lock:
            read = self.pst_reads.get(row["id"])
            if read is None:
                return None
            age = time.monotonic()
            return {**{key: read[key] for key in ("item", "position", "total", "files")},
                    "elapsed": int(age - read["started"]), "idle": int(age - read["last_output"])}

    def import_folder_psts(self, folder, stage, event, known, source):
        """Also import PST files kept inside a registered mail folder.

        PSTs whose signature matches the last committed import are carried over
        from that version instead of being re-exported and re-indexed. Empty or
        unreadable PST files are skipped. A locked PST keeps its previous index
        and signature so the next refresh still sees it as pending.
        """
        extras, carried = [], []
        skipped = 0
        signatures = {}
        deferred = []
        files = sorted(path for path in folder.rglob("*")
                       if path.suffix.lower() == ".pst")
        for position, pst in enumerate(files, 1):
            self.check(event)
            item = pst.relative_to(folder).as_posix()
            self.start_pst_read(source, item, position, len(files))
            label = item + "/"
            try:
                if pst.is_symlink() or not stat.S_ISREG(pst.stat().st_mode):
                    continue
                signature = read_signature(pst)
                if known.get(label) == signature:
                    carried.append(label)
                    signatures[label] = signature
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                if not pst_access_error(exc):
                    raise
                deferred.append(item)
                if label in known:
                    carried.append(label)
                    signatures[label] = known[label]
                continue
            slot = stage / ("pst-" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:16])
            slot.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with stable_input(pst):
                    if not self.pst_header(pst):
                        raise ImportIssue("pst_invalid")
                    checkpoint = slot / "pst-signature"
                    if checkpoint.exists():
                        if checkpoint.read_text(encoding="ascii") != signature:
                            raise ImportIssue("changed")
                    else:
                        temporary = slot / "pst-signature.tmp"
                        temporary.write_text(signature, encoding="ascii")
                        temporary.replace(checkpoint)
                    output = self.export(pst, slot, event, source)
                    if read_signature(pst) != signature:
                        raise ImportIssue("changed")
            except OSError as exc:
                if not pst_access_error(exc):
                    raise
                deferred.append(item)
                if label in known:
                    carried.append(label)
                    signatures[label] = known[label]
                continue
            except ImportIssue as exc:
                if str(exc) in {"pst_no_mail", "pst_export", "pst_invalid"}:
                    skipped += 1
                    # Remember the signature so an unchanged unusable PST is not
                    # flagged as an update target on every later refresh.
                    signatures[label] = signature
                    continue
                raise
            extras.append((output, label))
            signatures[label] = signature
        return extras, carried, skipped, signatures, deferred

    def export(self, path, stage, event, source):
        output = stage / "eml"
        if (stage / "export-complete").is_file():
            return output
        binary = shutil.which(self.readpst)
        if not binary:
            raise ImportIssue("readpst_missing")
        shutil.rmtree(output, ignore_errors=True)
        output.mkdir(mode=0o700, exist_ok=True)
        # The child starts in the export directory so readpst need not decode its full path.
        with subprocess.Popen(
            [str(Path(binary).resolve()), "-q", "-j", "0", "-e", "-t", "e", "-o", ".", str(path)],
            cwd=output, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_FLAGS,
        ) as process:
            try:
                next_sample = time.monotonic() + PST_PROGRESS_INTERVAL
                while process.poll() is None:
                    if event.wait(.2):
                        raise Paused()
                    started = time.monotonic()
                    if started >= next_sample:
                        # Count output files by name, without opening mail or rechecking the source share.
                        count = sum(1 for _, _, files in os.walk(output) for name in files
                                    if name.lower().endswith(".eml") or (name.isascii() and name.isdecimal()))
                        sampled = time.monotonic()
                        with self.lock:
                            read = self.pst_reads[source]
                            if count > read["files"]:
                                read.update(files=count, last_output=sampled)
                        next_sample = sampled + max(PST_PROGRESS_INTERVAL, (sampled - started) * 10)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            if process.returncode:
                if os.name == "nt" and (process.returncode & 0xFFFFFFFF) in {0xC0000135, 0xC000007B, 0xC0000139}:
                    raise ImportIssue("readpst_dependencies")
                raise ImportIssue("pst_export")
        self.check(event)
        if next(self.entries(output, numbered=True), None) is None:
            raise ImportIssue("pst_no_mail")
        (stage / "export-complete").touch()
        return output

    @staticmethod
    def entries(root, *, numbered=False):
        if root.is_file():
            yield root, root.name, "/"
            return
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(name for name in dirs if not (Path(current) / name).is_symlink())
            for name in sorted(files):
                path = Path(current) / name
                suffix = path.suffix.lower()
                is_mail = suffix == ".eml" or suffix in DOCUMENT_SUFFIXES or (numbered and name.isascii() and name.isdecimal())
                if is_mail and not path.is_symlink() and path.is_file():
                    relative = path.relative_to(root)
                    yield path, relative.as_posix(), "/" + ("" if relative.parent == Path(".") else relative.parent.as_posix())

    def process(self, row, event):
        source, version, previous = row["id"], row["pending"], row["active"]
        path = Path(row["path"])
        stage = self.spool / str(source) / str(version)
        stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = stage / "temporary"
        temp.mkdir(exist_ok=True, mode=0o700)
        for leftover in temp.glob("pdf-*"):
            shutil.rmtree(leftover, ignore_errors=True)
        signature = read_signature(path)
        if row["signature"] and signature != row["signature"]:
            raise ImportIssue("changed")
        with self.store.connection() as conn:
            conn.execute("UPDATE sources SET signature=?,state='extracting' WHERE id=? AND state='queued'", (signature, source))
        self.check(event)
        is_pst = row["kind"] == "pst"
        extras, carried, signatures, deferred = (), (), None, ()
        if is_pst:
            self.start_pst_read(source, path.name, 1, 1)
            try:
                with stable_input(path):
                    if read_signature(path) != signature:
                        raise ImportIssue("changed")
                    if not self.pst_header(path):
                        raise ImportIssue("pst_invalid")
                    root = self.export(path, stage, event, source)
                    if read_signature(path) != signature:
                        raise ImportIssue("changed")
            except OSError as exc:
                if not pst_access_error(exc):
                    raise
                raise ImportIssue("pst_in_use") from exc
        else:
            root = path
            if path.is_dir():
                extras, carried, skipped_psts, signatures, deferred = self.import_folder_psts(
                    path, stage, event, self.store.pst_files(source), source)
                with self.store.connection() as conn:
                    conn.execute("DELETE FROM problems WHERE source=? AND version=? AND code='pst_in_use'",
                                 (source, version))
                    conn.executemany("INSERT INTO problems VALUES(?,?,?,'pst_in_use')",
                                     ((source, version, item) for item in deferred))
                    # The same folder may be scanned again after a restart. Rebuild
                    # the count from the current PST scan and committed items.
                    omitted_documents = conn.execute(
                        "SELECT COUNT(*) FROM problems WHERE source=? AND version=? "
                        "AND code NOT IN ('parse','pst_in_use')", (source, version),
                    ).fetchone()[0]
                    omitted_attachments = conn.execute(
                        "SELECT COUNT(*) FROM attachments a JOIN mails m ON m.id=a.mail "
                        "WHERE m.source=? AND m.version=? AND m.kind='mail' AND a.status NOT IN ('대기','완료') "
                        "AND NOT EXISTS(SELECT 1 FROM passages p WHERE p.attachment=a.id AND p.location<>'파일명')",
                        (source, version),
                    ).fetchone()[0]
                    omitted = skipped_psts + len(deferred) + omitted_documents + omitted_attachments
                    conn.execute("UPDATE sources SET omitted=? WHERE id=?", (omitted, source))
        self.check(event)
        self.state(source, "headers")
        self.index_headers(root, source, version, event, numbered=is_pst, extras=extras,
                           previous=previous, carried=carried, temp=temp)
        self.check(event)
        progress = self.store.source(source)
        if not progress["done"] and deferred and not progress["failed"]:
            raise ImportIssue("pst_in_use")
        if not progress["done"] and progress["omitted"] and not is_pst:
            with self.store.connection() as conn:
                missing_document = conn.execute(
                    "SELECT 1 FROM problems WHERE source=? AND version=? AND code NOT IN ('parse','pst_in_use') LIMIT 1",
                    (source, version),
                ).fetchone()
            if missing_document:
                raise ImportIssue("document_extract_failed")
        empty_eml = progress["done"] == 0 and not progress["failed"] and not is_pst
        if not progress["done"] and not empty_eml:
            raise ImportIssue("mail_parse_failed" if progress["failed"] else "pst_no_mail")
        self.state(source, "attachments")
        self.index_attachments(root, temp, source, version, event, extras=extras)
        issue = None
        if self.ai is not None and self.ai.embedding_index:
            issue = self.index_embeddings(source, version, event)
        else:
            with self.store.connection() as conn:
                conn.execute("""UPDATE sources SET embedding_done=NULL,embedding_total=NULL
                                WHERE id=? AND pending=?""", (source, version))
        self.check(event)
        if read_signature(path) != signature:
            raise ImportIssue("changed")
        with self.lock:
            self.check(event)
            with self.store.connection() as conn:
                conn.execute(
                    "UPDATE sources SET active=?,pending=NULL,state=?,updated=?,issue=? WHERE id=?",
                    (version, "empty" if empty_eml else "ready", now(), "eml_no_mail" if empty_eml else issue, source),
                )
                conn.execute("DELETE FROM mails WHERE source=? AND version<>?", (source, version))
                conn.execute("DELETE FROM problems WHERE source=? AND version<>?", (source, version))
                if signatures is not None:
                    self.store.save_pst_files(conn, source, signatures)
        shutil.rmtree(stage, ignore_errors=True)

    def pending_updates(self, row):
        """PST files under a registered folder that changed since the last import."""
        if row["kind"] != "eml_dir" or row["state"] not in {"ready", "empty", "error"}:
            return []
        path = Path(row["path"])
        known = self.store.pst_files(row["id"])
        changed = []
        try:
            files = sorted(item for item in path.rglob("*") if item.suffix.lower() == ".pst")
        except OSError:
            return []
        for pst in files:
            item = pst.relative_to(path).as_posix()
            try:
                if not pst.is_symlink() and stat.S_ISREG(pst.stat().st_mode) and known.get(item + "/") != read_signature(pst):
                    changed.append(item)
            except OSError as exc:
                if pst_access_error(exc):
                    changed.append(item)
        return changed

    def scan_entries(self, root, *, numbered, extras=()):
        yield from self.entries(root, numbered=numbered)
        for output, prefix in extras:
            for path, item, folder in self.entries(output, numbered=True):
                yield path, prefix + item, "/" + prefix.rstrip("/") + folder

    @staticmethod
    def locate(root, extras, item):
        if root.is_file():
            return root
        for output, prefix in extras:
            if item.startswith(prefix):
                return output / item[len(prefix):]
        return root / item

    def index_headers(self, root, source, version, event, *, numbered=False, extras=(), previous=None, carried=(), temp=None):
        with self.store.connection() as conn:
            batch = 0
            for prefix in carried:
                self.check(event)
                adopted = self.store.carry_prefix(conn, source, previous, version, prefix)
                if adopted:
                    conn.execute("UPDATE sources SET done=done+? WHERE id=?", (adopted, source))
                    batch += adopted
                    conn.commit()
            workspace = temp or self.spool
            for path, item, folder in self.scan_entries(root, numbered=numbered, extras=extras):
                self.check(event)
                if conn.execute("SELECT 1 FROM mails WHERE source=? AND version=? AND item=?", (source, version, item)).fetchone():
                    continue
                if conn.execute("SELECT 1 FROM problems WHERE source=? AND version=? AND item=?", (source, version, item)).fetchone():
                    continue
                if path.suffix.lower() in DOCUMENT_SUFFIXES:
                    structured = {} if (path.suffix.lower() in WORD_SUFFIXES and document_kind(path.name) == "pcf") else None
                    try:
                        status, locations = extract_document(
                            path, workspace, self.pdf, check=lambda: self.check(event), structure=structured)
                    except OSError:
                        status, locations = "추출 실패 또는 암호화", []
                    if not locations and not (structured and structured.get("comments")):
                        conn.execute("INSERT INTO problems VALUES(?,?,?,?)", (source, version, item, status))
                        conn.execute("UPDATE sources SET omitted=omitted+1 WHERE id=?", (source,))
                    else:
                        doc = {"name": path.name, "folder": folder, "sent": document_date(path),
                               "status": status, "sections": locations, "pcf": structured}
                        adopted = previous is not None and self.store.try_adopt(
                            conn, source, previous, version, item, document_identity(doc))
                        if adopted:
                            conn.execute("""UPDATE attachments SET status=? WHERE mail IN (
                                SELECT id FROM mails WHERE source=? AND version=? AND item=?)""",
                                         (status, source, version, item))
                        else:
                            self.store.add_document(conn, source, version, item, doc)
                        conn.execute("UPDATE sources SET done=done+1,partial=partial+? WHERE id=?",
                                     (int(status.startswith("일부 색인")), source))
                    batch += 1
                    if batch % 32 == 0:
                        conn.commit()
                    continue
                try:
                    mail = parse_mail(path, folder)
                except (ValueError, OSError, LookupError, TypeError, RecursionError):
                    conn.execute("INSERT INTO problems VALUES(?,?,?,'parse')", (source, version, item))
                    conn.execute("UPDATE sources SET failed=failed+1 WHERE id=?", (source,))
                else:
                    if previous is None or not self.store.try_adopt(conn, source, previous, version, item, mail):
                        self.store.add_mail(conn, source, version, item, mail)
                    conn.execute("UPDATE sources SET done=done+1 WHERE id=?", (source,))
                batch += 1
                if batch % 32 == 0:
                    conn.commit()

    def index_attachments(self, root, temp, source, version, event, extras=()):
        with self.store.connection() as conn:
            last = 0
            extracted = []

            def save_extracted():
                with conn:
                    for attachment, mail, status, locations in extracted:
                        for location, text in locations:
                            for start, segment in sections(text):
                                self.store.add_passage(conn, attachment["mail"], mail, attachment["id"],
                                                       attachment["name"], location, start, segment)
                        conn.execute("UPDATE attachments SET status=? WHERE id=?", (status, attachment["id"]))
                    omitted = sum(not locations for _, _, _, locations in extracted)
                    partial = sum(bool(locations) and status.startswith("일부 색인")
                                  for _, _, status, locations in extracted)
                    conn.execute("UPDATE sources SET omitted=omitted+?,partial=partial+? WHERE id=?",
                                 (omitted, partial, source))
                extracted.clear()

            while True:
                self.check(event)
                batch = conn.execute(
                    """SELECT m.item,m.folder,m.id FROM mails m WHERE m.source=? AND m.version=? AND m.id>?
                       AND EXISTS(SELECT 1 FROM attachments a WHERE a.mail=m.id AND a.status='대기')
                       ORDER BY m.id LIMIT 128""", (source, version, last),
                ).fetchall()
                if not batch:
                    break
                for row in batch:
                    self.check(event)
                    path = self.locate(root, extras, row["item"])
                    attachments = conn.execute(
                        "SELECT id,mail,name,status FROM attachments WHERE mail=? ORDER BY id", (row["id"],),
                    ).fetchall()
                    try:
                        mail = parse_mail(path, row["folder"])
                    except (ValueError, OSError, LookupError, TypeError, RecursionError):
                        mail = None
                    if mail and [a["name"] for a in attachments] != [name for name, _ in mail["attachments"]]:
                        raise ImportIssue("changed")
                    for position, attachment in enumerate(attachments):
                        if attachment["status"] != "대기":
                            continue
                        self.check(event)
                        if mail is None:
                            status, locations = "추출 실패", []
                        else:
                            name, part = mail["attachments"][position]
                            status, locations = extract_attachment(name, part, temp, self.pdf, check=lambda: self.check(event))
                        extracted.append((attachment, mail, status, locations))
                        if len(extracted) == 24:
                            save_extracted()
                last = batch[-1]["id"]
            if extracted:
                save_extracted()

    def index_embeddings(self, source, version, event):
        model = self.ai.embedding_index
        self.check(event)
        with self.store.connection() as conn:
            total, done = conn.execute(
                """SELECT COUNT(*),COUNT(v.passage) FROM mails m JOIN passages p ON p.mail=m.id
                   LEFT JOIN vectors v ON v.passage=p.id AND v.model=?
                   WHERE m.source=? AND m.version=?""", (model, source, version),
            ).fetchone()
            conn.execute("""UPDATE sources SET state='embeddings',embedding_done=?,embedding_total=?
                            WHERE id=? AND pending=? AND state NOT IN ('paused','removing')""",
                         (done, total, source, version))
        last = 0
        while True:
            self.check(event)
            with self.store.connection() as conn:
                rows = conn.execute(
                    """SELECT p.id,p.text,m.subject,a.name FROM passages p JOIN mails m ON m.id=p.mail
                       LEFT JOIN attachments a ON a.id=p.attachment WHERE m.source=? AND m.version=? AND p.id>?
                       AND NOT EXISTS(SELECT 1 FROM vectors v WHERE v.passage=p.id AND v.model=?)
                       ORDER BY p.id LIMIT 24""", (source, version, last, model)
                ).fetchall()
            if not rows:
                return None
            try:
                encoded = [pack_vector(v) for v in self.ai.embed(
                    [f"{r['subject']}\n{r['name'] or ''}\n{r['text']}" for r in rows], timeout=45
                )]
                if len(encoded) != len(rows):
                    raise ValueError("embeddings")
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, http.client.HTTPException) as exc:
                winerror = getattr(exc, "winerror", None)
                backend = "gateway" if self.ai.embedding_api and self.ai.embedding_api.index_key else "Ollama"
                logger.error("Embedding batch failed (backend=%s, completed=%d/%d): %s (errno=%s, winerror=%s)",
                             backend, done, total, type(exc).__name__, getattr(exc, "errno", None), winerror)
                if isinstance(exc, ConnectionAbortedError) or winerror == 10053:
                    return "embedding_connection_aborted"
                return "embedding_unavailable"
            self.check(event)
            with self.store.connection() as conn:
                for row, value in zip(rows, encoded):
                    passage = row["id"]
                    conn.execute("INSERT INTO vectors VALUES(?,?,?)", (passage, model, value))
                    conn.executemany(
                        "INSERT INTO vector_buckets VALUES(?,?,?)",
                        ((model, bucket, passage) for bucket in buckets(vector_array(value))),
                    )
                done += len(rows)
                conn.execute("UPDATE sources SET embedding_done=? WHERE id=? AND pending=?",
                             (done, source, version))
            last = rows[-1]["id"]
