"""Evaluate answer retrieval and grounded Markdown against an existing local index."""
import argparse
import json
import os
import sys
from contextlib import closing
from pathlib import Path
from time import perf_counter

from .answer import LocalAI, source_text, validate_markdown_answer
from .api_settings import ApiSettings
from .external import MAX_GATEWAY_EXCERPTS, pack_excerpts
from .intranet import IntranetAI
from .search_profile import SearchProfile
from .server import App, gateway_defaults, gateway_tls, generate_intranet_markdown, retrieve_answer_evidence
from .store import Store, normalized

FIELDS = {"expected_keywords": source_text, "expected_senders": lambda row: row["sender"],
          "expected_subjects": lambda row: row["subject"]}
TIMINGS = ("rewrite", "fts", "embed", "vector", "rerank", "followup", "generation")


def load_cases(path):
    cases = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected a nonempty JSON array of evaluation cases.")
    for number, case in enumerate(cases, 1):
        question = case.get("question") if isinstance(case, dict) else None
        if not isinstance(question, str) or not question.strip() or len(question) > 450:
            raise ValueError(f"Case {number} needs a question of 1–450 characters.")
        for field in FIELDS:
            values = case.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"Case {number}: {field} must be an array of nonempty strings.")
    return cases


def retrieval_recall(case, rows):
    expected, found = 0, 0
    for field, extract in FIELDS.items():
        targets = {normalized(value.strip()) for value in case.get(field, ())}
        texts = [normalized(extract(row)) for row in rows]
        expected += len(targets)
        found += sum(any(target in text for text in texts) for target in targets)
    return found, expected


def evaluate_case(store, ai, case, k, *, retrieval_only, external=None, workers=None):
    profile = SearchProfile("rag_eval")
    question = case["question"].strip()
    first_token = None

    def delta(text):
        nonlocal first_token
        if text.strip() and first_token is None:
            first_token = (perf_counter() - profile.started) * 1000

    budget = external.internal_budget if external and not retrieval_only else 0
    with store.search_session(profile):
        rows, scope, aspects, _ = retrieve_answer_evidence(
            store, ai, question, "", {"regular_only": True}, external, budget, budget, retrieval_only=retrieval_only, limit=k,
            workers=workers, worker_check=None if retrieval_only else (lambda: None),
        )
        found, expected = retrieval_recall(case, rows[:k])
        result = None
        if not retrieval_only:
            excerpts = pack_excerpts(question, rows, budget, with_context=True, chars=external.review_chars,
                                     max_excerpts=MAX_GATEWAY_EXCERPTS, gap_aspects=aspects)
            if excerpts:
                status, result = generate_intranet_markdown(
                    store, external, question, excerpts, scope, gap_aspects=aspects, on_delta=delta,
                )
                if status != 200:
                    result = {"status": result["error"]}
            else:
                result = validate_markdown_answer("", (), gap_aspects=aspects)
    with profile.lock:
        timings = {stage: round(profile.ms[stage], 3) for stage in TIMINGS}
    timings["TTFT"] = round(first_token, 3) if first_token is not None else None
    timings["total"] = round((perf_counter() - profile.started) * 1000, 3)
    return {
        "question": question, "retrieved": min(k, len(rows)), "expected_found": found, "expected_total": expected,
        "recall_at_k": found / expected if expected else None, "latency_ms": timings,
        "citation_passed": result["status"] in {"answered", "partial", "ambiguous"} if result is not None else None,
        "valid_sources": len(result.get("sources", ())) if result is not None else None,
        "answer_status": result["status"] if result is not None else None,
    }


