"""Measure app-side lookup on a generated, non-confidential archive."""
import argparse
import importlib.util
import math
import random
import statistics
import tempfile
import time
from pathlib import Path
from email.message import EmailMessage
from mailsearch.indexer import Indexer

from mailsearch.store import Store, buckets, pack_vector


def percentile_95(samples):
    return sorted(samples)[math.ceil(len(samples) * .95) - 1]


def run(count, repetitions, chunks=1):
    with tempfile.TemporaryDirectory(prefix="mail-benchmark-") as directory:
        root = Path(directory)
        archive = root / "synthetic"
        archive.mkdir()
        store = Store(root / "index")
        begin = time.perf_counter()
        with store.connection() as conn:
            source = conn.execute("INSERT INTO sources(path,kind,active,state) VALUES(?,'eml_dir',1,'ready')", (str(archive),)).lastrowid
            for number in range(count):
                code = f"AX-{number:06d}"
                store.add_mail(conn, source, 1, str(number), {
                    "subject": f"{code} 반도체 펌프 양산 일정 변경",
                    "sender": "가상구매팀 <purchase@example.invalid>",
                    "recipient": "가상제조팀 <manufacturing@example.invalid>",
                    "sent": "2026-05-15T02:00:00+00:00", "folder": "/구매", "attachments": [],
                    "message_id": f"<{number}@example.invalid>", "reply_ids": "",
                    "body": (f"{code} 납품 일정입니다. 조립 검수는 5월 15일 진행합니다. 5월 22일 출하를 승인합니다."
                             + "\n" + ("검토 관련 설비 시운전 준비 일정이 확정되었습니다. " * (chunks * 36 if chunks > 1 else 0))),
                })
        ingestion = time.perf_counter() - begin
        generator = random.Random(17)
        queries = [f"AX-{generator.randrange(count):06d}" for _ in range(repetitions)]
        exact_times = []
        hit = 0
        for query in queries:
            begin = time.perf_counter()
            rows = store.lexical(query, {}, limit=180)
            exact_times.append((time.perf_counter() - begin) * 1_000)
            hit += bool(rows and rows[0]["subject"].startswith(query + " "))
        broad_times = []
        for query in ("반도체 펌프", "양산 일정", "납품 검수", "5월 출하 승인", "가상구매팀") * 4:
            begin = time.perf_counter()
            store.lexical(query, {}, limit=180)
            broad_times.append((time.perf_counter() - begin) * 1_000)
        with store.connection() as conn:
            passages = conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        print(f"{count:,} synthetic emails, {passages:,} passages; build {ingestion:.2f}s; database {store.db.stat().st_size / 1024**2:.2f} MiB")
        timed = getattr(store, "search_session", None)
        if timed:
            with timed(defer_access=True):
                for name, method in (("UI quick", store.quick_search), ("UI full text", store.lexical)):
                    samples = []
                    for _ in range(min(repetitions, 20) + 1):
                        start = time.perf_counter()
                        method("관련" if chunks > 1 else "양산 일정", {"source": source, "tab": "mail"})
                        samples.append((time.perf_counter() - start) * 1000)
                    print(f"{name}: median {statistics.median(samples[1:]):.1f} ms, P95 {percentile_95(samples[1:]):.1f} ms")
        print(f"Exact IDs: {hit}/{repetitions} first hits, median {statistics.median(exact_times):.1f} ms, P95 {percentile_95(exact_times):.1f} ms")
        print(f"Common Korean terms: {len(broad_times)} searches, median {statistics.median(broad_times):.1f} ms, P95 {percentile_95(broad_times):.1f} ms")


