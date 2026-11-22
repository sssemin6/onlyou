"""Loopback-only web application."""
import argparse
import datetime
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .answer import (PCF_INSTRUCTIONS, LocalAI, map_citations, pcf_candidates, pcf_retrieval, pcf_word, delivery_date_answer,
                     evidence_for, merge_rows, public_row, validate)
from .bundled import prepare as prepare_bundled
from .external import DEFAULT_MODEL, DEFAULT_URL, MAX_EXCERPTS, MAX_PCF_CELLS, ExternalAI, ExternalRequestError, Reviews, pcf_batches, pcf_input_fits
from .extract import MAX_ATTACHMENT
from .filesystem import local_path
from .indexer import ImportIssue, Indexer
from .pcf import PCF_CELL
from .store import Store, normalized, query_words, quoted_plan

STATIC = Path(__file__).with_name("static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8")}
MAX_REQUEST = 16 * 1024


class SessionLock:
    def __init__(self, folder):
        self.path = folder / "session.lock"

    def __enter__(self):
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.handle.close()
                raise
        else:
            import fcntl
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.handle.close()
                raise
        return self

    def __exit__(self, *exc):
        self.handle.close()


class App(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, store, indexer, ai, external=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.store, self.indexer, self.ai = store, indexer, ai
        self.external = external
        self.reviews = Reviews()
        self.outbound = threading.BoundedSemaphore(1)
        self.token = secrets.token_urlsafe(32)
        self.generation = threading.BoundedSemaphore(1)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.last_seen = 0.0


SNIPPET_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def best_snippet(text, tokens, size=170):
    """The sentence carrying the most query tokens, so results show why they matched."""
    if not tokens or not text:
        return None
    lowered = [token.casefold() for token in tokens]
    best, best_hits = None, 0
    for sentence in SNIPPET_SPLIT.split(text):
        hits = sum(1 for token in lowered if token in sentence.casefold())
        if hits > best_hits:
            best, best_hits = sentence, hits
    if best is None:
        return None
    best = best.strip()
    return best[:size] + ("…" if len(best) > size else "")


def filters_from(get):
    filters = {}
    for name in ("sender", "recipient", "subject", "body"):
        raw = get(name, "")
        if not isinstance(raw, str):
            raise ValueError(name)
        if raw.strip():
            filters[name] = raw.strip()[:200]
    attachment = get("attachment", "")
    if attachment in ("yes", "no"):
        filters["attachment"] = attachment
    tab = get("tab", "")
    if tab in ("mail", "document", "pcf"):
        filters["tab"] = tab
    # PCF scope fields (선급·FLAG·선주·호선·도면명): matched against file names and passage
    # text until the attributes become structured data.
    for name in ("class", "flag", "owner", "hull", "drawing"):
        raw = get(name, "")
        if isinstance(raw, str) and raw.strip():
            filters[name] = raw.strip()[:200]
    source = get("source", "")
    if source:
        filters["source"] = int(source)
    for name in ("before", "after"):
        value = get(name, "")
        if value:
            date = datetime.date.fromisoformat(value)
            if name == "before":
                date += datetime.timedelta(days=1)
            local = datetime.datetime.combine(date, datetime.time()).astimezone(datetime.timezone.utc)
            filters[name] = local.isoformat(timespec="seconds")
    return filters


class Handler(BaseHTTPRequestHandler):
    server_version = "PSTMailSearch"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, *_args):
        # Paths and query parameters contain private mail metadata.
        return

    def send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode())

    def authorized(self, route):
        host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        if self.headers.get("Host") != host or (origin and origin != self.server.origin) or site == "cross-site":
            self.send_json(403, {"error": "local_only"})
            return False
        if route.startswith("/api/") and not hmac.compare_digest(
            self.headers.get("X-Mail-Session", "").encode(), self.server.token.encode()
        ):
            self.send_json(403, {"error": "session"})
            return False
        return True

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self):
        try:
            self.get_route()
        except sqlite3.Error:
            self.send_json(503, {"error": "index_unavailable"})

    def get_route(self):
        route = urlsplit(self.path)
        if not self.authorized(route.path):
            return
        if route.path in FILES:
            file, content_type = FILES[route.path]
            body = (STATIC / file).read_bytes()
            if route.path == "/":
                body = body.replace(b"__LOCAL_SESSION__", self.server.token.encode())
            self.send_bytes(200, content_type, body)
        elif route.path == "/api/sources":
            sources = self.server.store.sources()
            ids = set(self.server.store.allowed_sources(sources=sources))
            external = self.server.external
            self.send_json(200, {
                "sources": [{**row, "available": row["id"] in ids,
                             "updates": self.server.indexer.pending_updates(row)} for row in sources],
                "model": self.server.ai.model, "embedding": self.server.ai.embedding_model,
                "external": external.public if external else None,
            })
        elif route.path == "/api/ping":
            self.server.last_seen = time.time()
            self.send_json(200, {"ok": True})
        elif route.path == "/api/external/settings":
            external = self.server.external
            self.send_json(200, {
                "configured": external.public if external else None,
                "defaults": {"destination": DEFAULT_URL, "model": DEFAULT_MODEL},
            })
        elif re.fullmatch(r"/api/evidence/\d{1,16}", route.path):
            row = self.server.store.open_passage(int(route.path.rsplit("/", 1)[-1]))
            if row is None:
                self.send_json(404, {"error": "evidence_missing"})
            else:
                case = self.server.store.pcf_case(row)
                payload = {**public_row(row), "text": row["text"], "body": row["body"],
                           "start": row["start"], "attachments": row["attachments"]}
                if case:
                    payload["pcf"] = {"number": case["number"], "warnings": case["warnings"], "revision": case["revision"],
                                      "cells": [{**item["pcf_cell"], "location": item["location"],
                                                 "passage": item["passage"], "text": item["text"]}
                                                for item in case["rows"]]}
                self.send_json(200, payload)
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        route = urlsplit(self.path).path
        if not self.authorized(route):
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("content_type")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST:
                raise ValueError("content_length")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("body")
            if route == "/api/sources":
                source = self.server.indexer.register(payload["path"])
                self.send_json(202, {"source": source})
            elif route == "/api/source-action":
                self.server.indexer.command(int(payload["source"]), payload["action"])
                self.send_json(202, {})
            elif route in {"/api/search", "/api/candidates", "/api/answer", "/api/external/preview", "/api/pcf/preview"}:
                self.query(route, payload)
            elif route in {"/api/external/send", "/api/pcf/send"}:
                self.send_external(payload, pcf=route == "/api/pcf/send")
            elif route == "/api/external/settings":
                self.change_external_settings(payload)
            else:
                self.send_json(404, {"error": "not_found"})
        except (ValueError, TypeError, KeyError, OverflowError):
            self.send_json(400, {"error": "invalid_input"})
        except ImportIssue as exc:
            self.send_json(409, {"error": str(exc)})
        except sqlite3.Error:
            self.send_json(503, {"error": "index_unavailable"})

    def query(self, route, payload):
        query, context, options = payload.get("query", ""), payload.get("context", ""), payload.get("filters", {})
        if not isinstance(query, str) or not isinstance(context, str) or not isinstance(options, dict):
            raise ValueError("query")
        query, context = query.strip()[:450], context.strip()[:450]
        filters = filters_from(options.get)
        store, ai = self.server.store, self.server.ai
        if route == "/api/search":
            plan = quoted_plan(query)
            if plan is not None:
                rows = store.exact_search(plan, filters)
            else:
                rows = store.lexical(query, filters, limit=180)
                if query and ai.embedding_model:
                    try:
                        nearby = store.semantic(ai.embed([query], timeout=6)[0], ai.embedding_model, filters)
                        rows = merge_rows(rows, nearby, limit=60)
                    except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, http.client.HTTPException):
                        pass
            tokens = query_words(query, fuzzy=True)[:8]
            distinct = {}
            for row in rows:
                cell = PCF_CELL.fullmatch(row["location"]) if filters.get("tab") == "pcf" else None
                key = (row["id"], cell[1]) if cell else row["fingerprint"]
                shown = distinct.setdefault(key, public_row(row))
                if "similarity" in row and shown.get("similarity") is None:
                    shown["similarity"] = round(row["similarity"], 3)
                if shown.get("matched") is None:
                    haystack = normalized(f"{row['subject']} {row['text']}")
                    matched = [token for token in tokens if normalized(token) in haystack]
                    shown["matched"] = matched
                    shown["snippet"] = best_snippet(row["text"], matched)
            self.send_json(200, {"results": list(distinct.values())[:20]})
            return
        if not query:
            self.send_json(400, {"error": "question_required"})
            return
        external = self.server.external
        if route in {"/api/external/preview", "/api/pcf/preview"} and not external:
            self.send_json(403, {"error": "external_disabled"})
            return
        pcf = route == "/api/pcf/preview"
        if pcf:
            filters["tab"] = "pcf"
            if self.preview_pcf_word(payload.get("passage"), query, context, filters, external):
                return
            evidence, mode = pcf_retrieval(store, ai, query, filters, context)
            evidence = [row for row in evidence if not pcf_word(row)]
        else:
            evidence, mode = evidence_for(store, ai, query, filters, context,
                                          semantic=(route in {"/api/answer", "/api/external/preview"}))
        if route in {"/api/external/preview", "/api/pcf/preview"}:
            token, review = self.server.reviews.create(query, evidence, external.review_generation, pcf=pcf)
            by_passage = {row["passage"]: public_row(row) for row in evidence}
            self.send_json(200, {
                **external.public, "review": token, "question": query,
                "excerpts": [{"id": item.number, "text": item.text, "source": by_passage[item.passage]}
                             for item in review.excerpts] if review else [],
                "results": [public_row(row) for row in evidence],
            })
            return
        rows = [public_row(row) for row in evidence]
        if route == "/api/candidates":
            self.send_json(200, {"results": rows})
            return
        result = {"status": "insufficient", "claims": []}
        if not ai.model:
            result["status"] = "model_not_configured"
        elif evidence and not self.server.generation.acquire(blocking=False):
            result["status"] = "model_busy"
        elif evidence:
            try:
                full_question = f"{query} {context}"
                direct = delivery_date_answer(query, evidence, context)
                proposed = ai.answer(query, evidence, context) if direct is None else None
                # Recheck visibility after inference, including source removal during generation.
                visible = {r["passage"] for r in store.passages([r["passage"] for r in evidence], filters)}
                evidence = [r for r in evidence if r["passage"] in visible]
                rows = [public_row(row) for row in evidence]
                result = delivery_date_answer(query, evidence, context) if direct is not None else None
                if result is None:
                    result = validate(proposed, evidence, full_question)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, http.client.HTTPException):
                result["status"] = "model_unavailable"
            finally:
                self.server.generation.release()
        self.send_json(200, {**result, "results": rows, "retrieval": mode})

    def preview_pcf_word(self, passage, question, context, filters, external):
        store = self.server.store
        if passage is not None:
            if type(passage) is not int or not 1 <= passage < 10**16:
                raise ValueError("passage")
            hits, number = store.passages([passage], filters), None
        else:
            hits, number = pcf_candidates(store, self.server.ai, question, filters, context)
        seeds = {}
        for hit in hits:
            match = PCF_CELL.fullmatch(hit["location"])
            if match:
                seeds.setdefault((hit["id"], match[1]), hit)
        if not seeds and passage is None:
            if number is None and not any(pcf_word(row) for row in hits):
                return False
            if number is not None:
                return False
        if len(seeds) != 1:
            self.send_json(200, {
                "results": [public_row(row) for row in seeds.values()] or [public_row(row) for row in hits[:10]],
                "choose_case": bool(seeds), "pcf_issue": "COMMENT을 선택하세요." if seeds else
                "구조화된 COMMENT을 찾지 못했습니다. 색인 상태와 원본 표를 확인하세요.",
            })
            return True
        seed = next(iter(seeds.values()))
        case = store.pcf_case(seed, filters)
        if case is None:
            self.send_json(409, {"error": "external_source_changed"})
            return True
        rows = case.pop("rows")
        case["cells"] = {row["passage"]: row["pcf_cell"] for row in rows}
        results = [public_row(row) for row in rows]
        budget = external.pcf_budget
        if not budget:
            self.send_json(200, {"results": [public_row(seed)], "pcf_issue":
                                 "PCF 전체 답변은 등록된 사내 API 주소에서 사용할 수 있습니다. 원문은 열 수 있습니다."})
            return True
        token, review = self.server.reviews.create(
            question, rows, external.review_generation, pcf=True, case=case)
        try:
            batches = pcf_batches(question, review.excerpts, case, budget)
        except ValueError:
            self.server.reviews.take(token)
            self.send_json(200, {"results": [public_row(seed)], "pcf_issue":
                                 "이 COMMENT은 현재 전송 한도를 초과합니다. 셀 원문을 확인할 수 있습니다."})
            return True
        self.send_json(200, {
            **external.public, "review": token, "question": question, "results": results,
            "pcf_coverage": {"expected": case["expected"], "included": len(rows),
                             "calls": len(batches) + (len(batches) > 1), "warnings": case["warnings"],
                             "revision": case["revision"]},
            "excerpts": [{"id": item.number, "text": item.text, "source": source}
                         for item, source in zip(review.excerpts, results)],
        })
        return True

    def send_pcf_word(self, external, review, excerpts):
        store = self.server.store
        ids = [item.passage for item in excerpts]
        case = review.case

        def visible():
            rows = store.passages(ids, {"tab": "pcf"})
            if len(rows) != len(ids):
                return {}
            try:
                row = rows[0]
                root = Path(row["path"])
                original = local_path(root / row["item"] if root.is_dir() else root, strict=True)
                if original != root and not original.is_relative_to(root):
                    return {}
                with original.open("rb") as handle:
                    digest = hashlib.sha256(handle.read(MAX_ATTACHMENT + 1)).hexdigest()
                if digest != case["source_version"]:
                    return {}
            except (OSError, ValueError, RuntimeError):
                return {}
            return {row["passage"]: row for row in rows}

        budget = external.pcf_budget
        if not budget:
            self.send_json(409, {"error": "external_review_expired"})
            return
        try:
            batches = pcf_batches(review.question, excerpts, case, budget)
        except ValueError:
            self.send_json(400, {"error": "pcf_input_too_large"})
            return
        passage_by_number = {item.number: item.passage for item in excerpts}
        claims, unknowns, results, statuses, used_lengths = [], [], {}, [], {}
        failed = False
        for batch in batches:
            if not visible():
                self.send_json(409, {"error": "external_source_changed"})
                return
            mapping = {item["source"]: passage_by_number[item["source"]] for item in batch["evidence"]}
            try:
                result = map_citations(external.answer_pcf(batch), mapping)
            except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, http.client.HTTPException):
                failed = True
                break
            rows = visible()
            if not rows:
                self.send_json(409, {"error": "external_source_changed"})
                return
            verified = []
            for item in batch["evidence"]:
                passage = mapping[item["source"]]
                row = rows[passage]
                verified.append({**row, "text": item["text"], "pcf_cell": case["cells"][passage]})
                used_lengths[passage] = used_lengths.get(passage, 0) + len(item["text"])
                results[passage] = public_row(row)
            checked = validate(result, verified, review.question, text_for=lambda row: row["text"],
                               pcf=True, claim_limit=12, complete=batch["coverage"]["complete"])
            statuses.append(checked["status"])
            for claim in checked["claims"]:
                if claim not in claims:
                    claims.append(claim)
            if isinstance(result, dict) and isinstance(result.get("unknowns"), list):
                unknowns.extend(value[:220] for value in result["unknowns"][:8] if isinstance(value, str))
        if not visible():
            self.send_json(409, {"error": "external_source_changed"})
            return
        included = sum(used_lengths.get(item.passage, -1) == len(item.text) for item in excerpts)
        status = statuses[0] if len(batches) == 1 and statuses else "insufficient"
        if claims and (len(batches) > 1 or not batches[0]["coverage"]["complete"] or unknowns):
            status = "partial"
        synthesized = False
        calls = len(statuses)
        if len(batches) > 1 and claims and not failed and included == case["expected"] and not case["warnings"]:
            rows = visible()
            if not rows:
                self.send_json(409, {"error": "external_source_changed"})
                return
            try:
                combined = combine_pcf_findings(external, review, rows, claims, budget)
            except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, http.client.HTTPException):
                failed = True
                combined = None
            if not visible():
                self.send_json(409, {"error": "external_source_changed"})
                return
            if combined is not None:
                calls += 1
                if combined["claims"]:
                    synthesized = True
                    claims, unknowns, status = combined["claims"], combined["unknowns"], combined["status"]
        if failed and not statuses:
            status = "external_unavailable"
        self.send_json(200, {
            "status": status, "claims": claims, "unknowns": list(dict.fromkeys(unknowns))[:8],
            "results": list(results.values()), "provider": "external",
            "pcf_coverage": {"included": included, "expected": case["expected"], "calls": calls,
                             "warnings": case["warnings"], "failed": failed,
                             "split": len(batches) > 1, "synthesized": synthesized},
        })

    def send_external(self, payload, pcf=False):
        server = self.server
        if not server.external:
            self.send_json(403, {"error": "external_disabled"})
            return
        token, selected = payload.get("review"), payload.get("excerpts")
        if (not isinstance(token, str) or not re.fullmatch(r"[\w-]{32}", token, re.ASCII)
                or not isinstance(selected, list) or not 1 <= len(selected) <= (MAX_PCF_CELLS if pcf else MAX_EXCERPTS)
                or any(type(number) is not int for number in selected)):
            raise ValueError("external_review")
        if not server.outbound.acquire(blocking=False):
            self.send_json(409, {"error": "external_busy"})
            return
        try:
            external = server.external
            if not external:
                self.send_json(403, {"error": "external_disabled"})
                return
            review = server.reviews.take(token)
            if review is None or review.generation is not external.review_generation or review.pcf != pcf:
                self.send_json(409, {"error": "external_review_expired"})
                return
            excerpts = [item for item in review.excerpts if item.number in selected]
            if len(excerpts) != len(selected):
                raise ValueError("external_excerpts")
            if review.case is not None:
                self.send_pcf_word(external, review, excerpts)
                return
            if len(excerpts) > MAX_EXCERPTS:
                raise ValueError("external_excerpts")
            ids = [item.passage for item in excerpts]
            scope = {"tab": "pcf"} if pcf else {}
            rows = server.store.passages(ids, scope)
            if len(rows) != len(ids):
                self.send_json(409, {"error": "external_source_changed"})
                return
            try:
                if pcf:
                    result = external.answer(review.question, excerpts,
                                             instructions=PCF_INSTRUCTIONS, token_limit=2800, timeout=90)
                else:
                    result = external.answer(review.question, excerpts)
            except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, http.client.HTTPException) as exc:
                self.send_json(200, {
                    "status": "external_unavailable", "claims": [], "results": [], "provider": "external",
                    "reason": type(exc).__name__,
                })
                return
            visible = {row["passage"]: row for row in server.store.passages(ids, scope)}
            verified = [{**visible[item.passage], "text": item.text} for item in excerpts if item.passage in visible]
            checked = validate(result, verified, review.question, text_for=lambda row: row["text"], pcf=pcf)
            self.send_json(200, {
                **checked, "results": [public_row(row) for row in verified], "provider": "external",
            })
        finally:
            server.outbound.release()


    def change_external_settings(self, payload):
        action = payload.get("action")
        if action not in ("apply", "test", "disconnect"):
            raise ValueError("external_settings_action")
        server = self.server
        if not server.outbound.acquire(blocking=False):
            self.send_json(409, {"error": "external_busy"})
            return
        try:
            if action == "disconnect":
                server.external = None
                server.reviews.clear()
                self.send_json(200, {"configured": None})
                return
            url, model, key = (payload.get(name, "") for name in ("destination", "model", "key"))
            if not all(isinstance(value, str) for value in (url, model, key)):
                raise ValueError("external_settings")
            try:
                external = ExternalAI.from_settings(url.strip(), model.strip(), key.strip(), server.external)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if action == "apply":
                server.external = external
                server.reviews.clear()
                self.send_json(200, {"configured": external.public})
                return
            try:
                external.test()
            except ExternalRequestError as exc:
                status = exc.status
                if status in (401, 403):
                    error = "external_test_auth"
                elif status in (400, 404, 422):
                    error = "external_test_endpoint"
                elif status == 429:
                    error = "external_test_limit"
                else:
                    error = "external_test_failed"
                self.send_json(502, {"error": error})
                return
            except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException):
                self.send_json(502, {"error": "external_test_failed"})
                return
            self.send_json(200, {"ok": True})
        finally:
            server.outbound.release()



