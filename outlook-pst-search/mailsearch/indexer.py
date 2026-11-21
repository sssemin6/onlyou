"""Background imports with persistent progress and atomic version switches."""
import http.client
import os
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

from .extract import CREATE_FLAGS, extract_attachment, parse_mail
from .filesystem import local_path, stable_input
from .store import buckets, now, pack_vector, read_signature, sections, vector_array


NO_MAIL = {"pst_no_mail", "eml_no_mail"}


class Paused(Exception):
    pass


class ImportIssue(Exception):
    pass


class Indexer:
    def __init__(self, store, readpst="readpst", pdf="pdftotext", ai=None):
        self.store, self.readpst, self.pdf, self.ai = store, readpst, pdf, ai
        self.lock = threading.RLock()
        self.events = {}
        self.worker = None
        self.stopping = False
        with store.connection() as conn:
            conn.execute("UPDATE sources SET state='paused',issue='interrupted' WHERE state IN ('extracting','headers','attachments','embeddings')")
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
        try:
            path = local_path(path, strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ImportIssue("source_path") from exc
        if path == self.store.root or path.is_relative_to(self.store.root) or self.store.root.is_relative_to(path):
            raise ImportIssue("source_path")
        if path.is_dir():
            kind = "eml_dir"
        elif path.is_file() and path.suffix.lower() in {".pst", ".eml"}:
            kind = path.suffix.lower().lstrip(".")
        else:
            raise ImportIssue("source_format")
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
                    if action == "refresh" or row["issue"] in NO_MAIL | {"changed", "mail_parse_failed"}:
                        self.discard_pending(conn, row)
                        conn.execute(
                            """UPDATE sources SET pending=revision+1,revision=revision+1,signature=NULL,
                               done=0,failed=0,omitted=0,partial=0 WHERE id=?""", (source,)
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
                task = next((row for row in self.store.sources() if row["state"] in {"queued", "removing"}), None)
                if self.stopping or task is None:
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
            except (Paused, ImportIssue, OSError, sqlite3.Error) as exc:
                code = "disk_or_source"
                if isinstance(exc, ImportIssue):
                    code = str(exc)
                elif isinstance(exc, Paused):
                    code = "interrupted"
                try:
                    with self.store.connection() as conn:
                        if task["state"] == "removing":
                            conn.execute("UPDATE sources SET state='remove_failed',issue=? WHERE id=?", (code, source))
                        else:
                            if isinstance(exc, Paused):
                                state = "paused"
                            else:
                                state = "empty" if code in NO_MAIL else "error"
                            conn.execute(
                                "UPDATE sources SET state=?,issue=? WHERE id=? AND state NOT IN ('removing','ready')",
                                (state, code, source),
                            )
                except sqlite3.Error:
                    # The last committed index remains available even if the disk is full.
                    with self.lock:
                        self.worker = None
                    return
            finally:
                with self.lock:
                    self.events.pop(source, None)

    @staticmethod
    def check(event):
        if event.is_set():
            raise Paused()

    def state(self, source, state, issue=None):
        with self.store.connection() as conn:
            conn.execute("UPDATE sources SET state=?,issue=? WHERE id=? AND state NOT IN ('paused','removing')", (state, issue, source))

    def export(self, path, stage, event):
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
                while process.poll() is None:
                    if event.wait(.2):
                        raise Paused()
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
                is_mail = path.suffix.lower() == ".eml" or (numbered and name.isascii() and name.isdecimal())
                if is_mail and not path.is_symlink() and path.is_file():
                    relative = path.relative_to(root)
                    yield path, relative.as_posix(), "/" + ("" if relative.parent == Path(".") else relative.parent.as_posix())

    def process(self, row, event):
        source, version = row["id"], row["pending"]
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
        if is_pst:
            try:
                with stable_input(path):
                    if read_signature(path) != signature:
                        raise ImportIssue("changed")
                    with path.open("rb") as stream:
                        header = stream.read(12)
                    if len(header) != 12 or header[:4] != b"!BDN" or header[8:10] != b"SM":
                        raise ImportIssue("pst_invalid")
                    root = self.export(path, stage, event)
                    if read_signature(path) != signature:
                        raise ImportIssue("changed")
            except PermissionError as exc:
                raise ImportIssue("pst_in_use") from exc
        else:
            root = path
        self.check(event)
        self.state(source, "headers")
        self.index_headers(root, source, version, event, numbered=is_pst)
        self.check(event)
        progress = self.store.source(source)
        empty_eml = progress["done"] == 0 and not progress["failed"] and not is_pst
        if not progress["done"] and not empty_eml:
            raise ImportIssue("mail_parse_failed" if progress["failed"] else "pst_no_mail")
        self.state(source, "attachments")
        self.index_attachments(root, temp, source, version, event)
        issue = None
        if self.ai is not None and self.ai.embedding_model:
            self.state(source, "embeddings")
            issue = self.index_embeddings(source, version, event)
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
        shutil.rmtree(stage, ignore_errors=True)

    def index_headers(self, root, source, version, event, *, numbered=False):
        with self.store.connection() as conn:
            batch = 0
            for path, item, folder in self.entries(root, numbered=numbered):
                self.check(event)
                if conn.execute("SELECT 1 FROM mails WHERE source=? AND version=? AND item=?", (source, version, item)).fetchone():
                    continue
                if conn.execute("SELECT 1 FROM problems WHERE source=? AND version=? AND item=?", (source, version, item)).fetchone():
                    continue
                try:
                    mail = parse_mail(path, folder)
                except (ValueError, OSError, LookupError, TypeError, RecursionError):
                    conn.execute("INSERT INTO problems VALUES(?,?,?,'parse')", (source, version, item))
                    conn.execute("UPDATE sources SET failed=failed+1 WHERE id=?", (source,))
                else:
                    self.store.add_mail(conn, source, version, item, mail)
                    conn.execute("UPDATE sources SET done=done+1 WHERE id=?", (source,))
                batch += 1
                if batch % 32 == 0:
                    conn.commit()

    def index_attachments(self, root, temp, source, version, event):
        with self.store.connection() as conn:
            pending = conn.execute(
                """SELECT m.item,m.folder,m.id FROM mails m WHERE m.source=? AND m.version=? AND
                   EXISTS(SELECT 1 FROM attachments a WHERE a.mail=m.id AND a.status='대기') ORDER BY m.id""", (source, version)
            )
            batch = pending.fetchmany(128)
            while batch:
                for row in batch:
                    self.check(event)
                    path = root if root.is_file() else root / row["item"]
                    try:
                        mail = parse_mail(path, row["folder"])
                    except (ValueError, OSError, LookupError, TypeError, RecursionError):
                        count = conn.execute("UPDATE attachments SET status='추출 실패' WHERE mail=? AND status='대기'", (row["id"],)).rowcount
                        conn.execute("UPDATE sources SET omitted=omitted+? WHERE id=?", (count, source))
                        conn.commit()
                        continue
                    attachments = conn.execute("SELECT id,name,status FROM attachments WHERE mail=? ORDER BY id", (row["id"],)).fetchall()
                    if [a["name"] for a in attachments] != [name for name, _ in mail["attachments"]]:
                        raise ImportIssue("changed")
                    for attachment, (name, part) in zip(attachments, mail["attachments"]):
                        if attachment["status"] != "대기":
                            continue
                        self.check(event)
                        status, locations = extract_attachment(name, part, temp, self.pdf)
                        for location, text in locations:
                            for start, segment in sections(text):
                                self.store.add_passage(conn, row["id"], mail, attachment["id"], name, location, start, segment)
                        conn.execute("UPDATE attachments SET status=? WHERE id=?", (status, attachment["id"]))
                        conn.execute(
                            "UPDATE sources SET omitted=omitted+?,partial=partial+? WHERE id=?",
                            (int(status not in {"완료", "일부 색인"}), int(status == "일부 색인"), source),
                        )
                        conn.commit()
                batch = pending.fetchmany(128)

    def index_embeddings(self, source, version, event):
        model = self.ai.embedding_model
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
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, http.client.HTTPException):
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
            last = rows[-1]["id"]
