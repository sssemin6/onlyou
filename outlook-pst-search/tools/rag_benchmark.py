"""Measure filtered search, restart behavior and grounded RAG on synthetic mail."""
import argparse
import json
import logging
import re
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from mailsearch import answer
from mailsearch.external import ExternalAI, Reviews
from mailsearch.search_profile import SearchProfile
from mailsearch.store import Store, buckets, pack_vector, query_words
from mailsearch.vector_index import USearchIndex


LOGGER = logging.getLogger("benchmark")
CASES = (
    ("A17", "시제품 승인과 양산 BOM 비교", ("시제품 승인", "양산 BOM"),
     ("A17 시제품 두께는 4 mm로 승인됐습니다.", "A17 양산 BOM 두께는 3 mm입니다."), 6),
    ("P24", "배관 적용 범위와 시험 결과 비교", ("배관 적용 범위", "시험 결과"),
     ("P24 배관 변경은 2호선에만 적용됩니다.", "P24 시험 변위는 0.18 mm입니다."), 18),
    ("K-55", "펌프 거래처 회신과 검수 기입값 비교", ("거래처 회신", "검수 기입값"),
     ("K-55 거래처 회신에 유량 20 L가 기록됐습니다.", "K-55 검수표에 압력 45 kPa가 기입됐습니다."), 28),
    ("R82", "변경 사유와 개정 도면 치수 비교", ("변경 사유", "개정 도면 치수"),
     ("R82 브래킷은 변형 발생으로 변경했습니다.", "R82 개정 도면의 폭은 18 mm입니다."), 36),
    ("T-09", "선박 부품 납기와 검사 사양 비교", ("부품 납기", "검사 사양"),
     ("T-09의 납품 요청일은 5월 28일입니다.", "T-09 검사 허용값은 0.20 mm입니다."), 48),
    ("M66", "부식 시험 및 규격 적용 범위 비교", ("부식 시험", "규격 적용 범위"),
     ("M66 부식 시험 온도는 45도입니다.", "M66 규격은 시제품에만 적용됩니다."), 60),
)


def add_source(conn, root, label):
    folder = root / label
    folder.mkdir()
    return conn.execute("INSERT INTO sources(path,kind,active,state) VALUES(?,'eml_dir',1,'ready')",
                        (str(folder),)).lastrowid


def add_mail(store, conn, source, item, subject, body, *, sender="김 부장", sent="2025-08-22"):
    mail = store.add_mail(conn, source, 1, item, {
        "subject": subject, "sender": sender, "recipient": "", "sent": sent, "folder": "/", "attachments": [],
        "message_id": f"<{item}@example.invalid>", "reply_ids": "", "body": body,
    })
    return conn.execute("SELECT id FROM passages WHERE mail=? ORDER BY id LIMIT 1", (mail,)).fetchone()[0]


def write_vector(conn, passage, model, vector, bands=()):
    conn.execute("INSERT INTO vectors VALUES(?,?,?)", (passage, model, vector))
    conn.executemany("INSERT INTO vector_buckets VALUES(?,?,?)", ((model, band, passage) for band in bands))


def measured(store, stage, phase, search):
    profile = SearchProfile(phase)
    with store.search_session(profile), store.measure(stage):
        rows = search()
    profile.log(LOGGER, phase)
    return rows, profile.ms[stage]