def combine_pcf_findings(external, review, rows, claims, budget):
    """Combine verified batches using only their cited original excerpts."""
    evidence, findings, verified, source_ids = [], [], [], {}
    for claim in claims:
        ids = []
        for ref in claim["citations"]:
            passage, quote = ref["passage"], ref["quote"]
            key = (passage, quote)
            if key not in source_ids:
                source = len(evidence) + 1
                source_ids[key] = source
                meta = review.case["cells"][passage]
                evidence.append({"source": source, "case": "C1", **meta, "text": quote})
                verified.append({**rows[passage], "passage": source, "text": quote, "pcf_cell": meta})
            ids.append(source_ids[key])
        findings.append({"sources": ids})
    content = {"question": review.question, "coverage": {"complete": True},
               "verified_findings": findings, "evidence": evidence}
    if not pcf_input_fits(content, budget):
        return None
    result = external.answer_pcf(content)
    checked = validate(result, verified, review.question, text_for=lambda row: row["text"], pcf=True, claim_limit=12)
    original_by_source = {source: key[0] for key, source in source_ids.items()}
    for claim in checked["claims"]:
        for ref in claim["citations"]:
            ref["passage"] = original_by_source[ref["passage"]]
    unknowns = result.get("unknowns", []) if isinstance(result, dict) else []
    checked["unknowns"] = [value[:220] for value in unknowns[:8] if isinstance(value, str)] if isinstance(unknowns, list) else []
    if checked["unknowns"] and checked["status"] == "answered":
        checked["status"] = "partial"
    return checked


