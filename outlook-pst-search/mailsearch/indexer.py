"""Background imports with persistent progress and atomic version switches."""
import datetime
import hashlib
import http.client
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .embedding import GatewayRequestError, logger
from .extract import CREATE_FLAGS, DOCUMENT_SUFFIXES, MAX_ATTACHMENT, extract_attachment, extract_document, parse_mail
from .filesystem import local_path, source_path, stable_input
from .outlook import DEFAULT_OUTLOOK, OutlookError, outlook_modules, outlook_store
from .pcf import WORD_SUFFIXES
from .store import (MAIL_INDEX_FORMAT, buckets, document_kind, document_identity, matches_fingerprint, now,
                    pack_vector, read_signature, sections, vector_array)


NO_MAIL = {"pst_no_mail", "eml_no_mail"}
PST_DEFERRED = ("pst_in_use", "pst_changed")
PST_PROGRESS_INTERVAL = 5
EMBEDDING_BATCH_SIZE = 24
EMBEDDING_BATCH_TIMEOUT = 60
RETIRED_MAIL_BATCH_SIZE = 64
runtime_logger = logging.getLogger("mailsearch.runtime")


def pst_access_error(exc):
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33, 65, 86, 1326}


def folder_identity(signature):
    # Directory size and mtime change when unrelated entries are added or removed.
    return json.dumps(json.loads(signature)[:2])