def scoped_semantic(count, repetitions, backend="sqlite", dimensions=96):
    """Tie every bucket to reproduce candidate starvation across archives."""
    vector = [1. if i % 2 == 0 else 0. for i in range(dimensions)]
    packed, bands = pack_vector(vector), list(buckets(vector))
    with tempfile.TemporaryDirectory(prefix="semantic-scope-") as directory:
        root = Path(directory)
        store = Store(root / "index")
        targets = []
        middle = count // 10
        for name, size in (("small", 20), ("medium", middle), ("large", count - middle - 20)):
            folder = root / name
            folder.mkdir()
            with store.connection() as conn:
                source = conn.execute("INSERT INTO sources(path,kind,active) VALUES(?,'eml_dir',1)", (str(folder),)).lastrowid
                for number in range(size):
                    mail = store.add_mail(conn, source, 1, str(number), {
                        "subject": "common", "sender": name, "recipient": "", "sent": "", "folder": "/",
                        "attachments": [], "message_id": "", "reply_ids": "", "body": "shared evidence",
                    })
                    passage = conn.execute("SELECT id FROM passages WHERE mail=?", (mail,)).fetchone()[0]
                    conn.execute("INSERT INTO vectors VALUES(?,'demo',?)", (passage, packed))
                    conn.executemany("INSERT INTO vector_buckets VALUES('demo',?,?)", [(band, passage) for band in bands])
            if name != "large":
                targets.append((name, source))
        if backend == "usearch":
            from mailsearch.vector_index import USearchIndex
            store.vector_index = USearchIndex(store)
            for _, source in targets:
                shard = ("demo", source, 1, dimensions)
                store.vector_index.build(shard)
                if store.vector_index.indices.get(shard) is None:
                    raise SystemExit(f"USearch failed to build source {source}")
        for name, source in targets:
            times = []
            for _ in range(repetitions + 1):
                start = time.perf_counter()
                rows = store.semantic(vector, "demo", {"source": source}, limit=20)
                times.append((time.perf_counter() - start) * 1000)
            hits = sum(row["source"] == source for row in rows)
            print(f"{backend} {name}: {hits}/20 scoped hits, warm P95 {percentile_95(times[1:]):.1f} ms")
        if backend == "usearch":
            store.vector_index.close()


def duplicate_attachments(count):
    class Fixture:
        embedding_index = "benchmark"
        embedding_api = None
        embedded = 0

        def embed(self, texts, timeout):
            self.embedded += len(texts)
            return [[1., 0., .1] for _ in texts]

    with tempfile.TemporaryDirectory(prefix="duplicate-mail-") as directory:
        root = Path(directory)
        folder = root / "sources"
        folder.mkdir()
        for i in range(count):
            msg = EmailMessage()
            msg["From"] = "fixture@example.invalid"
            msg["Message-ID"] = f"<fixture-{i}@example.invalid>"
            msg["Subject"] = f"Synthetic {i}"
            msg.set_content("Actual reply.\n\n> Repeated quote\n" + ("> duplicated follow-up paragraph \n" * 100))
            msg.add_attachment(("동일 견적 단락 12345\n" * 100).encode(),
                               maintype="text", subtype="plain", filename="quote.txt")
            (folder / f"mail-{i}.eml").write_bytes(msg.as_bytes())
        store = Store(root / "index")
        ai = Fixture()
        indexer = Indexer(store, ai=ai)
        started = time.perf_counter()
        try:
            source = indexer.register(folder)
            while time.perf_counter() - started < 120:
                row = store.source(source)
                if row["state"] in {"ready", "error"}:
                    break
                time.sleep(.02)
            if row["state"] != "ready":
                raise RuntimeError(f"Could not prepare benchmark corpus: {row['state']}/{row['issue']}")
            physical = ai.embedded
            logical = row["embedding_total"]
            print(f"{count} emails; {logical} indexed passages; {physical} texts sent to embedding; {time.perf_counter()-started:.2f} s")
        finally:
            indexer.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50_000)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--semantic-scope", action="store_true", help="Measure filtered vector search with colliding buckets")
    parser.add_argument("--duplicate-attachments", action="store_true", help="Count repeated quote and attachment work")
    parser.add_argument("--chunks", type=int, default=1, help="Scale out mail body text")
    parser.add_argument("--dimensions", type=int, default=96)
    parser.add_argument("--vector-index", choices=("sqlite", "usearch"), default="sqlite")
    args = parser.parse_args()
    if min(args.count, args.repetitions, args.chunks, args.dimensions) < 1:
        parser.error("Benchmark parameters must be positive")
    if args.semantic_scope:
        if args.count < 25000:
            parser.error("Use at least 25000 passages to exercise both scoped search paths")
        if args.vector_index == "usearch" and importlib.util.find_spec("usearch") is None:
            parser.error("Install the optional USearch wheel before benchmarking this backend")
        scoped_semantic(args.count, args.repetitions, args.vector_index, args.dimensions)
    elif args.duplicate_attachments:
        duplicate_attachments(args.count)
    else:
        run(args.count, args.repetitions, args.chunks)
