"""Store immutable mail versions and search their locally indexed passages."""
import array
import contextlib
import hashlib
import json
import math
import os
import sys
import re
import sqlite3
import struct
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    active INTEGER, pending INTEGER, revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued', signature TEXT, updated TEXT,
    done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
    omitted INTEGER NOT NULL DEFAULT 0, partial INTEGER NOT NULL DEFAULT 0, issue TEXT
);
CREATE TABLE IF NOT EXISTS mails (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source INTEGER NOT NULL REFERENCES sources(id),
    version INTEGER NOT NULL, item TEXT NOT NULL, fingerprint TEXT NOT NULL,
    folder TEXT NOT NULL, subject TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
    sent TEXT NOT NULL, message_id TEXT NOT NULL, topic TEXT NOT NULL, body TEXT NOT NULL,
    UNIQUE(source, version, item)
);
CREATE INDEX IF NOT EXISTS mail_message_id ON mails(message_id);
CREATE INDEX IF NOT EXISTS mail_topic ON mails(topic);
CREATE INDEX IF NOT EXISTS mail_sent ON mails(sent DESC, id DESC);
CREATE TABLE IF NOT EXISTS replies (
    mail INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE, parent TEXT NOT NULL,
    PRIMARY KEY(mail, parent)
);
CREATE INDEX IF NOT EXISTS replies_parent ON replies(parent);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY, mail INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    name TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mail INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    attachment INTEGER REFERENCES attachments(id) ON DELETE CASCADE, location TEXT NOT NULL,
    start INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_mail ON passages(mail);
CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(subject, people, label, text, tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS passage_remove AFTER DELETE ON passages BEGIN
    DELETE FROM terms WHERE rowid=old.id;
END;
CREATE TABLE IF NOT EXISTS problems (
    source INTEGER NOT NULL REFERENCES sources(id), version INTEGER NOT NULL, item TEXT NOT NULL,
    code TEXT NOT NULL, PRIMARY KEY(source, version, item)
);
CREATE TABLE IF NOT EXISTS pst_files (
    source INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    label TEXT NOT NULL, signature TEXT NOT NULL,
    PRIMARY KEY(source, label)
);
CREATE TABLE IF NOT EXISTS vectors (
    passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    model TEXT NOT NULL, value BLOB NOT NULL,
    PRIMARY KEY(model, passage)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS vector_buckets (
    model TEXT NOT NULL, bucket INTEGER NOT NULL, passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    PRIMARY KEY(model, bucket, passage)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS bucket_passage ON vector_buckets(passage);
"""
MAIL_COLUMNS = "m.id,m.source,m.fingerprint,m.folder,m.subject,m.sender,m.recipient,m.sent,m.kind,s.path,s.updated"
JOIN = "JOIN sources s ON s.id=m.source"
VISIBLE_VERSION = """m.version=coalesce(s.active,s.pending)
    AND (s.active IS NOT NULL OR coalesce(s.issue,'')<>'changed')"""
HANGUL = re.compile(r"[가-힣]+")
WORD = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*", re.UNICODE)
QUOTED = re.compile(r'"([^"\n]{1,120})"')


def quoted_plan(query):
    """Return a boolean plan of "quoted" exact terms, or None when none are quoted.

    Terms separated by nothing or & must all appear; terms separated by or need one.
    """
    found = list(QUOTED.finditer(query))
    if not found:
        return None
    plan = []
    for index, match in enumerate(found):
        term = match.group(1).strip()
        if not term:
            continue
        separator = query[found[index - 1].end():match.start()] if index else ""
        joiner = "or" if re.search(r"(?:^|[^a-z])or(?:$|[^a-z])", separator, re.I) or "|" in separator else "&"
        plan.append((joiner, term))
    return plan or None
SUFFIXES = ("으로부터", "에게서는", "에서", "으로", "에게", "부터", "까지", "처럼", "으로는", "이고", "만큼", "이랑", "이며", "의", "은", "는", "이", "가", "을", "를", "도", "만", "와", "과", "로", "건")
STOP = set("지난번에 지난번 결국 혹시 그리고 그거 어디 무슨 언제 누가 어떤 무엇 알려줘 알려주세요 확인해줘 됐지 정해졌지 정해졌어 됐어 요약해줘 부탁해 그 뒤에 뒤 기준 내역 건 관련 결과 대해서 최종 나한테 그때 찾고싶어 어떻게 내용 보낸 메일 받은 보여줘".split())
SYNONYMS = {
    "밀": ("지연", "납기", "변경"), "늦": ("지연", "변경"), "납품": ("납기", "출하"),
    "정해": ("확정", "승인"), "결정": ("확정", "승인"), "견적": ("단가", "금액"),
    "반려": ("거절", "취소"), "취소": ("번복", "철회"), "회신": ("답변",),
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_signature(path):
    info = path.stat()
    return json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


def grams(word):
    return [f"한글{word[i:i+2]}" for i in range(len(word) - 1)]


def index_text(value):
    value = normalized(value)
    words = WORD.findall(value)
    identifiers = [f"품번{re.sub(r'[-_]', '', word)}" for word in words if re.search(r"[-_]", word)]
    fragments = HANGUL.findall(value)
    korean_grams = dict.fromkeys(gram for word in fragments for gram in grams(word))
    return " ".join([value, *identifiers, *korean_grams])


def query_words(query, fuzzy=False):
    tokens = []
    value = normalized(query)
    for word in WORD.findall(value)[:32]:
        if fuzzy and HANGUL.fullmatch(word):
            if word in STOP:
                continue
            stem = next((word[:-len(s)] for s in SUFFIXES if word.endswith(s) and len(word) - len(s) >= 2), word)
            if stem not in STOP:
                tokens.append(stem)
        else:
            tokens.append(word)
    return list(dict.fromkeys(tokens))


def fts_query(words, mode):
    clauses = []
    for word in words:
        if mode == "grams" and HANGUL.fullmatch(word) and len(word) >= 2:
            clauses.append("(" + " AND ".join(f'"{gram}"' for gram in grams(word)) + ")")
        elif re.search(r"[-_]", word):
            joined = re.sub(r"[-_]", "", word)
            clauses.append(f'"품번{joined}"' if mode == "terms" else f'"{word}"')
        else:
            clauses.append(f'"{word}"')
    return " OR ".join(clauses)


def topic_key(subject):
    subject = re.sub(r"^(?:(?:re|fw|fwd|답장|전달)\s*:\s*)+", "", normalized(subject)).strip()
    return re.sub(r"\s+", " ", subject)


def fingerprint(mail, item):
    attachment_names = [name for name, _ in mail["attachments"]]
    identity = [mail[key] for key in ("message_id", "subject", "sender", "recipient", "sent", "body")]
    # An attachment-bearing copy may contain a different revision under the same name.
    return hashlib.sha256(json.dumps([identity, attachment_names, item if attachment_names else ""], ensure_ascii=False).encode()).hexdigest()


def like_prefix(prefix):
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def document_identity(doc):
    """Fingerprint basis for a document: filename and extracted content, never timestamps."""
    digest = hashlib.sha256("\n".join(f"{location}\n{text}" for location, text in doc["sections"]).encode()).hexdigest()
    return {"message_id": "", "subject": doc["name"], "sender": "", "recipient": "",
            "sent": "", "body": digest, "reply_ids": "", "attachments": [(doc["name"], None)]}


def sections(text, size=1100):
    position = 0
    while position < len(text):
        end = min(len(text), position + size)
        if end < len(text):
            boundary = max(text.rfind("\n", position + 500, end), text.rfind(" ", position + 500, end))
            if boundary > position:
                end = boundary + 1
        yield position, text[position:end]
        if end == len(text):
            break
        position = end - 120


def pack_vector(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 4096:
        raise ValueError("vector")
    magnitude = math.sqrt(sum(float(x) ** 2 for x in values))
    if not math.isfinite(magnitude) or magnitude == 0:
        raise ValueError("vector")
    return struct.pack(f"<{len(values)}f", *(float(x) / magnitude for x in values))


def vector_array(blob):
    result = array.array("f")
    result.frombytes(blob)
    if sys.byteorder != "little":
        result.byteswap()
    return result


def buckets(vector):
    # Multiple sparse random projections keep lookup local on large archives.
    for band in range(6):
        value = 0
        for bit in range(11):
            offset = band * 11 + bit
            sign = sum(
                vector[((offset + 1) * (factor * 211 + 307) + factor * 73) % len(vector)] * (-1 if factor % 2 else 1)
                for factor in range(1, 9)
            )
            value |= int(sign >= 0) << bit
        yield band * 2048 + value


class Store:
    def __init__(self, root):
        if sqlite3.sqlite_version_info < (3, 42, 0):
            raise RuntimeError("SQLite 3.42 or later is required for secure FTS5 deletion.")
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        self.db = self.root / "mail.sqlite"
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(mails)")}
            if "kind" not in columns:
                # Tab scoping (mail vs document); older rows are classified by item suffix.
                conn.execute("ALTER TABLE mails ADD COLUMN kind TEXT NOT NULL DEFAULT 'mail'")
                suffixes = ("pdf", "docx", "xlsx", "txt", "doc", "xls")
                conn.execute("UPDATE mails SET kind='document' WHERE " +
                             " OR ".join("item LIKE '%.' || ?" for _ in suffixes), suffixes)
            # Owner-correspondence (PCF) documents get their own tab; re-checked every start.
            conn.execute("UPDATE mails SET kind='pcf' WHERE kind='document' AND lower(item) LIKE '%pcf%'")
            conn.execute("INSERT INTO terms(terms,rank) VALUES('secure-delete',1)")

    @contextlib.contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def source(self, source):
        with self.connection() as conn:
            return conn.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone()

    def sources(self):
        with self.connection() as conn:
            # Per-kind item counts feed the sidebar's mail/document/PCF sections.
            counts = defaultdict(dict)
            for row in conn.execute(
                f"SELECT m.source source,m.kind kind,COUNT(*) n FROM mails m {JOIN} GROUP BY m.source,m.kind"
            ):
                counts[row["source"]][row["kind"]] = row["n"]
            return [{**dict(row), "counts": counts.get(row["id"], {})}
                    for row in conn.execute("SELECT * FROM sources ORDER BY id")]

    def allowed_sources(self, source=None):
        ids = []
        for row in self.sources():
            if source is not None and row["id"] != source:
                continue
            path = Path(row["path"])
            try:
                if row["kind"] == "eml_dir":
                    with os.scandir(path) as iterator:
                        next(iterator, None)
                else:
                    with path.open("rb"):
                        pass
            except OSError:
                continue
            ids.append(row["id"])
        return ids

    def scope(self, filters, extra=()):
        ids = self.allowed_sources(filters.get("source"))
        clauses = [VISIBLE_VERSION, "m.source IN (SELECT value FROM json_each(?))"]
        params = [json.dumps(ids)]
        for name, col in {"sender": "m.sender", "recipient": "m.recipient", "subject": "m.subject", "body": "m.body"}.items():
            if filters.get(name):
                clauses.append(f"instr(lower({col}),lower(?))>0")
                params.append(filters[name])
        if filters.get("attachment") in ("yes", "no"):
            negate = "" if filters["attachment"] == "yes" else "NOT "
            clauses.append(f"{negate}EXISTS(SELECT 1 FROM attachments a WHERE a.mail=m.id)")
        if filters.get("tab") in ("mail", "document", "pcf"):
            clauses.append("m.kind=?")
            params.append(filters["tab"])
        if filters.get("after"):
            clauses.append("m.sent>=?")
            params.append(filters["after"])
        if filters.get("before"):
            clauses.append("m.sent<>'' AND m.sent<?")
            params.append(filters["before"])
        if extra:
            clauses.append("m.id IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(extra))
        return " AND ".join(clauses), params

    def add_mail(self, conn, source, version, item, mail):
        cursor = conn.execute(
            """INSERT INTO mails(source,version,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, version, item, fingerprint(mail, item), *(mail[k] for k in ("folder", "subject", "sender", "recipient", "sent", "message_id")),
             topic_key(mail["subject"]), mail["body"]),
        )
        mail_id = cursor.lastrowid
        parents = list(dict.fromkeys(re.findall(r"<[^<>\s]+>", mail["reply_ids"])))[:100]
        conn.executemany("INSERT INTO replies(mail,parent) VALUES(?,?)", ((mail_id, x) for x in parents))
        for start, text in sections(mail["body"] or mail["subject"] or "본문 없음"):
            self.add_passage(conn, mail_id, mail, None, "", "본문" if mail["body"] else "제목", start, text)
        for name, _ in mail["attachments"]:
            attachment_id = conn.execute(
                "INSERT INTO attachments(mail,name,status) VALUES(?,?,'대기')", (mail_id, name)
            ).lastrowid
            self.add_passage(conn, mail_id, mail, attachment_id, name, "파일명", 0, name)
        return mail_id

    def add_passage(self, conn, mail_id, mail, attachment, name, location, start, text):
        passage_id = conn.execute(
            "INSERT INTO passages(mail,attachment,location,start,text) VALUES(?,?,?,?,?)",
            (mail_id, attachment, location, start, text),
        ).lastrowid
        conn.execute(
            "INSERT INTO terms(rowid,subject,people,label,text) VALUES(?,?,?,?,?)",
            (passage_id, index_text(mail["subject"]), index_text(mail["sender"] + " " + mail["recipient"]),
             index_text(name), index_text(text)),
        )

    def add_document(self, conn, source, version, item, doc):
        """Index a standalone document: one archive row with located passages."""
        mail = document_identity(doc)
        cursor = conn.execute(
            """INSERT INTO mails(source,version,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body,kind)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, version, item, fingerprint(mail, item), doc["folder"], doc["name"], "", "",
             doc["sent"], "", topic_key(doc["name"]), "",
             "pcf" if "pcf" in Path(item).stem.lower() else "document"),
        )
        mail_id = cursor.lastrowid
        attachment_id = conn.execute(
            "INSERT INTO attachments(mail,name,status) VALUES(?,?,?)", (mail_id, doc["name"], doc["status"])
        ).lastrowid
        for location, text in doc["sections"]:
            for start, segment in sections(text):
                self.add_passage(conn, mail_id, mail, attachment_id, doc["name"], location, start, segment)
        return mail_id

    def adopt_mail(self, conn, version, old_id):
        """Copy an imported mail with its index and embeddings into a new version."""
        new_id = conn.execute(
            """INSERT INTO mails(source,version,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body,kind)
               SELECT source,?,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body,kind
               FROM mails WHERE id=?""", (version, old_id)
        ).lastrowid
        conn.execute("INSERT INTO replies(mail,parent) SELECT ?,parent FROM replies WHERE mail=?", (new_id, old_id))
        attachments = {}
        for row in conn.execute("SELECT id,name,status FROM attachments WHERE mail=? ORDER BY id", (old_id,)):
            attachments[row["id"]] = conn.execute(
                "INSERT INTO attachments(mail,name,status) VALUES(?,?,?)", (new_id, row["name"], row["status"])
            ).lastrowid
        for row in conn.execute("SELECT id,attachment,location,start,text FROM passages WHERE mail=? ORDER BY id", (old_id,)):
            passage = conn.execute(
                "INSERT INTO passages(mail,attachment,location,start,text) VALUES(?,?,?,?,?)",
                (new_id, attachments.get(row["attachment"]), row["location"], row["start"], row["text"]),
            ).lastrowid
            conn.execute(
                "INSERT INTO terms(rowid,subject,people,label,text) SELECT ?,subject,people,label,text FROM terms WHERE rowid=?",
                (passage, row["id"]),
            )
            conn.execute("INSERT INTO vectors(passage,model,value) SELECT ?,model,value FROM vectors WHERE passage=?", (passage, row["id"]))
            conn.executemany(
                "INSERT INTO vector_buckets(model,bucket,passage) SELECT model,bucket,? FROM vector_buckets WHERE passage=?",
                ((passage, row["id"]),),
            )
        return new_id

    def try_adopt(self, conn, source, previous, version, item, mail):
        """Reuse the previous version's work when the mail content is unchanged."""
        old = conn.execute(
            "SELECT id FROM mails WHERE source=? AND version=? AND item=? AND fingerprint=?",
            (source, previous, item, fingerprint(mail, item)),
        ).fetchone()
        if old is None:
            return False
        self.adopt_mail(conn, version, old["id"])
        return True

    def carry_prefix(self, conn, source, previous, version, prefix):
        """Adopt every previous-version mail under an unchanged PST's item prefix."""
        rows = conn.execute(
            "SELECT id FROM mails WHERE source=? AND version=? AND item LIKE ? ESCAPE '\\'",
            (source, previous, like_prefix(prefix)),
        ).fetchall()
        for row in rows:
            self.adopt_mail(conn, version, row["id"])
        return len(rows)

    def pst_files(self, source):
        with self.connection() as conn:
            return {row["label"]: row["signature"] for row in
                    conn.execute("SELECT label,signature FROM pst_files WHERE source=?", (source,))}

    def save_pst_files(self, conn, source, signatures):
        conn.execute("DELETE FROM pst_files WHERE source=?", (source,))
        conn.executemany("INSERT INTO pst_files VALUES(?,?,?)", ((source, *item) for item in signatures.items()))

    def lexical(self, question, filters, fuzzy=False, limit=100):
        where, params = self.scope(filters)
        words = query_words(question, fuzzy)
        if not question.strip():
            with self.connection() as conn:
                rows = conn.execute(
                    f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,NULL attachment
                        FROM mails m {JOIN} JOIN passages p ON p.id=(SELECT min(id) FROM passages WHERE mail=m.id)
                        WHERE {where} ORDER BY m.sent DESC,m.id DESC LIMIT ?""", (*params, limit)
                ).fetchall()
                return [dict(row) for row in rows]
        if not words:
            return []
        expanded = list(dict.fromkeys([*words, *(word for key, alternatives in SYNONYMS.items() if fuzzy and key in question for word in alternatives)]))
        ranked = {}
        exact_identifier = len(words) == 1 and bool(re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)+", words[0]))
        strategies = (("terms", words, 1.5), ("grams", expanded, 1.0))
        for mode, group, weight in strategies:
            if mode == "grams" and exact_identifier and ranked and not fuzzy:
                break
            statement = fts_query(group, mode)
            with self.connection() as conn:
                rows = conn.execute(
                    f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,a.name attachment
                        FROM terms JOIN passages p ON p.id=terms.rowid JOIN mails m ON m.id=p.mail
                        {JOIN} LEFT JOIN attachments a ON a.id=p.attachment
                        WHERE terms MATCH ? AND {where}
                        ORDER BY bm25(terms,7.0,2.0,4.0,1.0) LIMIT ?""",
                    (statement, *params, limit),
                )
                for rank, row in enumerate(rows):
                    entry, score = ranked.get(row["passage"], (dict(row), 0))
                    ranked[row["passage"]] = (entry, score + weight / (30 + rank))
        ordered = sorted(ranked.values(), key=lambda entry: entry[1], reverse=True)
        if fuzzy:
            return [row for row, _ in ordered[:limit]]
        exact = normalized(question.strip())
        return [row for row, _ in sorted(ordered, key=lambda entry: (
            exact_identifier and exact in WORD.findall(normalized(entry[0]["subject"] + " " + entry[0]["text"])), entry[1]
        ), reverse=True)[:limit]]

    def exact_search(self, plan, filters):
        """Search "quoted" terms as exact substrings; & within a group, or between groups."""
        groups, current = [], []
        for joiner, term in plan:
            if joiner == "or" and current:
                groups.append(current)
                current = [term]
            else:
                current.append(term)
        groups.append(current)
        selected = {}
        for group in groups:
            matches = None
            for term in group:
                verified = {}
                for row in self.lexical(term, filters, fuzzy=False, limit=400):
                    if normalized(term) in normalized(f"{row['subject']} {row['text']}"):
                        verified.setdefault(row["fingerprint"], row)
                matches = verified if matches is None else {key: row for key, row in matches.items() if key in verified}
            for row in (matches or {}).values():
                selected.setdefault(row["fingerprint"], row)
        return list(selected.values())

    def semantic(self, values, model, filters, limit=40):
        vector = vector_array(pack_vector(values))
        where, params = self.scope(filters)
        with self.connection() as conn:
            # Exact cosine for small indexes. Limit comparisons for large archives.
            total = len(conn.execute("SELECT passage FROM vectors WHERE model=? LIMIT 2001", (model,)).fetchall())
            candidate_clause = ""
            options = []
            if total > 2000:
                probes = [candidate for bucket in buckets(vector) for candidate in (bucket, *(bucket ^ (1 << n) for n in range(11)))]
                candidate_clause = """ AND v.passage IN (
                    SELECT passage FROM vector_buckets WHERE model=? AND bucket IN (SELECT value FROM json_each(?))
                    GROUP BY passage ORDER BY count(*) DESC,passage DESC LIMIT 2400)"""
                options = [model, json.dumps(probes)]
            vectors = conn.execute(
                f"""SELECT v.passage,v.value FROM vectors v JOIN passages p ON p.id=v.passage
                    JOIN mails m ON m.id=p.mail {JOIN} WHERE v.model=? AND {where}{candidate_clause}""",
                (model, *params, *options),
            ).fetchall()
            scores = []
            for row in vectors:
                if len(row["value"]) != len(vector) * 4:
                    continue
                similarity = sum(a * b for a, b in zip(vector, vector_array(row["value"])))
                if similarity >= .28:
                    scores.append((similarity, row["passage"]))
            ids = [passage for _, passage in sorted(scores, reverse=True)[:limit]]
            found = {row["passage"]: row for row in self.passages(ids, filters, conn)}
            weight = {passage: similarity for similarity, passage in scores}
            return [{**found[pid], "similarity": weight[pid]} for pid in ids if pid in found]

    def passages(self, passages, filters=None, conn=None):
        if conn is None:
            with self.connection() as conn:
                return self.passages(passages, filters, conn)
        where, params = self.scope(filters or {})
        rows = conn.execute(
            f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,a.name attachment
                FROM passages p JOIN mails m ON m.id=p.mail {JOIN}
                LEFT JOIN attachments a ON a.id=p.attachment
                WHERE p.id IN (SELECT value FROM json_each(?)) AND {where}""",
            (json.dumps(passages), *params),
        )
        return [dict(row) for row in rows]

    def related(self, seeds, filters):
        if not seeds:
            return []
        where, params = self.scope(filters)
        with self.connection() as conn:
            refs = defaultdict(set)
            placeholders = json.dumps(seeds)
            for row in conn.execute("SELECT id,message_id,topic,sender,recipient FROM mails WHERE id IN (SELECT value FROM json_each(?))", (placeholders,)):
                for address in re.findall(r"[\w.+-]+@[\w.-]+", row["sender"] + " " + row["recipient"]):
                    if len(row["topic"]) >= 8:
                        refs[row["topic"]].add(address.lower())
            identifiers = [
                row[0] for row in conn.execute(
                    """SELECT message_id FROM mails WHERE id IN (SELECT value FROM json_each(?)) AND message_id<>''
                       UNION SELECT parent FROM replies WHERE mail IN (SELECT value FROM json_each(?))""",
                    (placeholders, placeholders),
                )
            ]
            candidates = conn.execute(
                f"""SELECT m.id,m.message_id,m.topic,m.sender,m.recipient,
                           (SELECT min(id) FROM passages WHERE mail=m.id) passage
                    FROM mails m {JOIN} WHERE {where} AND
                    (m.message_id IN (SELECT value FROM json_each(?))
                     OR m.id IN (SELECT mail FROM replies WHERE parent IN (SELECT value FROM json_each(?)))
                     OR m.topic IN (SELECT value FROM json_each(?)))
                    ORDER BY m.sent DESC LIMIT 60""",
                (*params, json.dumps(identifiers), json.dumps(identifiers), json.dumps(list(refs))),
            ).fetchall()
            reply_ids = {row[0] for row in conn.execute(
                "SELECT mail FROM replies WHERE parent IN (SELECT value FROM json_each(?))", (json.dumps(identifiers),)
            )}
        linked = []
        for row in candidates:
            emails = set(re.findall(r"[\w.+-]+@[\w.-]+", (row["sender"] + " " + row["recipient"]).lower()))
            if row["id"] not in seeds and (row["message_id"] in identifiers or row["id"] in reply_ids or refs[row["topic"]] & emails):
                linked.append(row["passage"])
        found = {row["passage"]: row for row in self.passages(linked[:6], filters)}
        return [found[item] for item in linked[:6] if item in found]

    def neighbors(self, mail, passage, conn=None):
        """Passages of the same mail or document directly around the given passage."""
        if conn is None:
            with self.connection() as conn:
                return self.neighbors(mail, passage, conn)
        ids = [row[0] for row in conn.execute("SELECT id FROM passages WHERE mail=? ORDER BY id", (mail,))]
        try:
            index = ids.index(passage)
        except ValueError:
            return []
        near = ids[max(0, index - 1):index] + ids[index + 1:index + 2]
        return self.passages(near, conn=conn) if near else []

    def open_passage(self, passage):
        where, params = self.scope({})
        with self.connection() as conn:
            row = conn.execute(
                f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,a.name attachment,m.body
                    FROM passages p JOIN mails m ON m.id=p.mail {JOIN}
                    LEFT JOIN attachments a ON a.id=p.attachment WHERE p.id=? AND {where}""", (passage, *params)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["attachments"] = [dict(item) for item in conn.execute(
                """SELECT a.name,a.status,(SELECT min(id) FROM passages WHERE attachment=a.id AND location<>'파일명') passage
                   FROM attachments a WHERE a.mail=? ORDER BY a.id""", (row["id"],)
            )]
        return result