def source_signature(path, kind):
    signature = read_signature(path)
    if kind == "eml_dir":
        if not path.is_dir():
            raise ImportIssue("changed")
        return folder_identity(signature)
    return signature


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
    def __init__(self, store, readpst="readpst", pdf="pdftotext", ai=None, *,
                 embedding_batch_size=EMBEDDING_BATCH_SIZE, embedding_batch_timeout=EMBEDDING_BATCH_TIMEOUT):
        self.store, self.readpst, self.pdf, self.ai = store, readpst, pdf, ai
        if not 24 <= embedding_batch_size <= 64 or not 30 <= embedding_batch_timeout <= 60:
            raise ValueError("embedding_batch_settings")
        self.embedding_batch_size = embedding_batch_size
        self.embedding_batch_timeout = embedding_batch_timeout
        self.lock = threading.RLock()
        self.events = {}
        self.pst_reads = {}
        self.finalizations = {}
        self.finalization_lock = threading.Lock()
        self.original_thread = None
        self.original_event = threading.Event()
        self.original_failure = None
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

    def register(self, path, *, outlook=False):
        if outlook:
            try:
                outlook_modules()
                if not isinstance(path, str):
                    raise ImportIssue("source_path")
                value = path.strip().strip('"')
                if not value:
                    return self.save_source(DEFAULT_OUTLOOK, "outlook_mapi")
                local = local_path(value, strict=True)
                if not local.is_file() or local.suffix.casefold() != ".pst":
                    raise ImportIssue("outlook_pst_path")
                return self.save_source("outlook:" + str(local), "outlook_mapi")
            except OutlookError as exc:
                raise ImportIssue(str(exc)) from exc
            except (OSError, ValueError, RuntimeError) as exc:
                raise ImportIssue("outlook_pst_path") from exc
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
        return self.save_source(str(path), kind)

    def save_source(self, path, kind):
        with self.lock:
            with self.store.connection() as conn:
                cursor = conn.execute("INSERT OR IGNORE INTO sources(path,kind) VALUES(?,?)", (str(path), kind))
                source = conn.execute("SELECT id FROM sources WHERE path=?", (str(path),)).fetchone()[0]
                if cursor.rowcount:
                    conn.execute("UPDATE sources SET pending=revision+1,revision=revision+1 WHERE id=?", (source,))
            if cursor.rowcount:
                self.store.reload_memory(source)
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
                    issue = row["issue"]
                    restart = issue in NO_MAIL | {"mail_parse_failed", "pst_in_use", "pst_changed"}
                    if action == "refresh" or restart or (issue == "changed" and row["kind"] != "eml_dir"):
                        self.discard_pending(conn, row)
                        conn.execute(
                            """UPDATE sources SET pending=revision+1,revision=revision+1,signature=NULL,
                               done=0,failed=0,omitted=0,partial=0,embedding_done=NULL,embedding_total=NULL,embedding_model_key=NULL,
                               index_stage=NULL,index_done=NULL,index_total=NULL
                               WHERE id=?""", (source,)
                        )
                    conn.execute("UPDATE sources SET state='queued',issue=NULL WHERE id=?", (source,))
                if row["active"] is None:
                    self.store.reload_memory(source)
            elif action == "remove":
                event = self.events.get(source)
                if event is not None:
                    event.set()
                with self.store.connection() as conn:
                    conn.execute("UPDATE sources SET state='removing',active=NULL,pending=NULL,issue=NULL WHERE id=?", (source,))
                self.store.reload_memory(source)
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
        return any(worker and worker.is_alive() for worker in (self.worker, self.original_thread))

    def stop(self):
        with self.lock:
            self.stopping = True
            for event in self.events.values():
                event.set()
            self.original_event.set()
            workers = (self.worker, self.original_thread)
        for worker in workers:
            if worker is not None:
                worker.join()

    def run(self):
        cleanup_failed = False
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
                        retired = None
                        if task is None and not cleanup_failed:
                            retired = conn.execute(
                                """SELECT s.id FROM sources s WHERE EXISTS (
                                       SELECT 1 FROM mails m WHERE m.source=s.id AND m.version<s.active)
                                   ORDER BY s.id LIMIT 1"""
                            ).fetchone()
                except sqlite3.Error as exc:
                    runtime_logger.error("Cannot read indexing queue (%s, sqlite=%s)",
                                         type(exc).__name__, getattr(exc, "sqlite_errorname", None))
                    self.worker = None
                    return
                if task is None and retired is None:
                    self.worker = None
                    return
                if task is not None:
                    source = task["id"]
                    event = self.events[source] = threading.Event()
            if task is None:
                try:
                    self.cleanup_retired(retired["id"])
                except sqlite3.Error as exc:
                    cleanup_failed = True
                    if not self.stopping:
                        runtime_logger.warning("Retired index cleanup deferred (source=%d, sqlite=%s)",
                                               retired["id"], getattr(exc, "sqlite_errorname", None))
                continue
            try:
                if task["state"] == "removing":
                    with self.lock:
                        original = self.original_thread
                        if original and original.is_alive():
                            self.original_event.set()
                    if original:
                        original.join()
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
                with self.finalization_lock:
                    self.finalizations.pop(source, None)
                with self.lock:
                    self.events.pop(source, None)
                    self.pst_reads.pop(source, None)

    def cleanup_retired(self, source):
        # Recheck the active version in the delete so active and pending rows
        # remain intact even when a source action arrives between batches.
        with self.store.connection() as conn:
            conn.set_progress_handler(lambda: self.stopping, 10000)
            try:
                conn.execute(
                    """DELETE FROM mails WHERE id IN (
                           SELECT id FROM mails WHERE source=? AND version<(SELECT active FROM sources WHERE id=?)
                           ORDER BY version,id LIMIT ?)""",
                    (source, source, RETIRED_MAIL_BATCH_SIZE),
                )
            finally:
                conn.set_progress_handler(None, 0)

    @staticmethod
    def check(event):
        if event.is_set():
            raise Paused()

    def state(self, source, state, issue=None):
        with self.store.connection() as conn:
            conn.execute("""UPDATE sources SET state=?,issue=?,index_stage=?,index_done=NULL,index_total=NULL
                            WHERE id=? AND state NOT IN ('paused','removing')""", (state, issue, state, source))

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

    def import_folder_psts(self, folder, stage, event, known, source, already_deferred, outdated):
        """Also import PST files kept inside a registered mail folder.

        PSTs whose signature matches the last committed import are carried over
        from that version instead of being re-exported and re-indexed. Empty or
        unreadable PST files are skipped. A locked PST keeps its previous index
        and signature so the next refresh still sees it as pending.
        """
        extras, carried = [], []
        skipped = {}
        signatures = {}
        deferred = dict(already_deferred)
        files = sorted(path for path in folder.rglob("*")
                       if path.suffix.lower() == ".pst")
        for position, pst in enumerate(files, 1):
            self.check(event)
            item = pst.relative_to(folder).as_posix()
            self.start_pst_read(source, item, position, len(files))
            label = item + "/"
            slot = stage / ("pst-" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:16])
            checkpoint = slot / "pst-signature"
            if item in deferred:
                if label in known:
                    carried.append(label)
                    signatures[label] = known[label]
                continue
            try:
                if pst.is_symlink() or not stat.S_ISREG(pst.stat().st_mode):
                    continue
                signature = read_signature(pst)
                previous_signature = checkpoint.read_text(encoding="ascii") if checkpoint.is_file() else None
                if previous_signature and previous_signature != signature:
                    cached = self.original_cache(source, label, previous_signature)
                    if not cached.is_dir() and (slot / "export-complete").is_file():
                        cached = slot / "eml"
                    if cached.is_dir():
                        extras.append((cached, label))
                        signatures[label] = previous_signature
                        continue
                    deferred[item] = "pst_changed"
                    if label in known:
                        carried.append(label)
                        signatures[label] = known[label]
                    continue
                if known.get(label) == signature and label not in outdated:
                    carried.append(label)
                    signatures[label] = signature
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                if not pst_access_error(exc):
                    raise
                deferred[item] = "pst_in_use"
                if label in known:
                    carried.append(label)
                    signatures[label] = known[label]
                continue
            slot.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with stable_input(pst):
                    if not self.pst_header(pst):
                        raise ImportIssue("pst_invalid")
                    if not checkpoint.exists():
                        temporary = slot / "pst-signature.tmp"
                        temporary.write_text(signature, encoding="ascii")
                        temporary.replace(checkpoint)
                    cached = self.original_cache(source, label, signature)
                    output = (cached if label in outdated and cached.is_dir() else
                              self.cached_export(pst, slot, event, source, label, signature))
                    if read_signature(pst) != signature:
                        raise ImportIssue("changed")
            except OSError as exc:
                if not pst_access_error(exc):
                    raise
                deferred[item] = "pst_in_use"
                if label in known:
                    carried.append(label)
                    signatures[label] = known[label]
                continue
            except ImportIssue as exc:
                if str(exc) == "changed":
                    deferred[item] = "pst_changed"
                    if label in known:
                        carried.append(label)
                        signatures[label] = known[label]
                    continue
                if str(exc) in {"pst_no_mail", "pst_export", "pst_invalid"}:
                    skipped[item] = str(exc)
                    if label in outdated and known.get(label) == signature:
                        # A failed migration must not drop results from an unchanged PST.
                        carried.append(label)
                    # Remember the signature so an unchanged unusable PST is not
                    # flagged as an update target on every later refresh.
                    signatures[label] = signature
                    continue
                raise
            extras.append((output, label))
            signatures[label] = signature
        return extras, carried, skipped, signatures, deferred

    def original_cache(self, source, label, signature):
        key = hashlib.sha256((label + "\0" + signature).encode()).hexdigest()
        return self.spool / str(source) / "originals" / key / "eml"

    def cached_export(self, path, stage, event, source, label, signature, *, progress=True):
        cached = self.original_cache(source, label, signature)
        completed = stage / "cached-export"
        if cached.is_dir() and (not progress or completed.is_file()):
            return cached
        output = self.export(path, stage, event, source if progress else None)
        if read_signature(path) != signature:
            raise ImportIssue("changed")
        if output != stage / "eml" or not (stage / "export-complete").is_file():
            return output
        with self.lock:
            if not cached.is_dir():
                cached.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                output.replace(cached)
            completed.touch()
            (stage / "export-complete").unlink(missing_ok=True)
        return cached

    def contained_file(self, root, item):
        root = root.resolve(strict=True)
        file = (root / item).resolve(strict=True)
        if file == root or not file.is_relative_to(root) or not file.is_file():
            raise ImportIssue("attachment_missing")
        return file

    def attachment_bytes(self, row):
        if row["kind"] == "outlook_mapi":
            return self.outlook_attachment_bytes(row)
        source, version, item = row["source"], row["version"], row["item"]
        root = Path(row["path"])
        if root.resolve(strict=True) != root:
            raise ImportIssue("attachment_missing")
        stage = self.spool / str(source) / str(version)
        parts = Path(item).parts
        split = next((i + 1 for i, part in enumerate(parts[:-1]) if part.lower().endswith(".pst")), 0)
        if row["kind"] == "pst" or row["kind"] == "eml_dir" and split:
            if row["kind"] == "pst":
                pst, label, eml = root, "", item
                signature = row["active_signature"] if version == row["active"] else row["signature"]
            else:
                label = Path(*parts[:split]).as_posix() + "/"
                pst = self.contained_file(root, label)
                eml = Path(*parts[split:]).as_posix()
                stage = stage / ("pst-" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:16])
                snapshot = stage / "pst-signature"
                signature = snapshot.read_text(encoding="ascii") if snapshot.is_file() else self.store.pst_files(source).get(label)
                if not signature and (stage / "export-complete").is_file():
                    signature = read_signature(pst)
            if not signature:
                raise ImportIssue("attachment_missing")
            with pst.open("rb"):
                pass
            cached = self.original_cache(source, label, signature)
            if not cached.is_dir():
                if (stage / "export-complete").is_file() and (stage / "eml").is_dir():
                    cached = stage / "eml"
                else:
                    if read_signature(pst) != signature:
                        raise ImportIssue("attachment_changed")
                    self.prepare_original(pst, source, label, signature)
                    return None
            path = self.contained_file(cached, eml)
        elif row["kind"] == "eml_dir":
            path = self.contained_file(root, item)
        else:
            path = root

        mail = parse_mail(path, row["folder"])
        position = row["position"]
        if not matches_fingerprint(mail, item, row["fingerprint"]) or position >= len(mail["attachments"]):
            raise ImportIssue("attachment_changed")
        name, part = mail["attachments"][position]
        if name != row["name"]:
            raise ImportIssue("attachment_changed")
        payload = part.get_payload(decode=True)
        if payload is None and part.get_content_type() == "message/rfc822":
            messages = part.get_payload()
            payload = messages[0].as_bytes() if isinstance(messages, list) and messages else None
        if payload is None:
            raise ImportIssue("attachment_missing")
        return payload

    def outlook_attachment_bytes(self, row):
        try:
            with tempfile.TemporaryDirectory(dir=self.spool, prefix="outlook-") as folder, outlook_store(row["path"]) as outlook:
                signature = row["active_signature"] if row["version"] == row["active"] else row["signature"]
                if signature != outlook.store_id:
                    raise ImportIssue("attachment_changed")
                mail = outlook.read(row["item"], row["folder"], folder)
                position = row["position"]
                if not matches_fingerprint(mail, row["item"], row["fingerprint"]) or position >= len(mail["attachments"]):
                    raise ImportIssue("attachment_changed")
                name, part = mail["attachments"][position]
                if name != row["name"]:
                    raise ImportIssue("attachment_changed")
                content = part.get_payload(decode=True)
                if not content or len(content) > MAX_ATTACHMENT:
                    raise ImportIssue("outlook_attachment_size")
                return content
        except OutlookError as exc:
            raise ImportIssue(str(exc)) from exc

    def prepare_original(self, path, source, label, signature):
        key = (source, label, signature)
        with self.lock:
            row = self.store.source(source)
            if self.stopping or row is None or row["state"] == "removing":
                raise ImportIssue("attachment_missing")
            if self.original_failure and self.original_failure[0] == key:
                _, issue = self.original_failure
                self.original_failure = None
                raise ImportIssue(issue)
            if self.original_thread and self.original_thread.is_alive():
                return
            self.original_event.clear()

            def prepare():
                try:
                    with (stable_input(path), tempfile.TemporaryDirectory(dir=self.spool, prefix="original-") as scratch):
                        if read_signature(path) != signature:
                            raise ImportIssue("attachment_changed")
                        self.cached_export(path, Path(scratch), self.original_event, source, label, signature, progress=False)
                except Paused:
                    pass
                except (OSError, ImportIssue, ValueError) as exc:
                    issue = str(exc) if isinstance(exc, ImportIssue) else "attachment_missing"
                    with self.lock:
                        self.original_failure = (key, issue)

            self.original_thread = threading.Thread(target=prepare, name="pst-original", daemon=True)
            self.original_thread.start()

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
                    if source is not None and started >= next_sample:
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
        self.state(source, "extracting")
        self.check(event)
        path = Path(row["path"])
        stage = self.spool / str(source) / str(version)
        stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = stage / "temporary"
        temp.mkdir(exist_ok=True, mode=0o700)
        for leftover in temp.glob("pdf-*"):
            shutil.rmtree(leftover, ignore_errors=True)
        kind = row["kind"]
        if kind == "outlook_mapi":
            try:
                with self.store.thread_connection():
                    self.process_outlook(row, temp, event)
            except OutlookError as exc:
                raise ImportIssue(str(exc)) from exc
            shutil.rmtree(stage, ignore_errors=True)
            return
        signature = source_signature(path, kind)
        saved = row["signature"]
        if saved and kind == "eml_dir":
            saved = folder_identity(saved)
        if saved and signature != saved:
            raise ImportIssue("changed")
        with self.store.connection() as conn:
            conn.execute("UPDATE sources SET signature=? WHERE id=? AND state='extracting'", (signature, source))
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
                    outdated = self.store.outdated_pst_labels(source, previous, ("",))
                    cached = self.original_cache(source, "", signature)
                    if previous is not None and row["active_signature"] == signature and not outdated:
                        carried = ("",)
                        root = cached if cached.is_dir() else stage
                    else:
                        root = cached if outdated and cached.is_dir() else self.cached_export(path, stage, event, source, "", signature)
                    if read_signature(path) != signature:
                        raise ImportIssue("changed")
            except OSError as exc:
                if not pst_access_error(exc):
                    raise
                raise ImportIssue("pst_in_use") from exc
        else:
            root = path
            if path.is_dir():
                already_deferred = {}
                if row["done"]:
                    with self.store.connection() as conn:
                        already_deferred = dict(conn.execute(
                            "SELECT item,code FROM problems WHERE source=? AND version=? AND code IN (?,?)",
                            (source, version, *PST_DEFERRED),
                        ).fetchall())
                known = self.store.pst_files(source)
                outdated = self.store.outdated_pst_labels(source, previous, known)
                extras, carried, skipped_psts, signatures, deferred = self.import_folder_psts(
                    path, stage, event, known, source, already_deferred, outdated)
                with self.store.connection() as conn:
                    conn.execute("""DELETE FROM problems WHERE source=? AND version=?
                                    AND code IN ('pst_in_use','pst_changed','pst_no_mail','pst_export','pst_invalid')""",
                                 (source, version))
                    conn.executemany("INSERT INTO problems VALUES(?,?,?,?)",
                                     ((source, version, item, code) for item, code in (deferred | skipped_psts).items()))
                    # The same folder may be scanned again after a restart. Rebuild
                    # the count from the current PST scan and committed items.
                    omitted_documents = conn.execute(
                        "SELECT COUNT(*) FROM problems WHERE source=? AND version=? "
                        "AND code NOT IN ('parse','pst_in_use','pst_changed','pst_no_mail','pst_export',"
                        "'pst_invalid','mail_original_changed')", (source, version),
                    ).fetchone()[0]
                    omitted_attachments = conn.execute(
                        "SELECT COUNT(*) FROM attachments a JOIN mails m ON m.id=a.mail "
                        "WHERE m.source=? AND m.version=? AND m.kind='mail' AND a.status NOT IN ('대기','완료') "
                        "AND NOT EXISTS(SELECT 1 FROM passages p WHERE p.attachment=a.id AND p.location<>'파일명')",
                        (source, version),
                    ).fetchone()[0]
                    omitted = len(skipped_psts) + len(deferred) + omitted_documents + omitted_attachments
                    conn.execute("UPDATE sources SET omitted=? WHERE id=?", (omitted, source))
        self.check(event)
        self.state(source, "headers")
        self.index_headers(root, source, version, event, numbered=is_pst, extras=extras,
                           previous=previous, carried=carried, temp=temp)
        self.check(event)
        progress = self.store.source(source)
        if not progress["done"] and deferred and not progress["failed"]:
            raise ImportIssue("pst_in_use" if "pst_in_use" in deferred.values() else "pst_changed")
        if not progress["done"] and progress["omitted"] and not is_pst:
            with self.store.connection() as conn:
                missing_document = conn.execute(
                    """SELECT 1 FROM problems WHERE source=? AND version=? AND code NOT IN
                       ('parse','pst_in_use','pst_changed','pst_no_mail','pst_export','pst_invalid','mail_original_changed')
                       LIMIT 1""",
                    (source, version),
                ).fetchone()
            if missing_document:
                raise ImportIssue("document_extract_failed")
        empty_eml = progress["done"] == 0 and not progress["failed"] and not is_pst
        if not progress["done"] and not empty_eml:
            raise ImportIssue("mail_parse_failed" if progress["failed"] else "pst_no_mail")
        self.state(source, "attachments")
        self.index_attachments(root, temp, source, version, event, extras=extras)
        issue = self.embed_revision(source, version, event)
        self.start_finalization(source, version, event, "verify_source", issue)
        if source_signature(path, kind) != signature:
            raise ImportIssue("changed")
        self.publish_revision(source, version, signature, event, issue, empty_eml=empty_eml, signatures=signatures)
        shutil.rmtree(stage, ignore_errors=True)
        if signatures is not None or is_pst:
            retained = signatures if signatures is not None else {"": signature}
            keep = {self.original_cache(source, label, value) for label, value in retained.items()}
            cached = self.spool / str(source) / "originals"
            for folder in cached.iterdir() if cached.is_dir() else ():
                if folder / "eml" not in keep:
                    shutil.rmtree(folder, ignore_errors=True)

    def embed_revision(self, source, version, event, *, retain=False):
        if self.ai is not None and self.ai.embedding_index:
            if retain:
                return self.index_embeddings(source, version, event, retain=True)
            return self.index_embeddings(source, version, event)
        with self.store.connection() as conn:
            conn.execute("""UPDATE sources SET embedding_done=NULL,embedding_total=NULL,embedding_model_key=NULL
                            WHERE id=? AND pending=?""", (source, version))
        return None

    def start_finalization(self, source, version, event, phase, issue):
        self.check(event)
        started = time.monotonic()
        with self.finalization_lock:
            self.finalizations[source] = {
                "version": version, "phase": phase, "done": None, "total": None,
                "started": started, "phase_started": started, "updated": started,
            }
        with self.store.connection() as conn:
            conn.execute("""UPDATE sources SET index_stage='finalizing',issue=? WHERE id=? AND pending=?
                            AND state NOT IN ('paused','removing')""", (issue, source, version))

    def update_finalization(self, source, phase, done=None, total=None):
        timestamp = time.monotonic()
        with self.finalization_lock:
            progress = self.finalizations[source]
            if (phase, done, total) != (progress["phase"], progress["done"], progress["total"]):
                if phase != progress["phase"]:
                    progress["phase_started"] = timestamp
                progress.update(phase=phase, done=done, total=total, updated=timestamp)

    def finalization_progress(self, row):
        if row["index_stage"] != "finalizing" or row["state"] not in ("attachments", "embeddings", "headers"):
            return None
        with self.finalization_lock:
            progress = self.finalizations.get(row["id"])
            if not progress or progress["version"] != row["pending"]:
                return None
            timestamp = time.monotonic()
            return {**{key: progress[key] for key in ("phase", "done", "total")},
                    "elapsed": int(timestamp - progress["started"]),
                    "phase_elapsed": int(timestamp - progress["phase_started"]),
                    "idle": int(timestamp - progress["updated"])}

    def publish_revision(self, source, version, signature, event, issue, *, empty_eml=False, signatures=None,
                         retain=False, unchanged=None):
        def report(phase, done=None, total=None):
            self.check(event)
            self.update_finalization(source, phase, done, total)

        active = version if unchanged is None else unchanged
        report("prepare_switch")
        with self.store.connection() as conn:
            changed = conn.execute(
                """UPDATE sources SET active=?,active_signature=?,pending=NULL,state=?,updated=?,issue=?,
                   index_stage=? WHERE id=? AND pending=? AND state NOT IN ('paused','removing')""",
                (active, signature, "empty" if empty_eml else "ready", now(), issue or ("eml_no_mail" if empty_eml else None),
                 "headers" if empty_eml else "complete", source, version),
            )
            if not changed.rowcount:
                raise Paused()
            if retain and unchanged is None:
                report("reuse_mail")
                conn.execute("UPDATE mails SET version=? WHERE id IN (SELECT id FROM outlook_retained)", (version,))
            report("save_metadata")
            conn.execute("DELETE FROM problems WHERE source=? AND version<>?", (source, active))
            if signatures is not None:
                self.store.save_pst_files(conn, source, signatures)
            if unchanged is None:
                self.store.commit_memory_revision(conn, source, active, report)
            else:
                report("commit")

    def process_outlook(self, row, temporary, event):
        source, version, previous = row["id"], row["pending"], row["active"]
        with outlook_store(row["path"]) as outlook:
            signature = outlook.store_id
            with self.store.connection() as conn:
                old = self.store.mail_identities(conn, source, previous) if row["active_signature"] == signature else {}
                if row["signature"] and row["signature"] != signature:
                    conn.execute("DELETE FROM mails WHERE source=? AND version=?", (source, version))
                pending = self.store.mail_identities(conn, source, version)
            folders, aliases = {}, {}
            for entry_id, folder in outlook.identities(lambda: self.check(event)):
                # Virtual search folders can expose the same EntryID more than once.
                if entry_id in folders and folders[entry_id] != folder:
                    aliases.setdefault(entry_id, {folders[entry_id]}).add(folder)
                previous_item = old.get(entry_id) or pending.get(entry_id)
                if entry_id not in folders or (previous_item and previous_item["folder"] == folder):
                    folders[entry_id] = folder

            self.state(source, "headers")
            self.check(event)
            retained = {entry: old[entry]["id"] for entry, folder in folders.items()
                        if entry in old and old[entry]["folder"] == folder}
            pending_ids = {entry for entry, item in pending.items()
                           if entry not in retained and folders.get(entry) == item["folder"]}
            with self.store.connection() as conn:
                conn.execute("CREATE TEMP TABLE outlook_retained(id INTEGER PRIMARY KEY)")
                conn.executemany("INSERT INTO outlook_retained VALUES(?)", ((mail,) for mail in retained.values()))
                conn.executemany("DELETE FROM mails WHERE id=?", ((item["id"],) for entry, item in pending.items()
                                                                 if entry not in pending_ids))
                done = len(retained) + len(pending_ids)
                conn.execute("""UPDATE sources SET signature=?,done=?,index_done=?,index_total=?,
                                failed=0,omitted=0,partial=0 WHERE id=? AND pending=?""",
                             (signature, done, done, len(folders), source, version))
            remaining = (entry for entry in folders if entry not in retained and entry not in pending_ids)
            batch = []
            with self.store.connection() as conn:
                for entry_id in remaining:
                    self.check(event)
                    mail = outlook.read(entry_id, folders[entry_id], temporary, aliases.get(entry_id, ()))
                    mail_id = self.store.add_mail(conn, source, version, entry_id, mail)
                    self.index_outlook_attachments(conn, mail_id, mail, temporary, event)
                    del mail
                    done += 1
                    batch.append(mail_id)
                    if len(batch) == 32:
                        conn.execute("UPDATE sources SET done=?,index_done=? WHERE id=? AND pending=?",
                                     (done, done, source, version))
                        conn.commit()
                        if previous is None:
                            self.store.append_pending_memory(source, batch)
                        batch.clear()
                conn.execute("UPDATE sources SET done=?,index_done=? WHERE id=? AND pending=?",
                             (done, done, source, version))
            if previous is None:
                self.store.append_pending_memory(source, batch)

            with self.store.connection() as conn:
                omitted, partial = conn.execute(
                    """SELECT COALESCE(SUM(a.status NOT IN ('완료','대기') AND NOT EXISTS(
                               SELECT 1 FROM passages p WHERE p.attachment=a.id AND p.location<>'파일명')),0),
                              COALESCE(SUM(a.status LIKE '일부 색인%'),0)
                       FROM mails m JOIN attachments a ON a.mail=m.id WHERE m.source=?
                       AND (m.version=? OR m.id IN (SELECT id FROM outlook_retained))""", (source, version),
                ).fetchone()
                conn.execute("UPDATE sources SET omitted=?,partial=? WHERE id=? AND pending=?",
                             (omitted, partial, source, version))

        issue = self.embed_revision(source, version, event, retain=True)
        self.start_finalization(source, version, event, "prepare_switch", issue)
        unchanged = previous if len(retained) == len(old) == len(folders) and row["active_signature"] == signature else None
        self.publish_revision(source, version, signature, event, issue or (None if folders else "outlook_no_mail"),
                              empty_eml=not folders, retain=True, unchanged=unchanged)

    def index_outlook_attachments(self, conn, mail_id, mail, temporary, event):
        entries = conn.execute("SELECT id FROM attachments WHERE mail=? ORDER BY id", (mail_id,)).fetchall()
        for entry, (name, part) in zip(entries, mail["attachments"], strict=True):
            self.check(event)
            try:
                status, locations = extract_attachment(name, part, temporary, self.pdf, check=lambda: self.check(event))
            except OutlookError as exc:
                status, locations = ("용량 초과" if str(exc) == "outlook_attachment_size" else "Outlook 첨부 읽기 실패"), []
            for location, text in locations:
                for start, segment in sections(text):
                    self.store.add_passage(conn, mail_id, mail, entry["id"], name, location, start, segment)
            conn.execute("UPDATE attachments SET status=? WHERE id=?", (status, entry["id"]))

    def pending_updates(self, row):
        """PST files that changed or use an older index format."""
        if row["kind"] != "eml_dir" or row["state"] not in {"ready", "empty", "error"}:
            return []
        path = Path(row["path"])
        known = self.store.pst_files(row["id"])
        outdated = self.store.outdated_pst_labels(row["id"], row["active"], known)
        changed = []
        try:
            files = sorted(item for item in path.rglob("*") if item.suffix.lower() == ".pst")
        except OSError:
            return []
        for pst in files:
            item = pst.relative_to(path).as_posix()
            try:
                label = item + "/"
                if (not pst.is_symlink() and stat.S_ISREG(pst.stat().st_mode)
                        and (label in outdated or known.get(label) != read_signature(pst))):
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
            for prefix in carried:
                self.check(event)
                adopted = self.store.carry_prefix(conn, source, previous, version, prefix)
                if adopted:
                    conn.execute("UPDATE sources SET done=done+? WHERE id=?", (adopted, source))
                    conn.commit()
            workspace = temp or self.spool
            if numbered and "" in carried:
                conn.execute("UPDATE sources SET index_done=0,index_total=0 WHERE id=? AND pending=?", (source, version))
                return
            old_mails = self.store.mail_identities(conn, source, previous)
            entries = []
            for entry in self.scan_entries(root, numbered=numbered, extras=extras):
                self.check(event)
                entries.append(entry)
            conn.execute("UPDATE sources SET index_done=0,index_total=? WHERE id=? AND pending=?", (len(entries), source, version))
            conn.commit()
            done = 0
            batch = []
            for done, (path, item, folder) in enumerate(entries, 1):
                self.check(event)
                if done > 1 and (done - 1) % 32 == 0:
                    conn.execute("UPDATE sources SET index_done=? WHERE id=? AND pending=?", (done - 1, source, version))
                    conn.commit()
                    if previous is None:
                        self.store.append_pending_memory(source, batch)
                        batch.clear()
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
                            conn, source, previous, version, item, document_identity(doc), index_format=None)
                        if adopted:
                            conn.execute("""UPDATE attachments SET status=? WHERE mail IN (
                                SELECT id FROM mails WHERE source=? AND version=? AND item=?)""",
                                         (status, source, version, item))
                        else:
                            mail_id = self.store.add_document(conn, source, version, item, doc)
                            if previous is None:
                                batch.append(mail_id)
                        conn.execute("UPDATE sources SET done=done+1,partial=partial+? WHERE id=?",
                                     (int(status.startswith("일부 색인")), source))
                    continue
                try:
                    stat_result = path.stat()
                    signature = (stat_result.st_size, stat_result.st_mtime_ns)
                    old = old_mails.get(item)
                    if (old and old["folder"] == folder and old["fingerprint"].startswith(MAIL_INDEX_FORMAT + ":")
                            and (old["file_size"], old["file_mtime_ns"]) == signature):
                        mail_id = self.store.adopt_mail(conn, version, old["id"])
                    else:
                        mail = parse_mail(path, folder)
                        current_stat = path.stat()
                        if (current_stat.st_size, current_stat.st_mtime_ns) != signature:
                            raise ImportIssue("changed")
                        if (old and old["folder"] == folder and old["fingerprint"].startswith(MAIL_INDEX_FORMAT + ":")
                                and matches_fingerprint(mail, item, old["fingerprint"])):
                            mail_id = self.store.adopt_mail(conn, version, old["id"])
                        else:
                            mail_id = self.store.add_mail(conn, source, version, item, mail)
                        conn.execute("UPDATE mails SET file_size=?,file_mtime_ns=? WHERE id=?", (*signature, mail_id))
                except (ValueError, OSError, LookupError, TypeError, RecursionError):
                    conn.execute("INSERT INTO problems VALUES(?,?,?,'parse')", (source, version, item))
                    conn.execute("UPDATE sources SET failed=failed+1 WHERE id=?", (source,))
                else:
                    if previous is None:
                        batch.append(mail_id)
                    conn.execute("UPDATE sources SET done=done+1 WHERE id=?", (source,))
            conn.execute("UPDATE sources SET index_done=?,index_total=? WHERE id=? AND pending=?", (done, done, source, version))
        if previous is None:
            self.store.append_pending_memory(source, batch)

    def index_attachments(self, root, temp, source, version, event, extras=()):
        with self.store.connection() as conn:
            last = 0
            extracted = []
            new_extracts = {}
            total, done = conn.execute(
                """SELECT COUNT(*),COALESCE(SUM(a.status<>'대기'),0)
                   FROM mails m JOIN attachments a ON a.mail=m.id WHERE m.source=? AND m.version=? AND m.kind='mail'""",
                (source, version),
            ).fetchone()
            conn.execute("UPDATE sources SET index_done=?,index_total=? WHERE id=? AND pending=?", (done, total, source, version))
            conn.commit()

            def load_extract(key, extract):
                if key in new_extracts:
                    return "완료", new_extracts[key], key
                row = conn.execute("SELECT locations FROM attachment_extracts WHERE digest=?", (key,)).fetchone()
                if row is not None:
                    return "완료", json.loads(row["locations"]), key
                status, locations = extract()
                if status == "완료" and locations:
                    new_extracts[key] = locations
                    return status, locations, key
                return status, locations, None

            def save_extracted():
                nonlocal done
                with conn:
                    conn.executemany(
                        "INSERT OR IGNORE INTO attachment_extracts VALUES(?,?)",
                        ((key, json.dumps(locations, ensure_ascii=False)) for key, locations in new_extracts.items()),
                    )
                    for attachment, mail, status, locations, content_key in extracted:
                        if status == "원본 변경":
                            conn.execute(
                                "INSERT OR IGNORE INTO problems VALUES(?,?,?,'mail_original_changed')",
                                (source, version, attachment["item"]),
                            )
                        for location, text in locations:
                            for start, segment in sections(text):
                                embedding_key = (hashlib.sha256(content_key + segment.encode()).digest()
                                                 if content_key is not None else None)
                                self.store.add_passage(conn, attachment["mail"], mail, attachment["id"],
                                                       attachment["name"], location, start, segment,
                                                       embedding_key=embedding_key)
                        conn.execute("UPDATE attachments SET status=?,content_key=? WHERE id=?",
                                     (status, content_key, attachment["id"]))
                    omitted = sum(not locations for _, _, _, locations, _ in extracted)
                    partial = sum(bool(locations) and status.startswith("일부 색인")
                                  for _, _, status, locations, _ in extracted)
                    conn.execute("UPDATE sources SET omitted=omitted+?,partial=partial+? WHERE id=?",
                                 (omitted, partial, source))
                    done += len(extracted)
                    conn.execute("UPDATE sources SET index_done=? WHERE id=? AND pending=?", (done, source, version))
                extracted.clear()
                new_extracts.clear()

            while True:
                self.check(event)
                batch = conn.execute(
                    """SELECT m.item,m.folder,m.id,m.fingerprint FROM mails m WHERE m.source=? AND m.version=? AND m.id>?
                       AND EXISTS(SELECT 1 FROM attachments a WHERE a.mail=m.id AND a.status='대기')
                       ORDER BY m.id LIMIT 128""", (source, version, last),
                ).fetchall()
                if not batch:
                    break
                for row in batch:
                    self.check(event)
                    path = self.locate(root, extras, row["item"])
                    attachments = conn.execute(
                        "SELECT id,mail,name,status,? item FROM attachments WHERE mail=? ORDER BY id",
                        (row["item"], row["id"]),
                    ).fetchall()
                    try:
                        mail = parse_mail(path, row["folder"])
                    except (ValueError, OSError, LookupError, TypeError, RecursionError):
                        mail = None
                    changed = mail is not None and not matches_fingerprint(mail, row["item"], row["fingerprint"])
                    if changed and (root.is_file() or not path.is_relative_to(root)):
                        raise ImportIssue("changed")
                    for position, attachment in enumerate(attachments):
                        if attachment["status"] != "대기":
                            continue
                        self.check(event)
                        cached = None
                        if changed:
                            status, locations = "원본 변경", []
                        elif mail is None:
                            status, locations = "추출 실패", []
                        else:
                            name, part = mail["attachments"][position]
                            result = extract_attachment(name, part, temp, self.pdf,
                                                        check=lambda: self.check(event), cache=load_extract)
                            status, locations = result[:2]
                            cached = result[2] if len(result) == 3 else None
                        extracted.append((attachment, mail, status, locations, cached))
                        if len(extracted) == 24:
                            save_extracted()
                last = batch[-1]["id"]
            if extracted:
                save_extracted()

    def index_embeddings(self, source, version, event, *, retain=False):
        model = self.ai.embedding_index
        batch_size = self.embedding_batch_size
        timeout = self.embedding_batch_timeout
        scope = "(m.version=? OR m.id IN (SELECT id FROM outlook_retained))" if retain else "m.version=?"
        self.check(event)
        with self.store.connection() as conn:
            total, done = conn.execute(
                f"""SELECT COUNT(*),COALESCE(SUM(v.passage IS NOT NULL OR r.passage IS NOT NULL),0)
                   FROM mails m JOIN passages p ON p.mail=m.id
                   LEFT JOIN vectors v ON v.passage=p.id AND v.model=?
                   LEFT JOIN embedding_refs r ON r.passage=p.id AND r.model=?
                   WHERE m.source=? AND {scope}""", (model, model, source, version),
            ).fetchone()
            conn.execute("""UPDATE sources SET state='embeddings',index_stage='embeddings',embedding_done=?,
                            embedding_total=?,embedding_model_key=?
                            WHERE id=? AND pending=? AND state NOT IN ('paused','removing')""",
                         (done, total, model, source, version))
        last = 0
        while True:
            self.check(event)
            with self.store.connection() as conn:
                rows = conn.execute(
                    f"""SELECT p.id,p.text,m.subject,a.name,p.embedding_key FROM passages p CROSS JOIN mails m ON m.id=p.mail
                       LEFT JOIN attachments a ON a.id=p.attachment WHERE m.source=? AND {scope} AND p.id>?
                       AND NOT EXISTS(SELECT 1 FROM vectors v WHERE v.passage=p.id AND v.model=?)
                       AND NOT EXISTS(SELECT 1 FROM embedding_refs r WHERE r.passage=p.id AND r.model=?)
                       ORDER BY p.id LIMIT ?""", (source, version, last, model, model, batch_size)
                ).fetchall()
                shared = {}
                inputs = {}
                for row in rows:
                    key = row["embedding_key"]
                    if key is None:
                        inputs[row["id"]] = f"{row['subject']}\n{row['name'] or ''}\n{row['text']}"
                    elif key not in inputs and key not in shared:
                        existing = conn.execute(
                            "SELECT value FROM embedding_values WHERE model=? AND digest=?", (model, key),
                        ).fetchone()
                        if existing:
                            shared[key] = existing["value"]
                        else:
                            inputs[key] = row["text"]
            if not rows:
                return None
            try:
                if self.ai.embedding_index != model:
                    return "embedding_unavailable"
                encoded = [pack_vector(v) for v in self.ai.embed(list(inputs.values()), timeout=timeout)] if inputs else []
                if len(encoded) != len(inputs):
                    raise ValueError("embeddings")
                if self.ai.embedding_index != model:
                    return "embedding_unavailable"
                shared.update(zip(inputs, encoded))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, http.client.HTTPException) as exc:
                winerror = getattr(exc, "winerror", None)
                backend = "gateway" if self.ai.embedding_api and self.ai.embedding_api.index_key else "Ollama"
                retryable = isinstance(exc, TimeoutError) or (
                    isinstance(exc, GatewayRequestError) and exc.status in {408, 413, 504})
                if retryable and len(inputs) > 1:
                    batch_size = max(1, len(inputs) // 2)
                    logger.warning("Embedding batch retry (backend=%s, completed=%d/%d, timeout=%ds, next_batch=%d): %s",
                                   backend, done, total, timeout, batch_size, type(exc).__name__)
                    continue
                logger.error("Embedding batch failed (backend=%s, completed=%d/%d): %s (errno=%s, winerror=%s)",
                             backend, done, total, type(exc).__name__, getattr(exc, "errno", None), winerror)
                if isinstance(exc, ConnectionAbortedError) or winerror == 10053:
                    return "embedding_connection_aborted"
                return "embedding_unavailable"
            self.check(event)
            with self.store.connection() as conn:
                for row in rows:
                    passage = row["id"]
                    key = row["embedding_key"]
                    value = shared[key if key is not None else passage]
                    if key is None:
                        conn.execute("INSERT INTO vectors VALUES(?,?,?)", (passage, model, value))
                    else:
                        conn.execute("INSERT OR IGNORE INTO embedding_values VALUES(?,?,?)", (model, key, value))
                        conn.execute("INSERT INTO embedding_refs VALUES(?,?,?)", (model, passage, key))
                    conn.executemany(
                        "INSERT INTO vector_buckets VALUES(?,?,?)",
                        ((model, bucket, passage) for bucket in buckets(vector_array(value))),
                    )
                done += len(rows)
                conn.execute("UPDATE sources SET embedding_done=? WHERE id=? AND pending=?",
                             (done, source, version))
            last = rows[-1]["id"]