def evaluate(store, ai, cases, k, *, retrieval_only, external=None, workers=None):
    rows = [evaluate_case(store, ai, case, k, retrieval_only=retrieval_only, external=external, workers=workers)
            for case in cases]
    recalls = [row["recall_at_k"] for row in rows if row["recall_at_k"] is not None]
    return {
        "k": k, "retrieval_only": retrieval_only, "cases": rows,
        "mean_recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "citation_pass_rate": None if retrieval_only else sum(row["citation_passed"] for row in rows) / len(rows),
        "valid_sources_total": None if retrieval_only else sum(row["valid_sources"] for row in rows),
    }


def print_table(report, output):
    def cell(value):
        return "—" if value is None else f"{value:.2f}"

    headings = ["Case", f"Recall@{report['k']}", *TIMINGS, "TTFT", "total", "Citations"]
    print("| " + " | ".join(headings) + " |", file=output)
    print("| " + " | ".join("---" for _ in headings) + " |", file=output)
    for number, row in enumerate(report["cases"], 1):
        latency = row["latency_ms"]
        values = [str(number), cell(row["recall_at_k"]), *(cell(latency[key]) for key in (*TIMINGS, "TTFT", "total"))]
        passed = row["citation_passed"]
        values.append("—" if passed is None else f"{'pass' if passed else 'fail'} ({row['valid_sources']})")
        print("| " + " | ".join(values) + " |", file=output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / (
        "OutlookMailSearch" if os.name == "nt" else ".outlook-mail-search"
    )
    parser.add_argument("--data", type=Path, default=default_data, help="Directory containing mail.sqlite")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("-k", type=int, default=10, help="Number of ranked passages used for recall, up to 32")
    parser.add_argument("--retrieval-only", action="store_true", help="Skip generative rewrite, diagnosis, and answers")
    parser.add_argument("--embedding-model", default="", help="Optional installed Ollama model for local vector search")
    parser.add_argument("--ollama-port", type=int, default=11435)
    parser.add_argument("--gateway-retrieval", action="store_true",
                        help="Use saved intranet embedding/rerank settings in retrieval-only mode")
    parser.add_argument("--json-out", type=Path, help="Write JSON to this path instead of stdout; table goes to stderr")
    args = parser.parse_args(argv)
    if not 1 <= args.k <= MAX_GATEWAY_EXCERPTS:
        parser.error(f"-k must be between 1 and {MAX_GATEWAY_EXCERPTS}")
    if not 0 < args.ollama_port < 65536:
        parser.error("--ollama-port must be between 1 and 65535")
    if not (args.data / "mail.sqlite").is_file():
        parser.error("--data must point to an existing onlyou index")
    try:
        cases = load_cases(args.cases)
        ai = LocalAI("", args.embedding_model, args.ollama_port)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        with closing(Store(args.data)) as store:
            if args.retrieval_only and not args.gateway_retrieval:
                report = evaluate(store, ai, cases, args.k, retrieval_only=True)
            else:
                preset = Path(__file__).resolve().parent.parent / "gateway-defaults.json"
                config = gateway_defaults(preset)
                env_key = os.environ.get("LITELLM_API_KEY")
                if env_key and config["gateway_url"]:
                    try:
                        gateway = IntranetAI(env_key, {"answer": "", "embedding": "", "rerank": ""},
                                             origin=config["gateway_url"],
                                             tls=gateway_tls(config["ca_file"], False))
                    except ValueError:
                        parser.error("Check LITELLM_API_KEY and the configured intranet gateway")
                    ai.configure_embedding(args.embedding_model, gateway)
                with App(0, store, None, ai, api_settings=ApiSettings(args.data), gateway_preset=preset) as app:
                    external = app.external
                    if not args.retrieval_only and (not external or not external.intranet or not external.internal_budget):
                        parser.error("Select and save a trusted intranet answer model in onlyou, or use --retrieval-only")
                    report = evaluate(store, ai, cases, args.k, retrieval_only=args.retrieval_only, external=external,
                                      workers=app.retrieval_workers)
    finally:
        ai.shutdown()
    print_table(report, sys.stderr)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_out:
        args.json_out.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)


if __name__ == "__main__":
    main()