def summary(samples):
    data = sorted(samples)
    return {"median_ms": round(statistics.median(data), 2),
            "p95_ms": round(data[(95 * len(data) + 99) // 100 - 1], 2)}


def search_benchmark(root, count, dimensions, repetitions):
    store = Store(root / "search-index")
    vector = [1. if n % 2 == 0 else 0. for n in range(dimensions)]
    packed, bands = pack_vector(vector), tuple(buckets(vector))
    sources = []
    with store.connection() as conn:
        for shard in range(10):
            source = add_source(conn, root, f"archive-{shard}")
            sources.append(source)
            for n in range(count):
                passage = add_mail(store, conn, source, f"{shard}-{n}", "Atlas 조립 검토",
                                   f"Atlas 시험 결과 및 조립 검토 {shard}-{n}",
                                   sent="2025-08-22" if shard < 9 else "2026-08-22")
                write_vector(conn, passage, "fixture", packed, bands)
    # Prepare all shards once, close the process-equivalent state, and reopen.
    store.vector_index = USearchIndex(store)
    for source in sources:
        store.vector_index.build(("fixture", source, 1, dimensions))
    store.vector_index.close()
    start = time.perf_counter()
    store = Store(root / "search-index")
    store.vector_index = USearchIndex(store)
    reopen_ms = (time.perf_counter() - start) * 1000
    try:
        single = {"source": sources[-1]}
        _, single_cold = measured(store, "vector", "vector_single_cold",
                                  lambda: store.semantic(vector, "fixture", single, limit=40))
        _, all_cold = measured(store, "vector", "vector_all_cold",
                               lambda: store.semantic(vector, "fixture", {}, limit=40))
        deadline = time.perf_counter() + 30
        while store.vector_index.building and time.perf_counter() < deadline:
            time.sleep(.002)
        ready_ms = (time.perf_counter() - start) * 1000
        series = {"vector_all_warm": [], "fts_scoped_warm": []}
        scoped = {"source": sources[-1], "after": "2026-08-01"}
        for _ in range(repetitions):
            rows, elapsed = measured(store, "vector", "vector_all_warm",
                                     lambda: store.semantic(vector, "fixture", {}, limit=40))
            assert len(rows) == 40
            series["vector_all_warm"].append(elapsed)
            rows, elapsed = measured(store, "fts", "fts_scoped_warm",
                                     lambda: store.lexical("Atlas 조립 검토", scoped, limit=80))
            assert len(rows) == 80 and all(row["source"] == sources[-1] for row in rows)
            series["fts_scoped_warm"].append(elapsed)
        return {"store_open_ms": round(reopen_ms, 2), "single_cold_vector_ms": round(single_cold, 2),
                "all_cold_vector_ms": round(all_cold, 2), "first_queries_and_shard_ready_ms": round(ready_ms, 2),
                **{label: summary(values) for label, values in series.items()}}
    finally:
        store.vector_index.close()


class ModelFixture:
    index_key = "quality-fixture"
    models = {"rerank": "synthetic"}

    def embed(self, texts, timeout):
        return [[float(code in text) for code, *_ in CASES] + [0.] for text in texts]

    def rerank(self, query, documents):
        self.documents = documents
        words = query_words(query, fuzzy=True)
        return sorted(range(len(documents)), key=lambda i: (
            bool(re.search(r"(?:입니다|됐습니다|했습니다|적용됩니다)\.", documents[i])),
            sum(word.casefold() in documents[i].casefold() for word in words),
        ), reverse=True)


def quality_benchmark(root):
    store = Store(root / "quality-index")
    fixture = ModelFixture()
    ai = answer.LocalAI("", embedding_api=fixture)
    provider = ExternalAI("https://example.invalid/v1/chat/completions", "fixture", "fixture")
    revised = hasattr(answer, "rewritten_evidence")
    with store.connection() as conn:
        source = add_source(conn, root, "quality-archive")
        for index, (code, question, aspects, quotes, distractors) in enumerate(CASES):
            for n in range(distractors):
                item = f"{code}-d{n}"
                passage = add_mail(store, conn, source, item, f"{code} {question}",
                                   f"{code} {question} 자료 취합과 첨부 {n}")
                vector = [float(i == index) for i in range(len(CASES))] + [0.]
                write_vector(conn, passage, fixture.index_key, pack_vector(vector))
            for n, (aspect, quote) in enumerate(zip(aspects, quotes, strict=True)):
                vector = [.26 if i == index else 0. for i in range(len(CASES))] + [.9656]
                passage = add_mail(store, conn, source, f"{code}-e{n}", f"{code} {aspect}",
                                   quote + (" 변경 전 기록과 참고 사항 " * 34), sent=f"2025-08-{21 + n}")
                write_vector(conn, passage, fixture.index_key, pack_vector(vector))
    metrics = []
    for code, question, aspects, quotes, _ in CASES:
        query = f"작년 하반기 김 부장이 보낸 {code} {question} 알려줘"
        fixture.documents = []
        profile = SearchProfile("rag_fixture")

        def complete(_instructions, payload, **_options):
            excerpts = json.loads(payload)["evidence"]
            claims = [{"text": quote, "citations": [{"source": item["source"], "quote": quote}]}
                      for quote in quotes for item in excerpts if quote in item["text"]]
            return {"status": "answered" if claims else "insufficient", "claims": claims}

        with store.search_session(profile):
            scope = {"source": source}
            if revised:
                proposed = {"sub_queries": [f"{code} {aspect}" for aspect in aspects], "keywords": [code],
                            "filters": {"sender": "김 부장", "date_from": "2025-07-01", "date_to": "2025-12-31"}}
                plan = answer.parse_rewrite(proposed, query)
                with patch.object(ai, "rewrite_query", return_value=plan):
                    rows, _, _, _ = answer.rewritten_evidence(store, ai, query, scope, "", limit=18, expand=True,
                                                             max_evidence=24)
            else:
                rows, _ = answer.evidence_for(store, ai, query, scope, "")
            with store.measure("context"):
                options = {"review_budget": 56000, "review_chars": 12000} if revised else {}
                _, review = Reviews().create(query, rows, **options)
            excerpts = review.excerpts if review else ()
            with store.measure("generation"), patch.object(provider, "complete", side_effect=complete):
                result = provider.answer(query, excerpts)
            by_id = {row["passage"]: row for row in rows}
            visible = [{**by_id[item.passage], "text": item.text} for item in excerpts]
            with store.measure("validation"):
                result = answer.validate(result, visible, query, text_for=lambda row: row["text"])
        profile.log(LOGGER, "rag_fixture")
        found = sum(any(quote in item for item in fixture.documents) for quote in quotes)
        verified = sum(any(claim["text"] == quote for claim in result["claims"]) for quote in quotes)
        metrics.append({"case": code, "first_stage_facts": found, "verified_answer_facts": verified, "expected": len(quotes)})
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-shard", type=int, default=2500)
    parser.add_argument("--dimensions", type=int, default=128)
    parser.add_argument("--repetitions", type=int, default=12)
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with tempfile.TemporaryDirectory(prefix="mail-rag-benchmark-") as directory:
        root = Path(directory)
        print(json.dumps({"search": search_benchmark(root, options.per_shard, options.dimensions, options.repetitions),
                          "quality": quality_benchmark(root)}, indent=2))
