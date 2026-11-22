"""Measure app-side lookup on a generated, non-confidential archive."""
import argparse
import math
import random
import statistics
import tempfile
import time
from pathlib import Path

from mailsearch.store import Store, buckets, pack_vector


def percentile_95(samples):
    return sorted(samples)[math.ceil(len(samples) * .95) - 1]


def run(count, repetitions):
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
                    "body": f"{code} 납품 일정입니다. 조립 검수는 5월 15일 진행합니다. 5월 22일 출하를 승인합니다.",
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
        print(f"{count:,} synthetic emails; build {ingestion:.2f}s; database {store.db.stat().st_size / 1024**2:.2f} MiB")
        print(f"Exact IDs: {hit}/{repetitions} first hits, median {statistics.median(exact_times):.1f} ms, P95 {percentile_95(exact_times):.1f} ms")
        print(f"Common Korean terms: {len(broad_times)} searches, median {statistics.median(broad_times):.1f} ms, P95 {percentile_95(broad_times):.1f} ms")


def scoped_semantic(count, repetitions):
    """Tie every bucket to reproduce candidate starvation across archives."""
    vector = [1., 0.] * 48
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
        for name, source in targets:
            times = []
            for _ in range(repetitions + 1):
                start = time.perf_counter()
                rows = store.semantic(vector, "demo", {"source": source}, limit=20)
                times.append((time.perf_counter() - start) * 1000)
            hits = sum(row["source"] == source for row in rows)
            print(f"{name}: {hits}/20 scoped hits, warm P95 {percentile_95(times[1:]):.1f} ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50_000)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--semantic-scope", action="store_true", help="Measure filtered vector search with colliding buckets")
    args = parser.parse_args()
    if args.count < 1 or args.repetitions < 1:
        parser.error("Benchmark parameters must be positive")
    if args.semantic_scope and args.count < 25000:
        parser.error("Use at least 25000 passages to exercise both scoped search paths")
    (scoped_semantic if args.semantic_scope else run)(args.count, args.repetitions)
