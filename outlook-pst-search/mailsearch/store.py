"""Store immutable mail versions and search their locally indexed passages."""
import array
import contextlib
import hashlib
import heapq
import json
import math
import os
import sys
import re
import sqlite3
import struct
import time
import unicodedata
from collections import defaultdict, deque, OrderedDict
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from threading import Lock, RLock, Thread, local
from datetime import date, datetime, time as midnight, timedelta, timezone
from email.utils import getaddresses
from functools import lru_cache
from itertools import tee, zip_longest
from pathlib import Path

from .pcf import COMMENT_HEADER, PCF_CELL, PCF_FIELDS, case_key, cell_location, pcf_field_value
from .outlook import DEFAULT_OUTLOOK, OutlookError, outlook_store
from .extract import DOCUMENT_SUFFIXES, UNSUPPORTED_FORMAT


def embedding_text_eligible(text):
    remaining = 15
    has_text = False
    for char in text:
        remaining -= not char.isspace()
        has_text = has_text or char.isalnum()
        if remaining <= 0 and has_text:
            return True
    return False

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    active INTEGER, pending INTEGER, revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued', signature TEXT, updated TEXT,
    done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
    omitted INTEGER NOT NULL DEFAULT 0, partial INTEGER NOT NULL DEFAULT 0, issue TEXT,
    embedding_done INTEGER, embedding_total INTEGER, embedding_model_key TEXT,
    index_stage TEXT, index_done INTEGER, index_total INTEGER
);
CREATE TABLE IF NOT EXISTS mails (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source INTEGER NOT NULL REFERENCES sources(id),
    version INTEGER NOT NULL, item TEXT NOT NULL, fingerprint TEXT NOT NULL,
    folder TEXT NOT NULL, subject TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
    sent TEXT NOT NULL, message_id TEXT NOT NULL, topic TEXT NOT NULL, body TEXT NOT NULL,
    body_prefix TEXT,
    file_size INTEGER, file_mtime_ns INTEGER,
    UNIQUE(source, version, item)
);
CREATE INDEX IF NOT EXISTS mail_source_version_id ON mails(source,version,id);
CREATE INDEX IF NOT EXISTS mail_message_id ON mails(message_id);
CREATE INDEX IF NOT EXISTS mail_topic ON mails(topic);
CREATE INDEX IF NOT EXISTS mail_sent ON mails(sent DESC, id DESC);
CREATE INDEX IF NOT EXISTS mail_source_version_sent ON mails(source,version,sent DESC,id DESC);
CREATE TABLE IF NOT EXISTS replies (
    mail INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE, parent TEXT NOT NULL,
    PRIMARY KEY(mail, parent)
);
CREATE INDEX IF NOT EXISTS replies_parent ON replies(parent);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY, mail INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    name TEXT NOT NULL, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attachment_mail_status ON attachments(mail,status);
CREATE INDEX IF NOT EXISTS attachment_extraction_issues ON attachments(mail,status)
    WHERE status NOT IN ('완료','대기','미지원 형식');
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mail INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    attachment INTEGER REFERENCES attachments(id) ON DELETE CASCADE, location TEXT NOT NULL,
    start INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY, conversation TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    question TEXT NOT NULL, answer TEXT NOT NULL, scope TEXT NOT NULL, created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversation_recent ON conversations(updated DESC, id DESC);
