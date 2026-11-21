"""Measure design-question evidence hit rate on the synthetic corpus (PRD 02 §7).

Indexes the corpus from tools.eval_corpus into a throwaway store, then asks each
scenario question through /api/design/preview. A fragment counts as found when
it appears in one of the outbound excerpts (max 5); the secondary metric checks
the top-10 evidence rows. Reports the PRD target of 90% scenario coverage.
"""
import argparse
import http.client
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mailsearch.answer import LocalAI
from mailsearch.external import ExternalAI
from mailsearch.indexer import Indexer
from mailsearch.server import App
from mailsearch.store import Store
from tools.eval_corpus import SCENARIOS, build


class EmbeddingFixture(BaseHTTPRequestHandler):
    """Deterministic stand-in for Ollama: same vector shape as the test suite."""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._reply({"models": [{"name": "embed:latest"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/embed":
            self._reply({"embeddings": [[1.0, .05, .12] for _ in payload["input"]]})
            return
        self._reply({"response": "{}"})

    def _reply(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def await_ready(store, source, timeout=90):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = store.source(source)
        if (row["state"] if row else None) in {"ready", "error", "empty"}:
            return row
        time.sleep(.05)
    raise AssertionError(f"corpus import did not finish: {dict(row) if row else None}")


def run(corpus_root):
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingFixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    workspace = tempfile.TemporaryDirectory()
    results = []
    try:
        build(corpus_root)
        store = Store(Path(workspace.name) / "index")
        ai = LocalAI("test", "embed", fixture.server_port)
        indexer = Indexer(store, ai=ai)
        app = App(0, store, indexer, ai)
        worker = threading.Thread(target=app.serve_forever, daemon=True)
        worker.start()
        # preview authorization requires a configured external; nothing is ever sent.
        app.external = ExternalAI("https://api.example.invalid/v1/chat/completions",
                                  "eval", "placeholder-token")
        connection = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=60)

        def call(route, data):
            connection.request("POST", route, json.dumps(data),
                               {"Content-Type": "application/json", "X-Mail-Session": app.token})
            return json.loads(connection.getresponse().read())

        try:
            registered = call("/api/sources", {"path": str(corpus_root.resolve())})
            ready = await_ready(store, registered["source"])
            if ready["state"] != "ready":
                raise AssertionError(f"corpus import ended as {ready['state']} ({ready['issue']})")
            for scenario in SCENARIOS:
                started = time.monotonic()
                preview = call("/api/design/preview", {"query": scenario["question"], "filters": {}})
                elapsed = time.monotonic() - started
                excerpts = [item["text"] for item in preview.get("excerpts", [])]
                top10 = [row.get("preview", "") for row in preview.get("results", [])[:10]]
                missed = [fragment for fragment in scenario["must_find"]
                          if not any(fragment in text for text in excerpts)]
                missed10 = [fragment for fragment in scenario["must_find"]
                            if not any(fragment in text for text in top10)]
                results.append({"scenario": scenario, "excerpts": len(excerpts), "missed": missed,
                                "missed10": missed10, "seconds": elapsed})
        finally:
            indexer.stop()
            app.shutdown()
            app.server_close()
            worker.join(timeout=10)
    finally:
        fixture.shutdown()
        fixture.server_close()
        workspace.cleanup()
    return results


def report(results, corpus_root):
    lines = [f"# 설계 이력 질문 근거 적중률 (PRD 02 §7)", "", f"코퍼스: {corpus_root}", "",
             "| 시나리오 | 발췌 | 상위10 미적중 | 발췌 미적중 | 소요 |", "|---|---|---|---|---|"]
    for item in results:
        scenario = item["scenario"]
        lines.append(f"| {scenario['id']} | {item['excerpts']} "
                     f"| {', '.join(item['missed10']) or '—'} | {', '.join(item['missed']) or '—'} "
                     f"| {item['seconds']:.1f}s |")
    full = sum(1 for item in results if not item["missed"])
    top10_full = sum(1 for item in results if not item["missed10"])
    ratio = full / len(results) if results else 0
    lines += ["", f"시나리오 전체 적중(발췌): **{full}/{len(results)} ({ratio:.0%})** — 목표 90%",
              f"시나리오 전체 적중(상위 10건): {top10_full}/{len(results)} ({top10_full / len(results):.0%})",
              "", "임베딩은 결정론적 fixture(전 단위벡터)를 사용하므로 의미 검색 기여가 실제 bge-m3과 다를 수 있다."]
    return lines, ratio


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", default=None,
                        help="corpus root (created/refreshed from tools.eval_corpus)")
    parser.add_argument("--out", default=None, help="also write the report as markdown")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    corpus_root = Path(args.folder) if args.folder else Path(tempfile.mkdtemp(prefix="eval-design-"))
    results = run(corpus_root)
    lines, ratio = report(results, corpus_root)
    print("\n".join(lines))
    if args.out:
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"보고서 저장: {args.out}")
    return 0 if ratio >= .9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