def main(argv=None):
    parser = argparse.ArgumentParser(description="Outlook PST mail search on this PC")
    default_data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / ("OutlookMailSearch" if os.name == "nt" else ".outlook-mail-search")
    parser.add_argument("--data", type=Path, default=default_data)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--readpst", default="readpst")
    parser.add_argument("--pdftotext", default="pdftotext")
    parser.add_argument("--model", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--ollama-port", type=int, default=11435)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.ollama_port < 65536 or not 0 <= args.port < 65536 or args.port == args.ollama_port:
        parser.error("Invalid port configuration")
    try:
        external = ExternalAI.from_environment()
    except ValueError:
        external = None
        print("External API disabled: check MAILSEARCH_EXTERNAL_MODEL, MAILSEARCH_EXTERNAL_URL and MAILSEARCH_EXTERNAL_API_KEY.", file=sys.stderr)
    finally:
        os.environ.pop("MAILSEARCH_EXTERNAL_API_KEY", None)
    try:
        root = local_path(args.data)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        models_dir = None
        if args.embedding_model and args.embedding_model.split(":")[0] == "bge-m3":
            # The repository ships the model split into parts; join it once on first run.
            models_dir = prepare_bundled(root / "ollama-models")
        ai = LocalAI(args.model, args.embedding_model, args.ollama_port, models_dir=models_dir)
        with SessionLock(root):
            store = Store(root)
            indexer = Indexer(store, args.readpst, args.pdftotext, ai)
            with App(args.port, store, indexer, ai, external) as app:
                indexer.wake()
                print(app.origin, flush=True)
                if not args.no_browser:
                    webbrowser.open(app.origin)
                    # Closing every app page stops the server (and its Ollama).
                    def watch_page():
                        while True:
                            time.sleep(3)
                            seen = app.last_seen
                            if seen and time.time() - seen > 12:
                                app.shutdown()
                                return
                    threading.Thread(target=watch_page, daemon=True).start()
                try:
                    app.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    indexer.stop()
                    ai.shutdown()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"Startup failed ({type(exc).__name__}). Check the data folder, port, and SQLite 3.42+ with FTS5.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