CREATE INDEX IF NOT EXISTS conversation_turn_order ON conversation_turns(conversation, id);
CREATE TABLE IF NOT EXISTS pcf_documents (
    mail INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_mail ON passages(mail);
CREATE INDEX IF NOT EXISTS passages_filenames ON passages(mail) WHERE location='파일명';
CREATE INDEX IF NOT EXISTS passages_attachment ON passages(attachment) WHERE attachment IS NOT NULL;
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
CREATE INDEX IF NOT EXISTS vector_passage ON vectors(passage);
CREATE TABLE IF NOT EXISTS vector_buckets (
    model TEXT NOT NULL, bucket INTEGER NOT NULL, passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    PRIMARY KEY(model, bucket, passage)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS bucket_passage ON vector_buckets(passage);
CREATE TABLE IF NOT EXISTS attachment_extracts (
    digest BLOB PRIMARY KEY, locations TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_revisions (
    source INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, model TEXT NOT NULL, PRIMARY KEY(source,version,model)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS embedding_values (
    model TEXT NOT NULL, digest BLOB NOT NULL, value BLOB NOT NULL,
    PRIMARY KEY(model,digest)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS embedding_refs (
    model TEXT NOT NULL, passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    digest BLOB NOT NULL, PRIMARY KEY(model,passage),
    FOREIGN KEY(model,digest) REFERENCES embedding_values(model,digest)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS embedding_ref_passage ON embedding_refs(passage);
CREATE INDEX IF NOT EXISTS embedding_ref_value ON embedding_refs(model,digest);
CREATE TABLE IF NOT EXISTS embedding_skips (
    model TEXT NOT NULL, passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    PRIMARY KEY(model,passage)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS embedding_skip_passage ON embedding_skips(passage);
CREATE TRIGGER IF NOT EXISTS embedding_ref_remove AFTER DELETE ON embedding_refs BEGIN
    DELETE FROM embedding_values WHERE model=old.model AND digest=old.digest
      AND NOT EXISTS(SELECT 1 FROM embedding_refs r WHERE r.model=old.model AND r.digest=old.digest);
END;
CREATE VIEW IF NOT EXISTS searchable_vectors AS
    SELECT model,passage,value FROM vectors
    UNION ALL
    SELECT r.model,r.passage,v.value FROM embedding_refs r
    JOIN embedding_values v ON v.model=r.model AND v.digest=r.digest;

"""
VECTOR_LOOKUP_SQL = """SELECT passage,value FROM searchable_vectors
                       WHERE model=? AND passage IN (SELECT value FROM json_each(?))"""
# The format prefix controls adoption; duplicate results still use the content digest.
MAIL_COLUMNS = """m.id,m.source,substr(m.fingerprint,instr(m.fingerprint,':')+1) fingerprint,m.folder,
                  m.subject,m.sender,m.recipient,m.sent,m.kind,m.item,s.path,s.updated,s.kind source_kind"""
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


def contextual_model_key(model):
    return "metadata-v2:" + model if model else ""


def contextual_model_keys(model):
    return (contextual_model_key(model), "metadata-v1:" + model) if model else ("", "")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_signature(path):
    info = path.stat()
    return json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


QUESTION_PERIOD = re.compile(
    r"(?<![\w@.-])(?P<period>최근|이번\s*주|지난\s*달|올해)"
    r"(?P<particle>에|의|\s+동안|부터|까지|이랑|랑|하고|와|과|은|는|도|만|을|를)?(?![\w@.-])"
)
MAIL_DATE_HINT = re.compile(r"메일|이메일|회신|발신|수신|발송|보낸|받은")
BUSINESS_PERIOD = re.compile(r"납기|납품|출하|출고|입고|마감|업무\s*일정|매출|예산")
EMAIL_ADDRESS = r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
QUESTION_SENDER = re.compile(
    rf'(?<!\w)(?:발신자\s*[:：]\s*(?:"(?P<name>[^"\n]{{2,80}})"|(?P<address>{EMAIL_ADDRESS})|(?P<label>[가-힣]{{2,6}})(?!\w))'
    rf'|(?P<from>{EMAIL_ADDRESS})\s*[이가]?\s*보낸|["“](?P<person>[가-힣][가-힣 ]{{1,20}})["”][이가]\s*보낸)'
    r'(?![A-Za-z0-9_@.-])', re.I
)
ATTACHMENT_MAIL = re.compile(r"(?<!\w)(?:첨부(?:\s*파일|물)?(?:이|가)?\s*(?:있는|포함된)|첨부된)\s+(?:메일|이메일|회신)")
ATTACHMENT_CONTENT = re.compile(r"(?<!\w)첨부(?:\s*파일|물)(?:의|을|를)?\s*(?:내용|요약|비교|정리|검토)")


def local_midnight_utc(day):
    return datetime.combine(day, midnight()).astimezone(timezone.utc).isoformat(timespec="seconds")


def period_bounds(period, today):
    if period == "최근":
        return today - timedelta(days=29), today + timedelta(days=1)
    if period == "이번주":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=7)
    if period == "지난달":
        start = today.replace(day=1)
        return (start - timedelta(days=1)).replace(day=1), start
    return today.replace(month=1, day=1), date(today.year + 1, 1, 1)


def question_scope(question, filters, *, today=None):
    """Use explicit question metadata without altering user-set constraints."""
    scope = dict(filters)
    if scope.get("tab") in ("document", "pcf"):
        return question, scope
    removed = []
    quoted = list(re.finditer(r'["“][^"”]*["”]', question))

    def outside_quotes(match):
        return not any(start.start() < match.start() < start.end() for start in quoted)

    periods = [match for match in QUESTION_PERIOD.finditer(question) if outside_quotes(match)]
    distinct_periods = {re.sub(r"\s+", "", match["period"]) for match in periods}
    if len(distinct_periods) == 1 and not (scope.get("after") or scope.get("before")):
        first = periods[0]
        following = question[first.end():].lstrip()
        is_mail_period = MAIL_DATE_HINT.search(question) or not question[:first.start()].strip()
        business = BUSINESS_PERIOD.search(following)
        is_business_period = business and not MAIL_DATE_HINT.search(following[:business.start()])
        if (is_mail_period and not is_business_period
                and not any(match["particle"] in ("부터", "까지", "이랑", "랑", "하고", "와", "과") for match in periods)
                and not re.match(r"(?:이전|이후|전까지|부터|버전|문서|대비|기준|\d+\s*(?:일|주|달|개월|년))", following)):
            start, end = period_bounds(distinct_periods.pop(), today or date.today())
            scope.update(after=local_midnight_utc(start), before=local_midnight_utc(end))
            removed.extend(match.span() for match in periods)

    senders = [match for match in QUESTION_SENDER.finditer(question) if outside_quotes(match)]
    if len(senders) == 1 and not scope.get("sender"):
        match = senders[0]
        if not re.match(r"\s*(?:[,/]|또는|및\s)", question[match.end():]):
            address = match["address"] or match["from"]
            sender = address or (match["name"] or match["label"] or match["person"]).strip()
            if sender:
                scope["sender"] = sender
                if address:
                    scope["sender_address"] = True
                removed.append(match.span())

    if not scope.get("attachment"):
        match = next((m for m in ATTACHMENT_MAIL.finditer(question) if outside_quotes(m)
                      and not re.match(r"(?:이|은|는|을|를)?\s*(?:아닌|말고|빼고|제외)", question[m.end():])), None)
        if match:
            scope["attachment"] = "yes"
            removed.append(match.span())
        elif any(outside_quotes(m) and not re.match(r"(?:은|을|는|를)?\s*(?:빼고|제외|말고)", question[m.end():])
                 for m in ATTACHMENT_CONTENT.finditer(question)):
            scope["prefer_attachment"] = True
    for start, end in sorted(removed, reverse=True):
        question = question[:start] + " " + question[end:]
    return " ".join(question.split()) if removed else question, scope


def rewritten_scope(filters, hints):
    """Use inferred hints only where the existing search scope is unspecified."""
    scope = dict(filters)
    if scope.get("tab") in ("document", "pcf"):
        return scope
    if not (scope.get("after") or scope.get("before")):
        bounds = {}
        for field, key in (("date_from", "after"), ("date_to", "before")):
            value = hints.get(field)
            if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                try:
                    day = date.fromisoformat(value)
                    if key == "before":
                        day += timedelta(days=1)
                    bounds[key] = local_midnight_utc(day)
                except (ValueError, OverflowError, OSError):
                    bounds.clear()
                    break
        if bounds.get("after", "") < bounds.get("before", "~"):
            scope.update(bounds)
    sender = hints.get("sender")
    if not scope.get("sender") and isinstance(sender, str) and 1 < len(sender.strip()) <= 80:
        scope["sender"] = sender.strip()
        if re.fullmatch(EMAIL_ADDRESS, scope["sender"]):
            scope["sender_address"] = True
    if "attachment" not in scope and hints.get("prefer_attachment") is True:
        scope["prefer_attachment"] = True
    return scope


DECISION_SIGNAL = re.compile(
    r"철회|번복|취소|반려|승인|확정|합의|채택|보류|제안|검토|"
    r"\b(?:approv\w*|reject\w*|withdraw\w*|cancel\w*|confirm\w*|propos\w*)\b", re.I,
)


def prioritize_attachments(rows, filters):
    if filters.get("prefer_attachment"):
        return sorted(rows, key=lambda row: not (row.get("attachment") and row.get("location") != "파일명"))
    return rows


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


def summarize_participants(headers, limit=12):
    people, domains = {}, {}
    count = 0
    for row in headers:
        count += 1
        addresses, mail_domains = set(), set()
        for field in ("sender", "recipient"):
            field_value = row.get(field, "")
            entries = re.split(r'[,;\n](?=(?:[^"]*"[^"]*")*[^"]*$)', field_value)
            for entry in entries:
                entry = entry.strip()
                if not entry:
                    continue
                name, address = next(iter(getaddresses([entry])), ("", "")) if "@" in entry else (entry, "")
                address = normalized(address.strip())
                domain = address.rpartition("@")[2] if re.fullmatch(EMAIL_ADDRESS, address) else ""
                if not domain:
                    name = name or entry
                    address = ""
                key = address or normalized(name)
                if not key or key in addresses:
                    continue
                addresses.add(key)
                entry = people.setdefault(key, {"name": "", "email": address, "domain": domain})
                if name and not entry["name"]:
                    entry["name"] = name[:120]
                update_correspondence(entry, row)
                if domain and domain not in mail_domains:
                    mail_domains.add(domain)
                    update_correspondence(domains.setdefault(domain, {"domain": domain}), row)

    def ranked(values):
        return sorted(values, key=lambda item: (item["count"], item["last_sent"]), reverse=True)[:limit]
    return {"mail_count": count, "participants": ranked(people.values()), "domains": ranked(domains.values())}


def update_correspondence(entry, row):
    entry["count"] = entry.get("count", 0) + 1
    if "last_sent" not in entry or row.get("sent", "") > entry["last_sent"]:
        entry.update(last_sent=row.get("sent", ""), subject=row.get("subject", "")[:160])


def topic_key(subject):
    subject = re.sub(r"^(?:(?:re|fw|fwd|답장|전달)\s*:\s*)+", "", normalized(subject)).strip()
    return re.sub(r"\s+", " ", subject)


MAIL_INDEX_FORMAT = "sections-v4"
MAIL_INDEX_PREFIXES = (f"{MAIL_INDEX_FORMAT}:", "sections-v3:", "sections-v2:")
ACCESS_CHECK_TIMEOUT = .35
MAX_ACCESS_CHECKS = 8


def fingerprint(mail, item, *, index_format=MAIL_INDEX_FORMAT):
    attachment_names = [name for name, _ in mail["attachments"]]
    identity = [mail[key] for key in ("message_id", "subject", "sender", "recipient", "sent", "body")]
    # An attachment-bearing copy may contain a different revision under the same name.
    data = [identity, attachment_names, item if attachment_names else ""]
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()
    return f"{index_format}:{digest}" if index_format else digest


def matches_fingerprint(mail, item, stored):
    # Older active results and paused jobs can still refer to pre-versioned originals.
    index_format, separator, _ = stored.partition(":")
    return fingerprint(mail, item, index_format=index_format if separator else None) == stored


def like_prefix(prefix):
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def document_identity(doc):
    """Fingerprint basis for a document: filename and extracted content, never timestamps."""
    digest = hashlib.sha256("\n".join(f"{location}\n{text}" for location, text in doc["sections"]).encode()).hexdigest()
    if doc.get("pcf"):
        digest = hashlib.sha256((digest + json.dumps(doc["pcf"], sort_keys=True)).encode()).hexdigest()
    marker = "pcf-sections-v1" if document_kind(doc["name"]) == "pcf" else ""
    return {"message_id": marker, "subject": doc["name"], "sender": "", "recipient": "",
            "sent": "", "body": digest, "reply_ids": "", "attachments": [(doc["name"], None)]}


def document_kind(filename):
    return "pcf" if "pcf" in Path(filename).stem.lower() else "document"


def source_readable(path, kind):
    try:
        if kind == "outlook_mapi":
            return os.name == "nt" and (path == DEFAULT_OUTLOOK or Path(path.removeprefix("outlook:")).is_file())
        path = Path(path)
        if kind == "eml_dir":
            with os.scandir(path) as iterator:
                next(iterator, None)
        else:
            with path.open("rb"):
                pass
        return True
    except OSError as exc:
        if kind == "pst" and getattr(exc, "winerror", None) in {32, 33}:
            try:
                with outlook_store("outlook:" + str(path), open_only=True):
                    return True
            except OutlookError:
                pass
        return False


def document_readable(source_path, item):
    try:
        root = Path(source_path)
        resolved_root = root.resolve(strict=True)
        if root != resolved_root:
            return False
        path = (root / item).resolve(strict=True) if root.is_dir() else root
        if not path.is_file() or (root != path and not path.is_relative_to(root)):
            return False
        with path.open("rb"):
            return True
    except (OSError, RuntimeError, ValueError):
        return False


def sections(text, size=1100, *, overlap=120):
    if not 0 <= overlap < size:
        raise ValueError("section_overlap")
    position = 0
    while position < len(text):
        end = min(len(text), position + size)
        if end < len(text):
            floor = position + max(overlap + 1, size // 2)
            for separator in ("\r\n\r\n", "\n\n", "\n", " "):
                boundary = text.rfind(separator, floor, end)
                if boundary != -1:
                    end = boundary + len(separator)
                    break
        yield position, text[position:end]
        if end == len(text):
            break
        position = end - overlap


def merge_contiguous_passages(rows):
    """Merge original offset/text pairs whose ranges touch and overlap agrees."""
    pairs = iter(rows)
    first = next(pairs, None)
    if first is None:
        return None
    start, text = first
    previous = start
    for next_start, next_text in pairs:
        offset = next_start - start
        if next_start <= previous or offset > len(text):
            return None
        overlap = min(len(text) - offset, len(next_text))
        if text[offset:offset + overlap] != next_text[:overlap]:
            return None
        text += next_text[overlap:]
        previous = next_start
    return start, text


REPLY_START = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|원본\s*메시지|original appointment|원본\s*메일|전달된\s*메일|"
    r"forwarded\s+message)\s*-{2,}|on .+ wrote:)\s*$", re.I,
)
REPLY_FROM = re.compile(r"^\s*(?:From|보낸\s*사람|발신(?:자)?)\s*:", re.I)
REPLY_SENT = re.compile(r"^\s*(?:Sent|Date|보낸\s*날짜|(?:발송\s*)?일시|보낸\s*시각)\s*:", re.I)
QUOTED_LINE = re.compile(r"^\s*>+(?=\s|\w|$)")
SIGN_OFF = re.compile(r"^\s*(?:best regards|kind regards|sincerely|감사합니다)[,.!]?\s*$", re.I)
CONTACT = re.compile(r"(?:\b(?:tel|mobile|fax|email)\s*[:.]|[\w.+-]+@[\w.-]+\.[\w]+|\d{2,4}[- ]\d{3,4}[- ]\d{4})", re.I)


def mail_sections(body):
    """Yield raw offsets while excluding recognized reply and signature blocks."""
    lines = body.splitlines(keepends=True)
    offset = start = 0
    for i, line in enumerate(lines):
        boundary = (REPLY_START.match(line.rstrip("\r\n")) or line.rstrip("\r\n") == "-- "
                    or (REPLY_FROM.match(line) and any(REPLY_SENT.match(item) for item in lines[i + 1:i + 6]))
                    or (0 < len(lines) - i <= 7 and SIGN_OFF.match(line)
                        and any(CONTACT.search(item) for item in lines[i + 1:])))
        if boundary or QUOTED_LINE.match(line):
            if body[start:offset].strip():
                yield from ((start + pos, text) for pos, text in sections(body[start:offset]))
            if boundary:
                return
            start = offset + len(line)
        offset += len(line)
    if body[start:offset].strip():
        yield from ((start + pos, text) for pos, text in sections(body[start:offset]))


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


@lru_cache(maxsize=1)
def optional_numpy():
    try:
        import numpy
        return numpy
    except (ImportError, OSError, RuntimeError, ValueError):
        return None


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


def mail_body_prefix(parts, limit=1200):
    prefix = ""
    end = 0
    for start, text in parts:
        if start > end and prefix and not prefix[-1].isspace():
            prefix += "\n"
        prefix += text[max(0, end - start):][:limit - len(prefix)]
        if len(prefix) >= limit:
            break
        end = start + len(text)
    return prefix


# Part-number fragments shortlist a query without changing substring semantics.
IDENTIFIER_FRAGMENT = re.compile(r"(?=([a-z0-9][-_][a-z0-9]{6}))")


@dataclass(frozen=True, slots=True)
class MemoryMail:
    id: int
    source: int
    fingerprint: str
    topic_key: str
    folder: str
    subject: str
    sender: str
    recipient: str
    sent: str
    kind: str
    item: str
    attachment_names: tuple[str, ...]
    body_prefix: str
    subject_norm: str = field(init=False)
    sender_norm: str = field(init=False)
    recipient_norm: str = field(init=False)
    people_norm: str = field(init=False)
    sender_addresses: str = field(init=False)
    attachment_norm: str = field(init=False)
    metadata_norm: str = field(init=False)
    body_norm: str = field(init=False)

    def __post_init__(self):
        for name in ("subject", "sender", "recipient"):
            object.__setattr__(self, name + "_norm", normalized(getattr(self, name)))
        object.__setattr__(self, "people_norm", self.sender_norm + "\n" + self.recipient_norm)
        object.__setattr__(self, "sender_addresses", "," + self.sender_norm.replace(" ", "") + ",")
        object.__setattr__(self, "attachment_norm", normalized("\n".join(self.attachment_names)))
        object.__setattr__(self, "metadata_norm", "\n".join((self.subject_norm, self.people_norm, self.attachment_norm)))
        object.__setattr__(self, "body_norm", normalized(self.body_prefix))


class MemoryIndex:
    """Publish immutable per-source lists and conversation references."""

    def __init__(self):
        self.lock = RLock()
        self.sources = {}
        self.topics = {}
        self.identifiers = {}
        self.generation = 0

    @staticmethod
    def prepare(records, progress=None):
        records = tuple(sorted(records, key=lambda row: (row.sent, row.id), reverse=True))
        topics, identifiers = defaultdict(list), defaultdict(list)
        for done, row in enumerate(records, 1):
            if row.topic_key:
                topics[row.topic_key].append(row.id)
            fragments = {fragment for field in (row.metadata_norm, row.body_norm)
                         for fragment in IDENTIFIER_FRAGMENT.findall(field)}
            for fragment in fragments:
                identifiers[fragment].append(row)
            if progress and done % 256 == 0:
                progress(done)
        return records, topics, identifiers

    def replace(self, source, prepared):
        records, topics, identifiers = prepared
        with self.lock:
            self.generation += 1
            for index, value in ((self.sources, [records] if records else []), (self.topics, topics),
                                 (self.identifiers, identifiers)):
                if value:
                    index[source] = value
                else:
                    index.pop(source, None)

    def extend(self, source, prepared):
        records, topics, identifiers = prepared
        if records:
            with self.lock:
                self.generation += 1
                self.sources.setdefault(source, []).append(records)
                for index, additions in ((self.topics, topics), (self.identifiers, identifiers)):
                    groups = index.setdefault(source, defaultdict(list))
                    for key, items in additions.items():
                        groups[key].extend(items)

    def snapshot(self, source=None, fragment=None):
        with self.lock:
            if fragment is not None:
                groups = (self.identifiers.get(source, {}),) if source is not None else self.identifiers.values()
                return tuple(tuple(group.get(fragment, ())) for group in groups)
            if source is not None:
                return tuple(self.sources.get(source, ()))
            return tuple(batch for batches in self.sources.values() for batch in batches)

    def conversation(self, source, subject):
        with self.lock:
            return tuple(self.topics.get(source, {}).get(topic_key(subject), ()))

    def search(self, question, filters, *, quick, limit):
        text = normalized(question)
        words = tuple(dict.fromkeys(text.replace(",", " ").split()))
        require_all = "," in text
        fragment = None
        if len(words) == 1 or require_all:
            match = next((part for word in words if (part := IDENTIFIER_FRAGMENT.search(word))), None)
            fragment = match[1] if match else None
        with self.lock:
            groups = self.snapshot(filters.get("source") or None, fragment)
            generation = self.generation
        subject, sender, recipient = (normalized(filters.get(key, "")) for key in ("subject", "sender", "recipient"))
        body = tuple(normalized(filters.get("body", "")).replace(",", " ").split())
        tab = filters.get("tab")
        kind = tab if tab in ("mail", "document", "pcf") else None
        before, after = filters.get("before"), filters.get("after")
        attach = filters.get("attachment")
        exact_sender = filters.get("sender_address") and sender
        sender_tag, sender_address = f"<{sender}>", f",{sender},"
        regular = filters.get("regular_only")
        best = (len(words), bool(words and not quick))
        ranked = []
        for records in groups:
            for row in records:
                if (fragment is None and len(ranked) == limit and ranked[0][:2] == best
                        and (row.sent, row.id) <= ranked[0][2:]):
                    break
                if ((kind and row.kind != kind) or (regular and row.kind == "pcf")
                        or (after and row.sent < after) or (before and (not row.sent or row.sent >= before))
                        or (attach == "yes" and not row.attachment_names) or (attach == "no" and row.attachment_names)
                        or (subject and subject not in row.subject_norm) or (recipient and recipient not in row.recipient_norm)
                        or (sender and sender not in row.sender_norm)
                        or (body and not all(word in row.body_norm for word in body))):
                    continue
                if exact_sender and sender_tag not in row.sender_norm and sender_address not in row.sender_addresses:
                    continue
                body_hits = False
                if not words:
                    hits = 0
                elif len(words) == 1:
                    word = words[0]
                    body_hits = not quick and word in row.body_norm
                    if not body_hits and word not in row.metadata_norm:
                        continue
                    hits = 1
                else:
                    body_count = hits = 0
                    for word in words:
                        in_body = not quick and word in row.body_norm
                        body_count += in_body
                        hits += in_body or word in row.metadata_norm
                    if not hits or (require_all and hits != len(words)):
                        continue
                    body_hits = body_count == len(words)
                candidate = (hits, body_hits, row.sent, row.id)
                if len(ranked) < limit:
                    heapq.heappush(ranked, candidate)
                elif candidate > ranked[0]:
                    heapq.heapreplace(ranked, candidate)
        return [item[-1] for item in sorted(ranked, reverse=True)], words, generation


class Store:
    def __init__(self, root):
        if sqlite3.sqlite_version_info < (3, 42, 0):
            raise RuntimeError("SQLite 3.42 or later is required for secure FTS5 deletion.")
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        self.db = self.root / "mail.sqlite"
        self.local = local()
        self.read_pool = []
        self.read_pool_lock = Lock()
        self.read_pool_closed = False
        self.max_idle = 6
        self.vector_index = None
        self.memory_index = MemoryIndex()
        self.memory_load_lock = Lock()
        self.access_cache = OrderedDict()
        self.access_cache_lock = Lock()
        self.access_inflight = {}
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
            for column, datatype in (("embedding_done", "INTEGER"), ("embedding_total", "INTEGER"), ("embedding_model_key", "TEXT"),
                                     ("index_stage", "TEXT"), ("index_done", "INTEGER"), ("index_total", "INTEGER")):
                if column not in source_columns:
                    conn.execute(f"ALTER TABLE sources ADD COLUMN {column} {datatype}")
            if "active_signature" not in source_columns:
                conn.execute("ALTER TABLE sources ADD COLUMN active_signature TEXT")
                conn.execute("UPDATE sources SET active_signature=signature WHERE active IS NOT NULL AND pending IS NULL")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(mails)")}
            for column, datatype in (("body_prefix", "TEXT"), ("file_size", "INTEGER"), ("file_mtime_ns", "INTEGER")):
                if column not in columns:
                    conn.execute(f"ALTER TABLE mails ADD COLUMN {column} {datatype}")
            if "kind" not in columns:
                # Tab scoping (mail vs document); older rows are classified by item suffix.
                conn.execute("ALTER TABLE mails ADD COLUMN kind TEXT NOT NULL DEFAULT 'mail'")
                suffixes = ("pdf", "docx", "xlsx", "txt", "doc", "xls")
                conn.execute("UPDATE mails SET kind='document' WHERE " +
                             " OR ".join("item LIKE '%.' || ?" for _ in suffixes), suffixes)
            for table, column in (("attachments", "content_key"), ("passages", "embedding_key")):
                if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} BLOB")
            conn.execute("CREATE INDEX IF NOT EXISTS attachment_content_key ON attachments(content_key)")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS attachment_extract_remove AFTER DELETE ON attachments
                WHEN old.content_key IS NOT NULL BEGIN
                    DELETE FROM attachment_extracts WHERE digest=old.content_key
                      AND NOT EXISTS(SELECT 1 FROM attachments WHERE content_key=old.content_key);
                END""")
            conn.create_function("document_kind", 1, document_kind)
            conn.execute("UPDATE mails SET kind=document_kind(subject) WHERE kind IN ('document','pcf') "
                         "AND kind<>document_kind(subject)")
            conn.execute("INSERT INTO terms(terms,rank) VALUES('secure-delete',1)")
            if not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='quick_terms'").fetchone():
                # Build once from the saved index. The caller's transaction publishes
                # the table and its contents together.
                conn.execute("CREATE VIRTUAL TABLE quick_terms USING fts5(subject, people, label, tokenize='unicode61')")
                conn.execute("INSERT INTO quick_terms(quick_terms,rank) VALUES('secure-delete',1)")
                conn.execute('''CREATE TRIGGER quick_terms_remove AFTER DELETE ON mails BEGIN
                    DELETE FROM quick_terms WHERE rowid=old.id;
                END''')
                conn.create_function("index_text", 1, index_text)
                conn.execute('''INSERT INTO quick_terms(rowid,subject,people,label)
                    SELECT m.id,index_text(m.subject),index_text(m.sender || ' ' || m.recipient),
                           index_text(coalesce((SELECT group_concat(a.name, ' ') FROM attachments a WHERE a.mail=m.id),''))
                    FROM mails m''')

        self.reload_memory()

    @staticmethod
    def read_memory(conn, where="", params=()):
        for row in conn.execute(
            f"""SELECT m.id,m.source,substr(m.fingerprint,instr(m.fingerprint,':')+1),m.topic,
                       m.folder,m.subject,m.sender,m.recipient,m.sent,m.kind,m.item,
                       (SELECT json_group_array(a.name) FROM attachments a WHERE a.mail=m.id),
                       m.body_prefix, CASE WHEN m.body_prefix IS NULL AND m.kind='mail' THEN m.body END
                FROM sources s JOIN mails m ON m.source=s.id AND m.version=coalesce(s.active,s.pending)
                WHERE {VISIBLE_VERSION}{where}""", params,
        ):
            raw = tuple(row)
            prefix = raw[12] if raw[12] is not None else mail_body_prefix(mail_sections(raw[13] or ""))
            yield MemoryMail(*raw[:11], tuple(json.loads(raw[11])), prefix)

    def reload_memory(self, source=None):
        with self.memory_load_lock, self.connection() as conn:
            where, params = ("", ()) if source is None else (" AND s.id=?", (source,))
            by_source = defaultdict(list)
            for row in self.read_memory(conn, where, params):
                by_source[row.source].append(row)
            keys = set(self.memory_index.sources) | set(by_source) if source is None else {source}
            for key in keys:
                self.memory_index.replace(key, self.memory_index.prepare(by_source[key]))

    def append_pending_memory(self, source, ids):
        if not ids:
            return
        with self.memory_load_lock, self.connection() as conn:
            rows = self.read_memory(conn, " AND s.id=? AND s.active IS NULL AND m.id IN (SELECT value FROM json_each(?))",
                                    (source, json.dumps(ids)))
            self.memory_index.extend(source, self.memory_index.prepare(rows))

    def commit_memory_revision(self, conn, source, version, report):
        report("count_memory")
        total = conn.execute(
            "SELECT COUNT(*) FROM mails WHERE source=? AND version=?", (source, version),
        ).fetchone()[0]
        report("load_memory", 0, total)
        records = []
        for done, mail in enumerate(self.read_memory(conn, " AND s.id=?", (source,)), 1):
            records.append(mail)
            if done % 256 == 0:
                report("load_memory", done, total)
        report("load_memory", len(records), total)
        report("build_memory", 0, len(records))
        prepared = self.memory_index.prepare(records, lambda done: report("build_memory", done, len(records)))
        report("build_memory", len(records), len(records))
        report("commit")
        with self.memory_index.lock:
            report("commit")
            conn.commit()
            self.memory_index.replace(source, prepared)

    def memory_search(self, question, filters, *, quick, limit=20):
        oversample = max(40, limit)
        for _ in range(2):
            ids, words, generation = self.memory_index.search(question, filters, quick=quick, limit=oversample)
            results = []
            if ids:
                with self.connection() as conn:
                    results = self.memory_passages(conn, ids, words, limit)
            with self.memory_index.lock:
                if generation == self.memory_index.generation:
                    break
        return results

    def memory_passages(self, conn, ids, words, limit):
        parts = defaultdict(list)
        for row in conn.execute(
            f"""WITH candidates(id) AS (SELECT value FROM json_each(?)),
                      part_ids AS (
                          SELECT (SELECT min(id) FROM passages WHERE mail=c.id) id FROM candidates c
                          UNION SELECT p.id FROM candidates c JOIN passages p ON p.mail=c.id WHERE p.location='파일명'
                      )
                SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,a.name attachment
                FROM part_ids JOIN passages p ON p.id=part_ids.id JOIN mails m ON m.id=p.mail {JOIN}
                LEFT JOIN attachments a ON a.id=p.attachment WHERE {VISIBLE_VERSION} ORDER BY p.id""", (json.dumps(ids),),
        ):
            parts[row["id"]].append(row)
        sources, documents, seen, results = {}, {}, set(), []
        for mail in ids:
            choices = parts[mail]
            if not choices:
                continue
            row = next((part for part in choices if part["location"] == "파일명"
                        and any(word in normalized(part["text"]) for word in words)), choices[0])
            key = row["fingerprint"]
            if key in seen or not self.readable(row, sources, documents):
                continue
            if not self.deferred_access():
                seen.add(key)
            results.append(dict(row))
            if len(results) == limit:
                break
        return results

    @contextlib.contextmanager
    def thread_connection(self):
        if getattr(self.local, "connection_state", None) is not None:
            yield
            return
        state = {"connection": None}
        self.local.connection_state = state
        try:
            yield
        finally:
            if state["connection"] is not None:
                state["connection"].close()
            self.local.connection_state = None

    @contextlib.contextmanager
    def search_session(self, profile=None, defer_access=False):
        previous = getattr(self.local, "search", None)
        state = previous["read"] if previous else {"connection": None, "failed": False}
        self.local.search = {"profile": profile, "defer": defer_access, "read": state}
        try:
            yield
        except BaseException:
            state["failed"] = True
            raise
        finally:
            self.local.search = previous
            if previous is None:
                self.release_read_connection(state)

    def borrow_read_connection(self):
        with self.read_pool_lock:
            if self.read_pool_closed:
                raise sqlite3.ProgrammingError("Store is closed")
            if self.read_pool:
                return self.read_pool.pop()
        return self.open_connection(read_only=True)

    def release_read_connection(self, state):
        conn = state["connection"]
        if conn is None:
            return
        if not state["failed"]:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            else:
                with self.read_pool_lock:
                    if not self.read_pool_closed and len(self.read_pool) < self.max_idle:
                        self.read_pool.append(conn)
                        return
        conn.close()

    def close(self):
        with self.read_pool_lock:
            self.read_pool_closed = True
            idle, self.read_pool = self.read_pool, []
        for conn in idle:
            conn.close()

    @property
    def search_profile(self):
        state = getattr(self.local, "search", None)
        return state["profile"] if state else None

    def measure(self, stage):
        profile = self.search_profile
        return profile.measure(stage) if profile else contextlib.nullcontext()

    def deferred_access(self):
        state = getattr(self.local, "search", None)
        return bool(state and state["defer"])

    def cached_access(self, key):
        with self.access_cache_lock:
            cached = self.access_cache.get(key)
            if cached and cached[0] > time.monotonic():
                self.access_cache.move_to_end(key)
                return cached[1]
            self.access_cache.pop(key, None)
            return None

    @staticmethod
    def access_keys(row):
        source_key = ("source", row["source"], row["path"], row["source_kind"])
        document_key = ("document", row["source"], row["path"], row["item"])
        return source_key, document_key

    def check_access(self, key):
        kind, _, path, detail = key
        check = source_readable if kind == "source" else document_readable
        decision = check(path, detail)
        # This lease applies only to search results. Opening or sending evidence rechecks.
        with self.access_cache_lock:
            self.access_cache[key] = (time.monotonic() + (10 if decision else 2), decision)
            self.access_cache.move_to_end(key)
            if len(self.access_cache) > 2048:
                self.access_cache.popitem(last=False)
        return decision

    def start_access_probe(self, key):
        with self.access_cache_lock:
            if key in self.access_inflight:
                return self.access_inflight[key]
            if len(self.access_inflight) >= MAX_ACCESS_CHECKS:
                return None
            future = Future()
            self.access_inflight[key] = future

        def run():
            try:
                decision = self.check_access(key)
            except (OSError, RuntimeError, ValueError):
                decision = False
            finally:
                with self.access_cache_lock:
                    self.access_inflight.pop(key, None)
            future.set_result(decision)

        try:
            # A blocked Windows share call cannot hold the HTTP request or app exit open.
            Thread(target=run, name="search-access", daemon=True).start()
        except RuntimeError:
            with self.access_cache_lock:
                self.access_inflight.pop(key, None)
            future.set_result(False)
        return future

    def prefetch_access(self, keys, *, refresh, deadline=None):
        decisions = {key: None if refresh else self.cached_access(key) for key in keys}
        remaining = deque(key for key, decision in decisions.items() if decision is None)
        if deadline is None:
            deadline = time.monotonic() + ACCESS_CHECK_TIMEOUT
        pending = {}
        while remaining or pending:
            while remaining and time.monotonic() < deadline:
                key = remaining[0]
                future = self.start_access_probe(key)
                if future is None:
                    break
                pending[future] = key
                remaining.popleft()
            if not pending:
                break
            done, _ = wait(pending, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
            for future in done:
                decisions[pending.pop(future)] = future.result()
            if time.monotonic() >= deadline:
                break
        return decisions

    def prefetch_search_access(self, rows, *, refresh=False):
        entries = [(self.access_keys(row), row["kind"]) for row in rows]
        sources = self.prefetch_access((keys[0] for keys, _ in entries), refresh=refresh)
        documents = self.prefetch_access((keys[1] for keys, kind in entries
                                          if kind != "mail" and sources[keys[0]] is True), refresh=refresh)
        pending = None in sources.values() or None in documents.values()
        return sources, documents, pending

    def result_access(self, row, sources, documents, *, cached_only=False, refresh=False):
        source_key, doc_key = self.access_keys(row)

        def verify(key, memo):
            if key not in memo:
                decision = None if refresh else self.cached_access(key)
                if decision is None and not cached_only:
                    with self.measure("access"):
                        decision = self.check_access(key)
                memo[key] = decision
            return memo[key]

        readable = verify(source_key, sources)
        if readable is not True or row["kind"] == "mail":
            return readable
        return verify(doc_key, documents)

    def readable(self, row, sources, documents):
        return self.deferred_access() or self.result_access(row, sources, documents, refresh=True)

    def visible_rows(self, rows, limit=None):
        sources, documents = {}, {}
        visible = []
        for row in rows:
            if self.readable(row, sources, documents):
                visible.append(dict(row))
                if len(visible) == limit:
                    break
        return visible

    def open_connection(self, *, read_only=False):
        if read_only:
            conn = sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True,
                                   timeout=10, check_same_thread=False)
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA cache_size=-131072")
        conn.execute("PRAGMA mmap_size=536870912")
        conn.execute("PRAGMA temp_store=MEMORY")

        conn.create_function("pcf_field_matches", 4, pcf_field_value)
        conn.create_function("embedding_text_eligible", 1, embedding_text_eligible, deterministic=True)
        return conn

    @contextlib.contextmanager
    def connection(self):
        search = getattr(self.local, "search", None)
        if search is not None:
            read = search["read"]
            if read["connection"] is None:
                read["connection"] = self.borrow_read_connection()
            conn = read["connection"]
            try:
                yield conn
            except sqlite3.Error:
                if read["connection"] is conn:
                    conn.close()
                    read["connection"] = None
                raise
            return
        state = getattr(self.local, "connection_state", None)
        if state is None:
            conn = self.open_connection()
        else:
            if state["connection"] is None:
                state["connection"] = self.open_connection()
            conn = state["connection"]
        try:
            with conn:
                yield conn
        finally:
            if state is None:
                conn.close()

    def source(self, source):
        with self.connection() as conn:
            return conn.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone()

    @staticmethod
    def source_extraction_issues(conn):
        # Poll over problem rows and the sparse attachment index, including only
        # the revision being shown on each card.
        return conn.execute(
            """WITH failed_files AS (
                   SELECT p.source,p.code reason,
                          CASE
                              WHEN p.code IN ('parse','mail_original_changed') THEN 'mail'
                              WHEN p.code IN ('pst_in_use','pst_access_denied','pst_changed','pst_no_mail',
                                              'pst_export','pst_invalid') THEN 'pst'
                              ELSE 'document'
                          END kind
                   FROM problems p JOIN sources s ON s.id=p.source AND p.version=coalesce(s.pending,s.active)
                   WHERE p.code<>? AND (p.code<>'mail_original_changed' OR NOT EXISTS (
                       SELECT 1 FROM mails m WHERE m.source=p.source AND m.version=p.version AND m.item=p.item))
               ), extracted AS (
                   SELECT m.source,a.status reason,
                          CASE WHEN m.kind='mail' THEN 'attachment' ELSE 'document' END kind,
                          EXISTS(SELECT 1 FROM passages p WHERE p.attachment=a.id AND p.location<>'파일명') has_text
                   FROM attachments a
                   CROSS JOIN mails m ON m.id=a.mail
                   JOIN sources s ON s.id=m.source AND m.version=coalesce(s.pending,s.active)
                   WHERE a.status NOT IN ('완료','대기','미지원 형식')
               ), issues AS (
                   SELECT source,kind,reason,0 partial FROM failed_files
                   UNION ALL
                   SELECT source,kind,reason,has_text partial FROM extracted
                   WHERE NOT has_text OR reason LIKE '일부 색인%'
               )
               SELECT source,kind,reason,partial,count(*) count FROM issues
               GROUP BY source,kind,reason,partial ORDER BY count DESC,kind,reason""",
            (UNSUPPORTED_FORMAT,),
        )

    def sources(self):
        with self.connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            # Per-kind item counts feed the sidebar's mail/document/PCF sections.
            counts = defaultdict(lambda: defaultdict(int))
            revision_counts = defaultdict(dict)
            for row in conn.execute(
                """SELECT m.source,m.version,m.kind,COUNT(*) n FROM sources s
                   CROSS JOIN mails m ON m.source=s.id AND m.version IN (s.active,s.pending)
                   GROUP BY m.source,m.version,m.kind"""
            ):
                counts[row["source"]][row["kind"]] += row["n"]
                revision_counts[row["source"], row["version"]][row["kind"]] = row["n"]
            issue_counts = defaultdict(lambda: defaultdict(int))
            issue_reasons = defaultdict(list)
            for source, kind, reason, partial, count in self.source_extraction_issues(conn):
                issue_counts[source, partial][kind] += count
                reasons = issue_reasons[source, partial]
                if len(reasons) < (6 if partial else 8):
                    reasons.append({"kind": kind, "reason": reason, "count": count})
            sources = []
            for row in conn.execute("SELECT * FROM sources ORDER BY id"):
                version = row["pending"] or row["active"]
                problems = conn.execute("""SELECT item,code FROM problems WHERE source=? AND version=? AND code<>?
                    ORDER BY code IN ('pst_in_use','pst_access_denied') DESC,item LIMIT 5""",
                    (row["id"], version, UNSUPPORTED_FORMAT)).fetchall()
                deferred_psts = conn.execute("""SELECT COUNT(*) FROM problems WHERE source=? AND version=?
                    AND code='pst_in_use'""", (row["id"], version)).fetchone()[0]
                deferred_names = [
                    item.rsplit("/", 1)[-1] for item, in conn.execute(
                        """SELECT item FROM problems WHERE source=? AND version=? AND code='pst_in_use'
                           ORDER BY item LIMIT 20""", (row["id"], version),
                    )
                ] if deferred_psts else []
                sources.append({**dict(row), "counts": counts.get(row["id"], {}),
                                "index_counts": revision_counts.get((row["id"], version), {}),
                                "omitted_by_kind": issue_counts[row["id"], 0],
                                "partial_by_kind": issue_counts[row["id"], 1],
                                "omitted_reasons": issue_reasons[row["id"], 0],
                                "partial_reasons": issue_reasons[row["id"], 1],
                                "deferred_psts": deferred_psts, "deferred_pst_names": deferred_names,
                                "problems": [dict(problem) for problem in problems]})
            return sources

    def allowed_sources(self, source=None, *, sources=None):
        if sources is None:
            with self.connection() as conn:
                sources = conn.execute("SELECT id,path,kind FROM sources WHERE ? IS NULL OR id=?", (source, source)).fetchall()
        ids = []
        for row in sources:
            if source is not None and row["id"] != source:
                continue
            if source_readable(row["path"], row["kind"]):
                ids.append(row["id"])
        return ids

    def list_conversations(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,title,updated FROM conversations ORDER BY updated DESC,id DESC LIMIT 30",
            )]

    def conversation_turns(self, key, *, limit=-1):
        with self.connection() as conn:
            turns = conn.execute(
                """SELECT question,answer,scope,created FROM conversation_turns
                   WHERE conversation=? ORDER BY id DESC LIMIT ?""", (key, limit),
            ).fetchall()
        return [{**row, "answer": json.loads(row["answer"]), "scope": json.loads(row["scope"])}
                for row in reversed(turns)]

    def save_conversation_turn(self, key, question, answer, scope):
        # Answer handlers use a pooled read-only connection for retrieval.
        with contextlib.closing(self.open_connection()) as conn, conn:
            created = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            conn.execute("""INSERT INTO conversations(id,title,updated) VALUES(?,?,?)
                            ON CONFLICT(id) DO UPDATE SET updated=excluded.updated""", (key, question, created))
            conn.execute("""INSERT INTO conversation_turns(conversation,question,answer,scope,created)
                            VALUES(?,?,?,?,?)""",
                         (key, question, json.dumps(answer, ensure_ascii=False), json.dumps(scope), created))
            conn.execute("""DELETE FROM conversations WHERE id IN (
                            SELECT id FROM conversations ORDER BY updated DESC,id DESC LIMIT -1 OFFSET 30)""")

    def delete_conversation(self, key):
        with self.connection() as conn:
            return bool(conn.execute("DELETE FROM conversations WHERE id=?", (key,)).rowcount)

    def participant_summary(self, query_terms, filters, limit=12, *, seed_ids=None):
        """Summarize scoped mail headers, including scoped neighbors of the strongest matches."""
        limit = max(0, min(limit, 30))
        if isinstance(query_terms, str):
            query_terms = re.findall(r"[\w@.+-]+", query_terms)
        terms = list(dict.fromkeys(normalized(term.strip()) for term in query_terms if term.strip()))[:12]
        if seed_ids is not None:
            seed_ids = list(seed_ids)
        where, params = self.scope(filters, seed_ids or ())
        if seed_ids is not None:
            matched = "1" if seed_ids else "0"
            match_params = []
        elif terms:
            matched = """EXISTS (SELECT 1 FROM json_each(?) term WHERE instr(norm(m.subject),term.value)>0
                         OR instr(norm(m.sender),term.value)>0 OR instr(norm(m.recipient),term.value)>0)"""
            match_params = [json.dumps(terms)]
        else:
            matched, match_params = "0", []
        if not limit:
            return summarize_participants(())
        fields = "m.id,m.subject,m.sender,m.recipient,m.sent,m.source,m.item,m.kind,s.path,s.kind AS source_kind"
        with self.connection() as conn:
            conn.create_function("norm", 1, normalized, deterministic=True)
            rows = conn.execute(f"""SELECT {fields} FROM mails m {JOIN}
                                    WHERE {where} AND m.kind='mail' AND {matched}
                                    ORDER BY m.sent DESC,m.id DESC""", (*params, *match_params))
            sources, documents = {}, {}
            headers = {row["id"]: dict(row) for row in rows
                       if self.result_access(row, sources, documents, refresh=True) is True}
            related = self.related_mail_ids(list(headers)[:8], filters, hops=3)
            if related:
                where, params = self.scope(filters, related)
                rows = conn.execute(f"SELECT {fields} FROM mails m {JOIN} WHERE {where} AND m.kind='mail'", params)
                headers.update((row["id"], dict(row)) for row in rows
                               if self.result_access(row, sources, documents, refresh=True) is True)
        return summarize_participants(headers.values(), limit)

    def scope(self, filters, extra=()):
        clauses = [VISIBLE_VERSION]
        params = []
        if filters.get("source"):
            clauses.append("m.source=?")
            params.append(filters["source"])
        if "vector_sources" in filters:
            clauses.append("m.source IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(filters["vector_sources"]))
        if filters.get("regular_only"):
            clauses.append("m.kind IN ('mail','document')")
        for name, col in {"sender": "m.sender", "recipient": "m.recipient", "subject": "m.subject"}.items():
            if filters.get(name):
                if name == "sender" and filters.get("sender_address"):
                    clauses.append("""(instr(lower(m.sender), '<' || lower(?) || '>')>0 OR
                        instr(',' || replace(lower(m.sender),' ','') || ',', ',' || lower(?) || ',')>0)""")
                    params.extend((filters[name], filters[name]))
                else:
                    clauses.append(f"instr(lower({col}),lower(?))>0")
                    params.append(filters[name])
        if filters.get("body"):
            clauses.append("""((m.kind='mail' AND instr(lower(m.body),lower(?))>0) OR
                (m.kind<>'mail' AND EXISTS(SELECT 1 FROM passages bp WHERE bp.mail=m.id AND instr(lower(bp.text),lower(?))>0)))""")
            params.extend([filters["body"]] * 2)
        if filters.get("attachment") in ("yes", "no"):
            negate = "" if filters["attachment"] == "yes" else "NOT "
            clauses.append(f"{negate}EXISTS(SELECT 1 FROM attachments a WHERE a.mail=m.id)")
        if filters.get("tab") in ("mail", "document", "pcf"):
            clauses.append("m.kind=?")
            params.append(filters["tab"])
        for name in PCF_FIELDS:
            if filters.get(name):
                clauses.append("""(m.kind='pcf' AND (EXISTS(
                    SELECT 1 FROM passages fp WHERE fp.mail=m.id AND pcf_field_matches(?,'',fp.text,?))
                    OR (pcf_field_matches(?,m.subject,'',?) AND NOT EXISTS(
                    SELECT 1 FROM passages fp WHERE fp.mail=m.id AND pcf_field_matches(?,'',fp.text,NULL)))))""")
                params.extend((name, filters[name], name, filters[name], name))
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
        segments, preview = tee(mail_sections(mail["body"]))
        cursor = conn.execute(
            """INSERT INTO mails(source,version,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body,body_prefix)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, version, item, fingerprint(mail, item), *(mail[k] for k in ("folder", "subject", "sender", "recipient", "sent", "message_id")),
             topic_key(mail["subject"]), mail["body"], mail_body_prefix(preview)),
        )
        mail_id = cursor.lastrowid
        parents = list(dict.fromkeys(re.findall(r"<[^<>\s]+>", mail["reply_ids"])))[:100]
        conn.executemany("INSERT INTO replies(mail,parent) VALUES(?,?)", ((mail_id, x) for x in parents))
        wrote_body = False
        del preview
        for start, text in segments:
            self.add_passage(conn, mail_id, mail, None, "", "본문", start, text)
            wrote_body = True
        if not wrote_body:
            for start, text in sections(mail["subject"] or "본문 없음"):
                self.add_passage(conn, mail_id, mail, None, "", "제목", start, text)
        for name, _ in mail["attachments"]:
            status = "대기" if Path(name).suffix.casefold() in DOCUMENT_SUFFIXES else UNSUPPORTED_FORMAT
            attachment_id = conn.execute(
                "INSERT INTO attachments(mail,name,status) VALUES(?,?,?)", (mail_id, name, status)
            ).lastrowid
            self.add_passage(conn, mail_id, mail, attachment_id, name, "파일명", 0, name)
        self.add_quick_terms(conn, mail_id, mail)
        return mail_id

    @staticmethod
    def add_quick_terms(conn, mail_id, mail):
        names = " ".join(name for name, _ in mail["attachments"])
        conn.execute(
            "INSERT INTO quick_terms(rowid,subject,people,label) VALUES(?,?,?,?)",
            (mail_id, index_text(mail["subject"]), index_text(mail["sender"] + " " + mail["recipient"]),
             index_text(names)),
        )

    def add_passage(self, conn, mail_id, mail, attachment, name, location, start, text,
                    search_label=None, embedding_key=None):
        passage_id = conn.execute(
            "INSERT INTO passages(mail,attachment,location,start,text,embedding_key) VALUES(?,?,?,?,?,?)",
            (mail_id, attachment, location, start, text, embedding_key),
        ).lastrowid
        conn.execute(
            "INSERT INTO terms(rowid,subject,people,label,text) VALUES(?,?,?,?,?)",
            (passage_id, index_text(mail["subject"]), index_text(mail["sender"] + " " + mail["recipient"]),
             index_text(search_label or name), index_text(text)),
        )

    def add_document(self, conn, source, version, item, doc):
        """Index a standalone document: one archive row with located passages."""
        mail = document_identity(doc)
        cursor = conn.execute(
            """INSERT INTO mails(source,version,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body,kind)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, version, item, fingerprint(mail, item), doc["folder"], doc["name"], "", "",
             doc["sent"], "", topic_key(doc["name"]), "",
             document_kind(doc["name"])),
        )
        mail_id = cursor.lastrowid
        attachment_id = conn.execute(
            "INSERT INTO attachments(mail,name,status) VALUES(?,?,?)", (mail_id, doc["name"], doc["status"])
        ).lastrowid
        if doc.get("pcf"):
            structure = doc["pcf"]
            conn.execute("INSERT INTO pcf_documents VALUES(?,?)",
                         (mail_id, json.dumps(structure, ensure_ascii=False)))
            for comment in structure.get("comments", []):
                for row in comment["rows"]:
                    for side in ("owner", "yard"):
                        cell = row[side]
                        if cell is not None:
                            location = cell_location(comment, side, cell)
                            label = f'{doc["name"]} COMMENT NO. {comment["number"]} {cell["label"]} {side}'
                            self.add_passage(conn, mail_id, mail, attachment_id, doc["name"], location, 0,
                                             cell["text"], search_label=label)
        is_pcf = document_kind(doc["name"]) == "pcf"
        for location, text in doc["sections"]:
            offsets = [0]
            if is_pcf:
                offsets.extend(match.start(1) for match in COMMENT_HEADER.finditer(text) if match.start(1))
            for begin, end in zip(offsets, [*offsets[1:], len(text)]):
                chunks = sections(text[begin:end], 1100, overlap=120) if is_pcf else sections(text[begin:end])
                for start, segment in chunks:
                    self.add_passage(conn, mail_id, mail, attachment_id, doc["name"], location, begin + start, segment)
        self.add_quick_terms(conn, mail_id, mail)
        return mail_id

    def adopt_mail(self, conn, version, old_id):
        """Copy an imported mail with its index and embeddings into a new version."""
        new_id = conn.execute(
            """INSERT INTO mails(source,version,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body,kind,
                                 body_prefix,file_size,file_mtime_ns)
               SELECT source,?,item,fingerprint,folder,subject,sender,recipient,sent,message_id,topic,body,kind,
                      body_prefix,file_size,file_mtime_ns
               FROM mails WHERE id=?""", (version, old_id)
        ).lastrowid
        conn.execute(
            "INSERT INTO quick_terms(rowid,subject,people,label) SELECT ?,subject,people,label FROM quick_terms WHERE rowid=?",
            (new_id, old_id),
        )
        conn.execute("INSERT INTO replies(mail,parent) SELECT ?,parent FROM replies WHERE mail=?", (new_id, old_id))
        conn.execute("INSERT INTO pcf_documents SELECT ?,data FROM pcf_documents WHERE mail=?", (new_id, old_id))
        attachments = {}
        for row in conn.execute("SELECT id,name,status,content_key FROM attachments WHERE mail=? ORDER BY id", (old_id,)):
            attachments[row["id"]] = conn.execute(
                "INSERT INTO attachments(mail,name,status,content_key) VALUES(?,?,?,?)", (new_id, row["name"], row["status"], row["content_key"])
            ).lastrowid
        for row in conn.execute("SELECT id,attachment,location,start,text,embedding_key FROM passages WHERE mail=? ORDER BY id", (old_id,)):
            passage = conn.execute(
                "INSERT INTO passages(mail,attachment,location,start,text,embedding_key) VALUES(?,?,?,?,?,?)",
                (new_id, attachments.get(row["attachment"]), row["location"], row["start"], row["text"], row["embedding_key"]),
            ).lastrowid
            conn.execute(
                "INSERT INTO terms(rowid,subject,people,label,text) SELECT ?,subject,people,label,text FROM terms WHERE rowid=?",
                (passage, row["id"]),
            )
            conn.execute("INSERT INTO vectors(passage,model,value) SELECT ?,model,value FROM vectors WHERE passage=?", (passage, row["id"]))
            conn.execute(
                "INSERT INTO embedding_refs(model,passage,digest) SELECT model,?,digest FROM embedding_refs WHERE passage=?",
                (passage, row["id"]),
            )
            conn.execute(
                "INSERT INTO vector_buckets(model,bucket,passage) SELECT model,bucket,? FROM vector_buckets WHERE passage=?",
                (passage, row["id"]),
            )
        return new_id

    def try_adopt(self, conn, source, previous, version, item, mail):
        """Reuse the previous version's work when the mail content is unchanged."""
        old = conn.execute(
            "SELECT id,fingerprint FROM mails WHERE source=? AND version=? AND item=?",
            (source, previous, item),
        ).fetchone()
        if (old is None or (":" in old["fingerprint"] and not old["fingerprint"].startswith(MAIL_INDEX_PREFIXES))
                or not matches_fingerprint(mail, item, old["fingerprint"])):
            return False
        self.adopt_mail(conn, version, old["id"])
        return True

    @staticmethod
    def mail_identities(conn, source, version):
        return {row["item"]: row for row in conn.execute(
            """SELECT id,item,folder,fingerprint,file_size,file_mtime_ns FROM mails
               WHERE source=? AND version=? AND kind='mail'""", (source, version),
        )} if version is not None else {}

    def carry_prefix(self, conn, source, previous, version, prefix):
        """Adopt previous-version mails under a PST prefix once per pending version."""
        rows = conn.execute(
            """SELECT old.id FROM mails old WHERE old.source=? AND old.version=? AND old.item LIKE ? ESCAPE '\\'
               AND NOT EXISTS (SELECT 1 FROM mails new WHERE new.source=old.source AND new.version=? AND new.item=old.item)""",
            (source, previous, like_prefix(prefix), version),
        ).fetchall()
        for row in rows:
            self.adopt_mail(conn, version, row["id"])
        return len(rows)

    def pst_files(self, source):
        with self.connection() as conn:
            return {row["label"]: row["signature"] for row in
                    conn.execute("SELECT label,signature FROM pst_files WHERE source=?", (source,))}

    def outdated_pst_labels(self, source, version, labels):
        """Find PSTs whose active mail passages predate the current format."""
        if version is None or not labels:
            return set()
        labels = set(labels)
        outdated = set()
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT item FROM mails WHERE source=? AND version=? AND kind='mail'
                   AND substr(fingerprint,1,?) NOT IN (SELECT value FROM json_each(?))""",
                (source, version, len(MAIL_INDEX_PREFIXES[0]), json.dumps(MAIL_INDEX_PREFIXES)),
            )
            for row in rows:
                if "" in labels:
                    return {""}
                item = row["item"]
                for position in re.finditer("/", item):
                    prefix = item[:position.end()]
                    if prefix in labels:
                        outdated.add(prefix)
                        break
                if outdated == labels:
                    break
        return outdated

    def save_pst_files(self, conn, source, signatures):
        conn.execute("DELETE FROM pst_files WHERE source=?", (source,))
        conn.executemany("INSERT INTO pst_files VALUES(?,?,?)", ((source, *item) for item in signatures.items()))

    def suggest(self, prefix, filters, limit=8):
        prefix = normalized(prefix.strip())[:450]
        limit = min(8, max(0, limit))
        if len(prefix) < 2 or not limit or "\x00" in prefix or not WORD.search(prefix):
            return []

        deadline = time.monotonic() + .028
        sql_deadline = deadline - .008  # Leave time for bounded original-access checks.
        match = re.compile(r"(?<!\w)" + re.escape(prefix))
        where, params = self.scope(filters)
        columns = "m.subject,m.sender,m.recipient,m.kind,m.source,m.item,s.path,s.kind source_kind"
        escaped_prefix = prefix.replace('"', '""')
        queries = (
            (f"""SELECT {columns} FROM quick_terms JOIN mails m ON m.id=quick_terms.rowid {JOIN}
                 WHERE quick_terms MATCH ? AND {where} ORDER BY quick_terms.rowid DESC LIMIT 96""",
             [f'{{subject people}} : "{escaped_prefix}"*', *params]),
            (f"""SELECT {columns} FROM mails m {JOIN} WHERE {where}
                 ORDER BY m.sent DESC,m.id DESC LIMIT 96""", params),
        )
        candidates = []
        seen_rows = set()
        try:
            with self.search_session(), self.connection() as conn:
                conn.execute("PRAGMA busy_timeout=0")
                conn.set_progress_handler(lambda: time.monotonic() >= sql_deadline, 256)
                try:
                    for statement, args in queries:
                        if time.monotonic() >= sql_deadline:
                            break
                        for row in conn.execute(statement, args):
                            if time.monotonic() >= sql_deadline:
                                break
                            key = row["source"], row["item"]
                            if key in seen_rows:
                                continue
                            seen_rows.add(key)
                            kind = "subject" if row["kind"] == "mail" else "document"
                            labels = [(row["subject"], kind)]
                            for key in ("sender", "recipient"):
                                people = row[key][:4096]
                                if "@" in people or "<" in people:
                                    for name, address in getaddresses(people.replace("\n", ",").split(";")):
                                        labels.extend(((name, "sender"), (address, "sender")))
                                else:
                                    labels.extend((name, "sender") for name in re.split(r"[,;\n]", people))
                            matching = [(label, kind) for value, kind in labels
                                        if (label := " ".join(value.split())[:450]) and match.search(normalized(label))]
                            if matching:
                                candidates.append((row, matching))
                finally:
                    conn.set_progress_handler(None, 0)
                    conn.execute("PRAGMA busy_timeout=10000")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode not in (sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise

        access = [(self.access_keys(row), row["kind"]) for row, _ in candidates]
        sources = self.prefetch_access((keys[0] for keys, _ in access), refresh=False, deadline=deadline)
        documents = self.prefetch_access((keys[1] for keys, kind in access
                                          if kind != "mail" and sources[keys[0]]), refresh=False, deadline=deadline)
        results = {}
        for (keys, kind), (_, labels) in zip(access, candidates, strict=True):
            if not sources[keys[0]] or (kind != "mail" and not documents[keys[1]]):
                continue
            for label, category in labels:
                results.setdefault(normalized(label), {"label": label, "query": label, "kind": category})
                if len(results) == limit:
                    return list(results.values())
        return list(results.values())

    def quick_search(self, question, filters, limit=20):
        """Search one metadata row per message, then verify access to ranked results."""
        where, params = self.scope(filters)
        if question:
            words = query_words(question)
            if not words:
                return []
            expressions = dict.fromkeys(fts_query(words, mode) for mode in ("terms", "grams"))
            from_clause = f"quick_terms JOIN mails m ON m.id=quick_terms.rowid {JOIN}"
            where = "quick_terms MATCH ? AND " + where
            params = [" OR ".join(expressions), *params]
            order = "bm25(quick_terms,7.0,3.0,5.0),m.sent DESC,m.id DESC"
        else:
            words = []
            from_clause = f"mails m {JOIN}"
            order = "m.sent DESC,m.id DESC"

        with self.connection() as conn:
            candidates = conn.execute(
                f"""SELECT {MAIL_COLUMNS} FROM {from_clause}
                    WHERE {where} ORDER BY {order}""", params,
            )
            selected = []
            seen = set()
            sources, documents = {}, {}
            try:
                for candidate in candidates:
                    key = candidate["fingerprint"]
                    if key in seen or not self.readable(candidate, sources, documents):
                        continue
                    if not self.deferred_access():
                        seen.add(key)
                    selected.append(candidate)
                    if len(selected) == limit:
                        break
            finally:
                candidates.close()
            parts = defaultdict(list)
            for row in conn.execute(
                """WITH selected AS (SELECT value mail FROM json_each(?)),
                          part_ids AS (
                              SELECT (SELECT min(id) FROM passages WHERE mail=s.mail) id FROM selected s
                              UNION
                              SELECT p.id FROM selected s JOIN passages p ON p.mail=s.mail
                              WHERE p.location='파일명'
                          )
                   SELECT p.mail mail_id,p.id passage,p.text,p.location,p.start,a.name attachment
                   FROM part_ids JOIN passages p ON p.id=part_ids.id
                   LEFT JOIN attachments a ON a.id=p.attachment
                   ORDER BY p.id""", (json.dumps([row["id"] for row in selected]),),
            ):
                parts[row["mail_id"]].append(row)
            results = []
            for row in selected:
                choices = parts[row["id"]]
                if choices:
                    part = next((item for item in choices if item["location"] == "파일명"
                                 and any(word in normalized(item["text"]) for word in words)), choices[0])
                    results.append({**dict(row), **dict(part)})
        return results

    def scoped_lexical(self, conn, statement, filters, columns, window):
        # Restrict a single FTS scan by the shortlisted rowid range. Reopening
        # an FTS cursor per row would recompute global BM25 term statistics.
        coarse = {key: filters[key] for key in ("source", "after", "before") if filters.get(key)}
        base, base_params = self.scope(coarse)
        where, params = self.scope(filters)
        if coarse.get("source"):
            mail_range = f"""SELECT m.id FROM sources s CROSS JOIN mails m INDEXED BY mail_source_version_sent
                             ON m.source=s.id AND m.version=coalesce(s.active,s.pending)
                             WHERE s.id=? AND {base} ORDER BY m.sent DESC,m.id DESC LIMIT 4096"""
            base_params = [coarse["source"], *base_params]
        elif not coarse and filters.get("sender") and (words := query_words(filters["sender"])):
            mail_range = """SELECT rowid id FROM quick_terms WHERE quick_terms MATCH ? LIMIT 4096"""
            base_params = ["people : (" + fts_query(words, "grams") + ")"]
        else:
            mail_range = f"""SELECT m.id FROM mails m INDEXED BY mail_sent CROSS JOIN sources s ON s.id=m.source
                             WHERE {base} ORDER BY m.sent DESC,m.id DESC LIMIT 4096"""
        return conn.execute(
            f"""WITH mail_range AS MATERIALIZED ({mail_range}), shortlist AS MATERIALIZED (
                    SELECT p.id FROM mail_range r CROSS JOIN mails m ON m.id=r.id
                    CROSS JOIN sources s ON s.id=m.source CROSS JOIN passages p ON p.mail=m.id
                    WHERE {where} LIMIT 12000),
                bounds AS (SELECT min(id) first,max(id) last FROM shortlist),
                matches AS MATERIALIZED (
                    SELECT terms.rowid id,bm25(terms,7.0,2.0,4.0,1.0) score
                    FROM bounds CROSS JOIN terms ON terms MATCH ? AND terms.rowid BETWEEN bounds.first AND bounds.last
                    ORDER BY terms.rowid DESC LIMIT 16000)
                SELECT {columns} FROM matches f CROSS JOIN passages p ON p.id=f.id
                CROSS JOIN mails m ON m.id=p.mail {JOIN}
                WHERE f.id IN (SELECT id FROM shortlist) ORDER BY f.score,p.id LIMIT ?""",
            (*base_params, *params, statement, window),
        )

    def lexical(self, question, filters, fuzzy=False, limit=100, *, required=()):
        where, params = self.scope(filters)
        words = query_words(question, fuzzy)
        if not question.strip():
            with self.connection() as conn:
                rows = conn.execute(
                    f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,NULL attachment
                        FROM mails m {JOIN} JOIN passages p ON p.id=(SELECT min(id) FROM passages WHERE mail=m.id)
                        WHERE {where} ORDER BY m.sent DESC,m.id DESC""", params,
                )
                try:
                    return self.visible_rows(rows, limit)
                finally:
                    rows.close()
        if not words:
            return []
        expanded = list(dict.fromkeys([*words, *(word for key, alternatives in SYNONYMS.items() if fuzzy and key in question for word in alternatives)]))
        ranked = {}
        exact_identifier = len(words) == 1 and bool(re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)+", words[0]))
        term_statement = fts_query(words, "terms")
        gram_statement = fts_query(expanded, "grams")
        strategies = [(term_statement, 1.5), (gram_statement, 1.0)]
        if term_statement == gram_statement:
            strategies = [(term_statement, 2.5)]
        must_match = []
        for keyword in required:
            tokens = query_words(keyword, fuzzy=True)
            for token in tokens:
                variants = dict.fromkeys(fts_query([token], strategy) for strategy in ("terms", "grams"))
                must_match.append("(" + " OR ".join(variants) + ")")
        if must_match:
            strategies = [(" AND ".join([f"({statement})", *must_match]), weight) for statement, weight in strategies]
        filtered = bool(params or filters.get("regular_only"))
        window = max(limit * 3, 256)
        if filtered:
            window = max(window, min(2000, max(1024, limit * 6)))
        with self.connection() as conn:
            for position, (statement, weight) in enumerate(strategies):
                if position and exact_identifier and ranked and not fuzzy:
                    break
                # Keep the rank sort narrow and hydrate only surviving IDs.
                defer = self.deferred_access()
                columns = "p.id" if defer else "p.id,m.source,m.kind,m.item,s.path,s.kind source_kind"
                candidates = conn.execute(
                    "SELECT rowid FROM terms WHERE terms MATCH ? ORDER BY bm25(terms,7.0,2.0,4.0,1.0) LIMIT ?",
                    (statement, window),
                ).fetchall()
                candidate_ids = [row[0] for row in candidates]
                scoped = conn.execute(
                    f"""SELECT {columns} FROM json_each(?) ids CROSS JOIN passages p ON p.id=ids.value
                        CROSS JOIN mails m ON m.id=p.mail CROSS JOIN sources s ON s.id=m.source WHERE {where}
                        ORDER BY ids.key""",
                    (json.dumps(candidate_ids), *params),
                ).fetchall() if candidate_ids else []
                sources, documents = {}, {}
                top = []
                for row in scoped:
                    if self.readable(row, sources, documents):
                        top.append(row)
                    if len(top) == limit:
                        break
                if len(top) < limit and len(candidate_ids) == window:
                    # Keep globally ranked hits and fill from the bounded scope.
                    cursor = self.scoped_lexical(conn, statement, filters, columns, window)
                    seen = {row["id"] for row in top}
                    try:
                        for row in cursor:
                            if row["id"] not in seen and self.readable(row, sources, documents):
                                top.append(row)
                            if len(top) == limit:
                                break
                    finally:
                        cursor.close()
                for rank, row in enumerate(top):
                    passage = row["id"]
                    ranked[passage] = ranked.get(passage, 0) + weight / (30 + rank)
            ordered_ids = sorted(ranked, key=ranked.get, reverse=True)
            if not exact_identifier or fuzzy:
                ordered_ids = ordered_ids[:limit]
            found = {row["passage"]: row for row in self.passages(ordered_ids, filters, conn, checked=True)}
            ordered = [(found[item], ranked[item]) for item in ordered_ids if item in found]
        if fuzzy:
            return [row for row, _ in ordered[:limit]]
        exact = normalized(question.strip())
        return [row for row, _ in sorted(ordered, key=lambda entry: (
            exact_identifier and exact in WORD.findall(normalized(entry[0]["subject"] + " " + entry[0]["text"])), entry[1]
        ), reverse=True)[:limit]]

    def snippets(self, question, passage_ids):
        words = query_words(question)
        if not words or not passage_ids:
            return {}
        expressions = dict.fromkeys(fts_query(words, mode) for mode in ("terms", "grams"))
        with self.connection() as conn:
            return dict(conn.execute(
                """SELECT rowid,snippet(terms,3,'','','',24) FROM terms
                   WHERE terms MATCH ? AND rowid IN (SELECT value FROM json_each(?))""",
                (" OR ".join(expressions), json.dumps(passage_ids)),
            ))

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
                        key = (row["fingerprint"], row["source"]) if self.deferred_access() else row["fingerprint"]
                        verified.setdefault(key, row)
                matches = verified if matches is None else {key: row for key, row in matches.items() if key in verified}
            for row in (matches or {}).values():
                key = (row["fingerprint"], row["source"]) if self.deferred_access() else row["fingerprint"]
                selected.setdefault(key, row)
        return list(selected.values())

    def semantic_contextual(self, values, model, filters, limit=40, *, min_similarity=.28):
        """Use one input format per archive; keep legacy searches during migration."""
        contextual, previous = contextual_model_keys(model)
        source = filters.get("source")
        with self.connection() as conn:
            archives = conn.execute(
                """SELECT s.id,coalesce(current.model,legacy.model,?) model
                   FROM sources s LEFT JOIN embedding_revisions current
                   ON current.source=s.id AND current.version=coalesce(s.active,s.pending) AND current.model=?
                   LEFT JOIN embedding_revisions legacy
                   ON legacy.source=s.id AND legacy.version=coalesce(s.active,s.pending) AND legacy.model=?
                   WHERE (? IS NULL OR s.id=?) AND (s.active IS NOT NULL OR s.pending IS NOT NULL)""",
                (model, contextual, previous, source, source),
            ).fetchall()
        partitions = {key: [] for key in (contextual, previous, model)}
        for row in archives:
            partitions[row["model"]].append(row["id"])
        partitions = {key: ids for key, ids in partitions.items() if ids}
        if len(partitions) == 1:
            return self.semantic(values, next(iter(partitions)), filters, limit, min_similarity=min_similarity)
        pools = [self.semantic(values, key, {**filters, "vector_sources": ids}, limit, min_similarity=min_similarity)
                 for key, ids in partitions.items()]
        # Use ranks across formats because their similarity scores are not interchangeable.
        return [row for group in zip_longest(*pools) for row in group if row is not None][:limit]

    def semantic(self, values, model, filters, limit=40, *, min_similarity=.28):
        vector = vector_array(pack_vector(values))
        where, params = self.scope(filters)
        with self.connection() as conn:
            # Exact cosine for small indexes. Limit comparisons for large archives.
            scoped = f"""FROM sources s JOIN mails m ON m.source=s.id CROSS JOIN passages p ON p.mail=m.id
                         WHERE {where} AND (EXISTS(SELECT 1 FROM vectors v WHERE v.passage=p.id AND v.model=?)
                            OR EXISTS(SELECT 1 FROM embedding_refs r WHERE r.passage=p.id AND r.model=?))"""
            chosen = self.vector_index.candidates(conn, vector, model, filters, limit) if self.vector_index else None
            if chosen is None:
                ids = conn.execute("SELECT p.id " + scoped + " LIMIT 2001", (*params, model, model)).fetchall()
                if len(ids) <= 2000:
                    chosen = [row[0] for row in ids]
                else:
                    probes = [candidate for bucket in buckets(vector)
                              for candidate in (bucket, *(bucket ^ (1 << n) for n in range(11)))]
                    ranked = conn.execute(
                        f"""SELECT b.passage FROM vector_buckets b JOIN passages p ON p.id=b.passage
                            JOIN mails m ON m.id=p.mail {JOIN}
                            WHERE b.model=? AND b.bucket IN (SELECT value FROM json_each(?)) AND {where}
                            GROUP BY b.passage ORDER BY count(*) DESC,b.passage DESC LIMIT 2400""",
                        (model, json.dumps(probes), *params),
                    )
                    chosen = [row[0] for row in ranked]
            vectors = conn.execute(VECTOR_LOOKUP_SQL, (model, json.dumps(chosen)))
            eligible = [(row["passage"], row["value"]) for row in vectors if len(row["value"]) == len(vector) * 4]
            scores = []
            if eligible:
                numpy = self.vector_index.numpy if self.vector_index else optional_numpy()
                if numpy is not None:
                    passage_ids, values = zip(*eligible)
                    matrix = numpy.frombuffer(b"".join(values), dtype="<f4").reshape(-1, len(vector))
                    similarities = numpy.einsum("ij,j->i", matrix, numpy.asarray(vector, dtype="float32"))
                    scores = [(float(score), passage) for passage, score in zip(passage_ids, similarities) if score >= min_similarity]
                else:
                    scores = [(math.sumprod(vector, vector_array(value)), passage) for passage, value in eligible]
                    scores = [(score, passage) for score, passage in scores if score >= min_similarity]
            ordered = sorted(scores, reverse=True)
            results = []
            for start in range(0, len(ordered), limit):
                batch = ordered[start:start + limit]
                found = {row["passage"]: row for row in self.passages([pid for _, pid in batch], filters, conn)}
                results.extend({**found[pid], "similarity": similarity} for similarity, pid in batch if pid in found)
                if len(results) >= limit:
                    break
            return results[:limit]

    @property
    def vector_backend(self):
        if self.vector_index:
            return "usearch(int8)"
        return "sqlite+numpy" if optional_numpy() is not None else "sqlite(pure-python)"

    def passages(self, passages, filters=None, conn=None, *, checked=False, windows=()):
        if not passages:
            return []
        if conn is None:
            with self.connection() as conn:
                return self.passages(passages, filters, conn, checked=checked, windows=windows)
        where, params = self.scope(filters or {})
        rows = conn.execute(
            f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,p.attachment attachment_id,a.name attachment
                FROM json_each(?) ids CROSS JOIN passages p ON p.id=ids.value
                CROSS JOIN mails m ON m.id=p.mail CROSS JOIN sources s ON s.id=m.source
                LEFT JOIN attachments a ON a.id=p.attachment
                WHERE {where}""",
            (json.dumps(passages), *params),
        )
        visible = [dict(row) for row in rows] if checked else self.visible_rows(rows)
        return [self.expand_passage_window(row, conn=conn) if row["passage"] in windows else row
                for row in visible] if windows else visible

    def reply_links(self, mails):
        if not mails:
            return []
        with self.connection() as conn:
            return conn.execute(
                """SELECT m.id,m.source,m.message_id,r.parent FROM mails m
                   LEFT JOIN replies r ON r.mail=m.id WHERE m.id IN (SELECT value FROM json_each(?))""",
                (json.dumps(sorted(mails)),),
            ).fetchall()

    def evidence_attachment_names(self, mails, *, indexed_only=True):
        names = defaultdict(list)
        if not mails:
            return names
        with self.connection() as conn:
            if not indexed_only:
                rows = conn.execute(
                    """SELECT a.mail,a.name FROM json_each(?) ids CROSS JOIN attachments a ON a.mail=ids.value
                       ORDER BY a.mail,a.id""", (json.dumps(sorted(set(mails))),),
                )
                for row in rows:
                    names[row["mail"]].append(row["name"])
                return names
            for mail in mails:
                names[mail] = [row[0][:160] for row in conn.execute(
                    """SELECT DISTINCT a.name FROM attachments a WHERE a.mail=? AND EXISTS
                       (SELECT 1 FROM passages p WHERE p.attachment=a.id AND p.location<>'파일명')
                       ORDER BY a.name LIMIT 6""", (mail,),
                )]
        return names

    def mail_body_previews(self, mails):
        if not mails:
            return {}
        with self.connection() as conn:
            return dict(conn.execute(
                """SELECT ids.value,(SELECT substr(p.text,1,200) FROM passages p WHERE p.mail=ids.value
                   AND p.attachment IS NULL AND p.location='본문' ORDER BY p.start,p.id LIMIT 1)
                   FROM json_each(?) ids""", (json.dumps(sorted(set(mails))),),
            ))

    def attachment_evidence(self, mails, filenames, filters, question):
        """Find indexed attachment text only on the supplied, currently scoped mails."""
        if not mails or not filenames:
            return []
        where, params = self.scope(filters)
        terms = query_words(question, fuzzy=True)[:8]
        score = " + ".join("(instr(lower(p.text), lower(?)) > 0)" for _ in terms)
        order = f"({score}) DESC, p.start, p.id" if terms else "p.start, p.id"
        found = []
        with self.connection() as conn:
            for name in filenames[:2]:
                attachments = conn.execute(
                    f"""SELECT a.id FROM json_each(?) ids CROSS JOIN mails m ON m.id=ids.value
                        CROSS JOIN sources s ON s.id=m.source CROSS JOIN attachments a ON a.mail=m.id
                        WHERE {where} AND instr(lower(a.name), lower(?)) > 0
                        AND EXISTS (SELECT 1 FROM passages p WHERE p.attachment=a.id AND p.location<>'파일명')
                        ORDER BY lower(a.name)<>lower(?), ids.key, length(a.name), a.id LIMIT 1""",
                        (json.dumps(mails), *params, name, name),
                ).fetchall()
                for attachment in attachments:
                    found.extend(row[0] for row in conn.execute(
                        f"""SELECT p.id FROM passages p WHERE p.attachment=? AND p.location<>'파일명'
                            ORDER BY {order} LIMIT 3""", (attachment["id"], *terms),
                    ))
            ids = list(dict.fromkeys(found))
            visible = {row["passage"]: row for row in self.passages(ids, filters, conn)}
        return [visible[pid] for pid in ids if pid in visible]

    def related_mail_ids(self, seeds, filters, *, hops=1):
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
            candidates = {}
            frontier = set(identifiers)
            for hop in range(max(1, min(hops, 3))):
                hits = conn.execute(
                    f"""SELECT m.id,m.message_id,m.topic,m.sender,m.recipient
                        FROM mails m {JOIN} WHERE {where} AND
                        (m.message_id IN (SELECT value FROM json_each(?))
                         OR m.id IN (SELECT mail FROM replies WHERE parent IN (SELECT value FROM json_each(?)))
                         OR m.topic IN (SELECT value FROM json_each(?)))
                        ORDER BY m.sent DESC LIMIT 60""",
                    (*params, json.dumps(list(frontier)), json.dumps(list(frontier)),
                     json.dumps(list(refs) if hop == 0 else [])),
                ).fetchall()
                new = [row for row in hits if row["id"] not in candidates and row["id"] not in seeds]
                candidates.update((row["id"], row) for row in new)
                if hop + 1 >= hops or not new:
                    break
                parents = conn.execute(
                    "SELECT parent FROM replies WHERE mail IN (SELECT value FROM json_each(?))",
                    (json.dumps([row["id"] for row in new]),),
                )
                next_ids = {row[0] for row in parents} | {row["message_id"] for row in new if row["message_id"]}
                frontier = next_ids - set(identifiers)
                if not frontier:
                    break
                identifiers.extend(frontier)
            reply_ids = {row[0] for row in conn.execute(
                "SELECT mail FROM replies WHERE parent IN (SELECT value FROM json_each(?))", (json.dumps(identifiers),)
            )}
        linked = []
        for row in candidates.values():
            emails = set(re.findall(r"[\w.+-]+@[\w.-]+", (row["sender"] + " " + row["recipient"]).lower()))
            if row["id"] not in seeds and (row["message_id"] in identifiers or row["id"] in reply_ids or refs[row["topic"]] & emails):
                linked.append(row["id"])
        return linked

    def related(self, seeds, filters, *, hops=1, question=""):
        linked = self.related_mail_ids(seeds, filters, hops=hops)
        if not linked:
            return []
        words = [word for word in query_words(question, fuzzy=True) if len(word) > 1]
        best = defaultdict(list)
        linked = linked[:24]
        with self.connection() as conn:
            passages = conn.execute(
                """SELECT p.id,p.mail,p.text,p.location,a.name FROM json_each(?) ids
                   CROSS JOIN passages p ON p.mail=ids.value LEFT JOIN attachments a ON a.id=p.attachment""",
                (json.dumps(linked),),
            )
            for part in passages:
                text = normalized(part["text"] + " " + (part["name"] or ""))
                matches = sum(word in text for word in words)
                score = (part["location"] != "파일명", matches, bool(DECISION_SIGNAL.search(part["text"])),
                         bool(part["name"] and filters.get("prefer_attachment")), -part["id"])
                choices = best[part["mail"]]
                choices.append((score, part["id"]))
                choices.sort(reverse=True)
                del choices[2:]
            picked = [best[mail][0][1] for mail in linked if best[mail]]
            picked.extend(best[mail][1][1] for mail in linked if len(best[mail]) > 1
                          and best[mail][1][0][0] and any(best[mail][1][0][1:4]))
            found = {row["passage"]: row for row in self.passages(picked, filters, conn)}
        return [found[item] for item in picked if item in found][:6]

    def expand_passage_window(self, row, *, max_chars=1800, conn=None):
        """Expand a visible passage within the same original part."""
        if (row.get("kind") == "pcf" or row["location"] in {"파일명", "제목"} or row.get("window")
                or "start" not in row or len(row["text"]) >= max_chars):
            return row
        if conn is None:
            with self.connection() as conn:
                return self.expand_passage_window(row, max_chars=max_chars, conn=conn)
        anchor = row["passage"]
        base = """SELECT start,text FROM passages WHERE mail=? AND location=? AND attachment IS
                  (SELECT attachment FROM passages WHERE id=?) AND id"""
        params = (row["id"], row["location"], anchor, anchor)
        left = conn.execute(base + "<? ORDER BY id DESC LIMIT 4", params).fetchall()
        right = conn.execute(base + ">? ORDER BY id LIMIT 4", params).fetchall()
        window = (row["start"], row["text"])
        right_start = row["start"]
        extend_left = extend_right = True
        for before, after in zip_longest(left, right):
            if before is not None and extend_left:
                merged = merge_contiguous_passages((tuple(before), window))
                if merged:
                    window = merged
                else:
                    extend_left = False
            if after is not None and extend_right:
                merged = merge_contiguous_passages((window, tuple(after))) if after["start"] > right_start else None
                if merged:
                    window = merged
                    right_start = after["start"]
                else:
                    extend_right = False
        start, text = window
        if len(text) > max_chars:
            anchor = row["start"] - start
            spare = max_chars - len(row["text"])
            after = len(text) - anchor - len(row["text"])
            left_chars = min(anchor, max(spare // 2, spare - after))
            offset = anchor - left_chars
            start += offset
            text = text[offset:offset + max_chars]
        if (start, text) == (row["start"], row["text"]):
            return row
        return {**row, "start": start, "text": text, "window": True}

    def evidence_context(self, row, filters):
        """Fetch reviewable context from the same mail part or worksheet."""
        location = row["location"]
        sheet, sep, cells = location.rpartition("!")
        is_sheet = sep and re.fullmatch(r"[A-Z]+[0-9]+~[A-Z]+[0-9]+", cells)
        with self.connection() as conn:
            base = """SELECT id FROM passages WHERE mail=? AND attachment IS
                      (SELECT attachment FROM passages WHERE id=?)"""
            args = (row["id"], row["passage"])
            if is_sheet:
                prefix = sheet + "!"
                base += " AND substr(location,1,length(?))=?"
                args += (prefix, prefix)
                queries = [(base + " ORDER BY id LIMIT 2", args),
                           (base + " AND id<? ORDER BY id DESC LIMIT 1", (*args, row["passage"]))]
            else:
                base += " AND location=?"
                args += (location,)
                queries = [(base + " AND id>? ORDER BY id LIMIT 1", (*args, row["passage"])),
                           (base + " AND id<? ORDER BY id DESC LIMIT 1", (*args, row["passage"]))]
                if location == "본문":
                    queries.insert(0, (base + " ORDER BY id DESC LIMIT 1", args))
            ids = dict.fromkeys(item[0] for sql, params in queries for item in conn.execute(sql, params))
            ids.pop(row["passage"], None)
            found = {item["passage"]: item for item in self.passages(list(ids), filters, conn)} if ids else {}
            return [found[key] for key in ids if key in found]

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

    def open_passage(self, passage, *, window=False):
        where, params = self.scope({})
        with self.connection() as conn:
            row = conn.execute(
                f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,a.name attachment,m.body
                    FROM passages p JOIN mails m ON m.id=p.mail {JOIN}
                    LEFT JOIN attachments a ON a.id=p.attachment WHERE p.id=? AND {where}""", (passage, *params)
            ).fetchone()
            if row is None or not self.result_access(row, {}, {}, refresh=True):
                return None
            result = dict(row)
            if window:
                result = self.expand_passage_window(result, conn=conn)
            result["attachments"] = [dict(item) for item in conn.execute(
                """SELECT a.id,a.name,a.status,(SELECT min(id) FROM passages WHERE attachment=a.id AND location<>'파일명') passage
                   FROM attachments a WHERE a.mail=? ORDER BY a.id""", (row["id"],)
            )]
        return result

    def original_attachment(self, attachment):
        with self.connection() as conn:
            row = conn.execute(
                f"""SELECT a.name,a.content_key,m.message_id,m.item,m.folder,m.fingerprint,m.version,m.source,s.kind,s.path,
                           s.active,s.pending,s.signature,s.active_signature,
                           (SELECT COUNT(*) FROM attachments earlier WHERE earlier.mail=m.id AND earlier.id<a.id) position
                    FROM attachments a JOIN mails m ON m.id=a.mail {JOIN}
                    WHERE a.id=? AND m.kind='mail' AND {VISIBLE_VERSION}""",
                (attachment,),
            ).fetchone()
        if row is not None:
            with self.measure("access"):
                if source_readable(row["path"], row["kind"]):
                    return row
        return None

    def pcf_case(self, seed, filters=None):
        """Resolve a cell to its full case in the visible indexed document version."""
        match = PCF_CELL.fullmatch(seed["location"])
        if not match:
            return None
        with self.connection() as conn:
            stored = conn.execute("SELECT data FROM pcf_documents WHERE mail=?", (seed["id"],)).fetchone()
            if stored is None:
                return None
            doc = json.loads(stored["data"])
            case = next((item for item in doc["comments"] if case_key(item) == match[1]), None)
            if case is None:
                return None
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM passages WHERE mail=? AND location LIKE ?",
                (seed["id"], f"PCF {match[1]} · %"),
            )]
            visible = {row["location"]: row for row in self.passages(ids, filters, conn)}
            resolved = []
            for item in case["rows"]:
                for side in ("owner", "yard"):
                    cell = item[side]
                    if cell is not None:
                        row = visible.get(cell_location(case, side, cell))
                        if row is not None:
                            resolved.append({**row, "pcf_cell": {
                                "no": case["number"], "row": item["row"], "label": cell["label"],
                                "side": side.upper(),
                            }})
            if not resolved:
                return None
            return {
                "number": case["number"], "rows": resolved, "expected": 2 * len(case["rows"]),
                "warnings": [*doc["warnings"], *case["warnings"]],
                "source_version": doc["source_version"], "revision": doc.get("revision"),
            }

    def pcf_by_number(self, number, filters):
        where, params = self.scope(filters)
        with self.connection() as conn:
            rows = conn.execute(
                f"""SELECT {MAIL_COLUMNS},p.id passage,p.text,p.location,p.start,a.name attachment
                    FROM pcf_documents d JOIN mails m ON m.id=d.mail {JOIN}
                    JOIN passages p ON p.mail=m.id JOIN attachments a ON a.id=p.attachment
                    WHERE {where} AND p.location GLOB ? GROUP BY m.id,substr(p.location,1,instr(p.location,' · ') - 1)
                    ORDER BY m.sent DESC LIMIT 20""",
                (*params, f"PCF T* · No. {number} · T*"),
            )
            return self.visible_rows(rows)
