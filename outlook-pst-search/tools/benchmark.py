"""Measure app-side lookup on a generated, non-confidential archive."""
import argparse
import math
import random
import statistics
import tempfile
import time
from pathlib import Path

from mailsearch.store import Store


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50_000)
    parser.add_argument("--repetitions", type=int, default=100)
    args = parser.parse_args()
    if args.count < 1 or args.repetitions < 1:
        parser.error("Benchmark parameters must be positive")
    run(args.count, args.repetitions)
