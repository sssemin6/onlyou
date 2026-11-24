"""Loopback-only web application."""
import argparse
import datetime
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlsplit

from .answer import (PCF_INSTRUCTIONS, LocalAI, map_citations, pcf_candidates, pcf_retrieval, pcf_word, delivery_date_answer,
                     evidence_for, merge_rows, public_row, validate)
from .api_settings import ApiSettings, write_api_settings
from .embedding import (CA_FILE_ENV, GatewayTLSOptions, MODEL as BGE_MODEL, PATH as EMBEDDING_PATH,
                        gateway_origin, logger as gateway_logger)
from .external import DEFAULT_MODEL, DEFAULT_URL, MAX_EXCERPTS, MAX_INTERNAL_EXCERPTS, MAX_PCF_CELLS, ExternalAI, ExternalRequestError, Reviews, pcf_batches, pcf_input_fits
from .extract import MAX_ATTACHMENT
from .filesystem import local_path, source_path
from .indexer import ImportIssue, Indexer, runtime_logger
from .intranet import CHAT_PATH, LOCAL_EMBEDDING, RERANK_PATH, IntranetAI, available_models, validate_selections
from .ollama_setup import OllamaSetup
from .pcf import PCF_CELL
from .store import Store, normalized, pack_vector, query_words, quoted_plan

STATIC = Path(__file__).with_name("static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/fonts/onlyou-display.ttf": ("fonts/onlyou-display.ttf", "font/ttf"),
}
MAX_REQUEST = 16 * 1024
PAGE_IDLE_SECONDS = 12


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

    def __init__(self, port, store, indexer, ai, external=None, *, api_settings=None, gateway_preset=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.store, self.indexer, self.ai = store, indexer, ai
        self.ollama_setup = OllamaSetup(ai)
        self.external = external
        self.generic_answer = external
        self.gateway_preset = gateway_preset
        self.gateway_preset_lock = threading.Lock()
        self.gateway_fields = gateway_defaults(gateway_preset)
        self.gateway_test_origin = self.gateway_fields["gateway_url"] if self.gateway_fields.pop("test_unverified") else ""
        self.gateway_unverified_origin = ""
        self.rerank_unverified_origin = ""
        self.api_settings = api_settings
        self.api_profiles = api_settings.load() if api_settings else {}
        for profile in ("gateway", "external"):
            if not isinstance(self.api_profiles.get(profile), dict):
                self.api_profiles.pop(profile, None)
        self.embedding_selection = None
        self.reviews = Reviews()
        self.outbound = threading.BoundedSemaphore(1)
        self.token = secrets.token_urlsafe(32)
        self.generation = threading.BoundedSemaphore(1)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.last_seen = 0.0

        self.restore_api_settings()

    def watch_page(self, stopped):
        last_busy = 0.0
        while not stopped.wait(3):
            now = time.monotonic()
            if self.indexer.has_work() or self.ollama_setup.snapshot()["phase"] != "idle":
                last_busy = now
                continue
            seen = self.last_seen
            if seen and now - max(seen, last_busy) > PAGE_IDLE_SECONDS:
                runtime_logger.info("App shutdown requested (no browser heartbeat, indexing idle)")
                self.shutdown()
                return

    @property
    def persistent_keys(self):
        return bool(self.api_settings and self.api_settings.secure)

    def generic_provider(self):
        external = self.external
        return external if external and not external.intranet else self.generic_answer

    def remember_api(self, name, profile, answer):
        selected = ("gateway" if answer.intranet else "external") if answer else ""
        profiles = {**self.api_profiles, name: profile, "answer_provider": selected}
        if self.api_settings:
            self.api_settings.save(profiles)
        self.api_profiles = profiles

    def restore_api_settings(self):
        external = self.api_profiles.get("external")
        if isinstance(external, dict):
            previous = self.generic_answer
            self.generic_answer = None
            if external:
                try:
                    self.generic_answer = ExternalAI.from_settings(
                        external["destination"], external["model"], external.get("key", ""), previous,
                    )
                except (ValueError, TypeError, KeyError):
                    gateway_logger.warning("Could not restore saved external API configuration")
        gateway_answer = None
        gateway = self.api_profiles.get("gateway")
        if isinstance(gateway, dict):
            try:
                fields = gateway_fields(gateway, self.gateway_fields)
                origin = fields["gateway_url"]
                unverified = bool(origin and gateway.get("unverified_origin") == origin)
                rerank_unverified = bool(origin and gateway.get("rerank_unverified_origin") == origin)
                selections = gateway["selections"]
                validate_selections(selections, fields["answer_models"])
                current = self.ai.embedding_api
                try:
                    client = IntranetAI.from_settings(
                        gateway.get("key", ""), selections, current, origin=origin,
                        answer_models=fields["answer_models"],
                        tls=gateway_tls(fields["ca_file"], unverified, rerank_unverified=rerank_unverified),
                    )
                except ValueError:
                    client = None
                    gateway_logger.warning("Could not restore saved gateway credentials")
                local = BGE_MODEL if selections["embedding"] == LOCAL_EMBEDDING else ""
                self.ai.configure_embedding(local, client)
                self.embedding_selection = selections["embedding"]
                self.gateway_fields = fields
                self.gateway_unverified_origin = origin if unverified else ""
                self.rerank_unverified_origin = origin if rerank_unverified else ""
                gateway_answer = client.answer_client if client else None
            except (ValueError, TypeError, KeyError):
                self.api_profiles.pop("gateway", None)
                gateway_logger.warning("Could not restore saved gateway configuration")
        if "answer_provider" in self.api_profiles:
            preferred = self.api_profiles["answer_provider"]
            self.external = self.generic_answer if preferred == "external" else gateway_answer if preferred == "gateway" else None


def gateway_fields(payload, defaults):
    limits = {"gateway_url": 512, "answer_models": 3000, "ca_file": 2048}
    fields = {}
    for name, limit in limits.items():
        value = payload.get(name, defaults.get(name, ""))
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError("gateway_settings")
        fields[name] = value.strip()
    origin = gateway_origin(fields["gateway_url"])
    if fields["gateway_url"] and not origin:
        raise ValueError("gateway_url")
    fields["gateway_url"] = origin
    models = available_models(fields["answer_models"])["answer"]
    if any(name.strip() and name.strip() not in models for name in fields["answer_models"].split(",")):
        raise ValueError("gateway_models")
    fields["answer_models"] = ",".join(models)
    return fields


def gateway_tls(ca_file, runtime_unverified, test_unverified=False, *, rerank_unverified=False):
    paths = (CHAT_PATH, EMBEDDING_PATH) if runtime_unverified else ()
    if rerank_unverified:
        paths += (RERANK_PATH,)
    return GatewayTLSOptions(ca_file if not test_unverified else "", verify=not test_unverified,
                             unverified_paths=paths)


def gateway_environment():
    return {
        "gateway_url": gateway_origin(),
        "answer_models": os.environ.get("MAILSEARCH_GATEWAY_ANSWER_MODELS", ""),
        "ca_file": os.environ.get(CA_FILE_ENV, ""),
        "test_unverified": False,
    }


def validate_gateway_preset(preset):
    if not isinstance(preset, dict) or preset.keys() - {"gateway_url", "answer_models", "ca_file", "test_unverified"}:
        raise ValueError("gateway_preset")
    test_unverified = preset.get("test_unverified", False)
    if type(test_unverified) is not bool:
        raise ValueError("gateway_preset")
    fields = gateway_fields(preset, {})
    if not fields["gateway_url"]:
        raise ValueError("gateway_url")
    return {**fields, "test_unverified": test_unverified}


def read_gateway_preset(path):
    with path.open(encoding="utf-8-sig") as file:
        raw = file.read(MAX_REQUEST + 1)
    return validate_gateway_preset(json.loads(raw) if len(raw) <= MAX_REQUEST else None)


def gateway_defaults(preset_path):
    defaults = gateway_environment()
    if preset_path is None:
        return defaults
    try:
        fields = read_gateway_preset(preset_path)
    except FileNotFoundError:
        return defaults
    except (OSError, ValueError):
        gateway_logger.warning("Could not read gateway-defaults.json")
        return defaults
    if defaults["gateway_url"] and defaults["gateway_url"] != fields["gateway_url"]:
        return defaults
    return {name: value or fields[name] for name, value in defaults.items()}


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

    def send_bytes(self, status, content_type, body, *, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename is not None:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # The browser can close a local connection while a request is in flight.
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
            scan = route.query != "scan=0"
            ids = set(self.server.store.allowed_sources(sources=sources)) if scan else None
            external = self.server.external
            embedding = self.server.ai.embedding_api
            use_gateway = embedding and embedding.index_key
            self.send_json(200, {
                "sources": [{**row, "available": row["id"] in ids if scan else None,
                             "updates": self.server.indexer.pending_updates(row) if scan else [],
                             "pst_read": self.server.indexer.pst_progress(row)} for row in sources],
                "model": self.server.ai.model,
                "embedding": embedding.model if use_gateway else self.server.ai.embedding_model,
                "embedding_endpoint": embedding.endpoint if use_gateway else None,
                "external": external.public if external else None,
            })
        elif re.fullmatch(r"/api/source-updates/\d{1,16}", route.path):
            source = int(route.path.rsplit("/", 1)[-1])
            row = self.server.store.source(source)
            if row is None:
                self.send_json(404, {"error": "source_missing"})
            else:
                self.send_json(200, {"available": bool(self.server.store.allowed_sources(source)),
                                     "updates": self.server.indexer.pending_updates(row)})
        elif re.fullmatch(r"/api/attachment/\d{1,16}", route.path):
            self.download_attachment(int(route.path.rsplit("/", 1)[-1]))
        elif route.path == "/api/ping":
            self.server.last_seen = time.monotonic()
            self.send_json(200, {"ok": True})
        elif route.path == "/api/intranet/settings":
            self.send_json(200, self.intranet_settings())
        elif route.path == "/api/ollama/setup":
            self.send_json(200, self.server.ollama_setup.snapshot())
        elif route.path == "/api/external/settings":
            external = self.server.generic_provider()
            saved = self.server.api_profiles.get("external") or {}
            self.send_json(200, {
                "configured": external.public if external else None,
                "defaults": {"destination": saved.get("destination", DEFAULT_URL), "model": saved.get("model", DEFAULT_MODEL)},
                "persistent_keys": self.server.persistent_keys,
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

    def download_attachment(self, attachment):
        row = self.server.store.original_attachment(attachment)
        if row is None:
            self.send_json(404, {"error": "attachment_missing"})
            return
        try:
            body = self.server.indexer.attachment_bytes(row)
        except ImportIssue as exc:
            self.send_json(409, {"error": str(exc)})
        except (OSError, ValueError, LookupError, RuntimeError, TypeError):
            self.send_json(422, {"error": "attachment_missing"})
        else:
            if body is None:
                self.send_json(202, {"status": "preparing"})
            elif self.server.store.original_attachment(attachment) is None:
                self.send_json(404, {"error": "attachment_missing"})
            else:
                filename = re.split(r"[/\\]", row["name"])[-1]
                self.send_bytes(200, "application/octet-stream", body, filename=filename)

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
            elif route == "/api/intranet/settings":
                self.change_intranet_settings(payload)
            elif route == "/api/intranet/defaults":
                if payload:
                    raise ValueError("gateway_preset")
                self.use_gateway_preset()
            elif route == "/api/ollama/setup":
                self.send_json(202, self.server.ollama_setup.start(payload.get("action")))
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
            started = time.monotonic()
            mode = payload.get("mode")
            keyword_only = mode in ("keyword", "quick")
            plan = quoted_plan(query)
            quick = mode == "quick" and plan is None and not filters.get("body")
            if plan is not None:
                rows = store.exact_search(plan, filters)
            elif quick:
                rows = store.quick_search(query, filters)
            else:
                rows = store.lexical(query, filters, limit=180)
                if query and not keyword_only and ai.embedding_index:
                    try:
                        nearby = ai.semantic_search(store, query, filters)
                        rows = merge_rows(rows, nearby, limit=60)
                    except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, http.client.HTTPException):
                        pass
            if query and not keyword_only and plan is None:
                rows = ai.rerank_candidates(query, rows)
            elapsed = time.monotonic() - started
            if elapsed >= 1:
                runtime_logger.info("Search took %.2fs (mode=%s, tab=%s, scoped=%s)", elapsed,
                                    "quick" if quick else "keyword" if keyword_only or plan else "hybrid",
                                    filters.get("tab", "all"), "source" in filters)
            tokens = query_words(query, fuzzy=True)[:8]
            distinct = {}
            for row in rows:
                cell = PCF_CELL.fullmatch(row["location"]) if filters.get("tab") == "pcf" else None
                key = (row["id"], cell[1]) if cell else row["fingerprint"]
                if key not in distinct:
                    if len(distinct) == 20:
                        continue
                    distinct[key] = public_row(row)
                shown = distinct[key]
                if "similarity" in row and shown.get("similarity") is None:
                    shown["similarity"] = round(row["similarity"], 3)
                if shown.get("matched") is None:
                    haystack = normalized(f"{row['subject']} {row['text']}")
                    matched = [token for token in tokens if normalized(token) in haystack]
                    shown["matched"] = matched
                    shown["snippet"] = best_snippet(row["text"], matched)
            result = {"results": list(distinct.values())}
            if quick:
                result["mode"] = "quick"
            self.send_json(200, result)
            return
        if not query:
            self.send_json(400, {"error": "question_required"})
            return
        external = self.server.external
        if route in {"/api/external/preview", "/api/pcf/preview"} and not external:
            self.send_json(403, {"error": "external_disabled"})
            return
        pcf = route == "/api/pcf/preview"
        budget = external.internal_budget if route == "/api/external/preview" else 0
        if budget:
            filters["regular_only"] = True
        if pcf:
            filters["tab"] = "pcf"
            if self.preview_pcf_word(payload.get("passage"), query, context, filters, external):
                return
            evidence, mode = pcf_retrieval(store, ai, query, filters, context)
            evidence = [row for row in evidence if not pcf_word(row)]
        else:
            evidence, mode = evidence_for(store, ai, query, filters, context,
                                          semantic=(route in {"/api/answer", "/api/external/preview"}),
                                          limit=MAX_INTERNAL_EXCERPTS if budget else 9, expand=bool(budget))
        if route in {"/api/external/preview", "/api/pcf/preview"}:
            token, review = self.server.reviews.create(query, evidence, external.review_generation, pcf=pcf, internal_budget=budget)
            by_passage = {row["passage"]: public_row(row) for row in evidence}
            self.send_json(200, {
                **external.public, "review": token, "question": query,
                "excerpts": [{"id": item.number, "text": item.text, "context": item.context, "source": by_passage[item.passage]}
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
                original = source_path(root / row["item"] if root.is_dir() else root)
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
                or not isinstance(selected, list) or not 1 <= len(selected) <= (MAX_PCF_CELLS if pcf else MAX_INTERNAL_EXCERPTS)
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
            if review.internal_budget and review.internal_budget != external.internal_budget:
                self.send_json(409, {"error": "external_review_expired"})
                return
            if len(excerpts) > (MAX_INTERNAL_EXCERPTS if review.internal_budget else MAX_EXCERPTS):
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
            verified = [{**visible[item.passage], "text": item.text,
                         "reviewed_sender": (item.context or {}).get("sender", "")}
                        for item in excerpts if item.passage in visible]
            checked = validate(result, verified, review.question, text_for=lambda row: row["text"], pcf=pcf)
            self.send_json(200, {
                **checked, "results": [public_row(row) for row in verified], "provider": "external",
            })
        finally:
            server.outbound.release()


    def intranet_settings(self):
        local, current = self.server.ai.embedding_config
        configured = isinstance(current, IntranetAI)
        fields = self.server.gateway_fields
        models = available_models(fields["answer_models"])
        origin = fields["gateway_url"]
        defaults = {"answer": next(iter(models["answer"]), ""), "rerank": "", "embedding": BGE_MODEL if origin else ""}
        saved = self.server.api_profiles.get("gateway", {}).get("selections")
        selections = dict(current.models if configured else saved if saved else defaults)
        if not configured and not saved and self.server.embedding_selection is not None:
            selections.update(answer="", rerank="", embedding=self.server.embedding_selection)
        if local.split(":")[0] == BGE_MODEL and not (current and current.index_key):
            selections["embedding"] = LOCAL_EMBEDDING
            if not configured:
                selections["answer"] = ""
        return {
            **fields, "destination": origin + CHAT_PATH if origin else "", "models": models,
            "configured": configured, "persistent_keys": self.server.persistent_keys,
            "test_unverified": bool(origin and origin == self.server.gateway_test_origin),
            "runtime_unverified": bool(origin and origin == self.server.gateway_unverified_origin),
            "rerank_unverified": bool(origin and origin == self.server.rerank_unverified_origin),
            "selections": selections,
            "active_answer": configured and current.answer_client is not None and self.server.external is current.answer_client,
        }

    def use_gateway_preset(self):
        server = self.server
        path = server.gateway_preset
        if path is None:
            self.send_json(404, {"error": "gateway_preset_missing"})
            return
        with server.gateway_preset_lock:
            try:
                try:
                    fields = read_gateway_preset(path)
                except FileNotFoundError:
                    try:
                        fields = read_gateway_preset(path.with_suffix(".seed.json"))
                    except FileNotFoundError:
                        if not gateway_origin():
                            self.send_json(404, {"error": "gateway_preset_missing"})
                            return
                        fields = validate_gateway_preset(gateway_environment())
                    data = (json.dumps(fields, ensure_ascii=False, indent=2) + "\n").encode()
                    write_api_settings(path, data)
                server.gateway_test_origin = fields["gateway_url"] if fields["test_unverified"] else ""
            except ValueError:
                self.send_json(400, {"error": "gateway_preset_invalid"})
                return
            except OSError:
                self.send_json(500, {"error": "gateway_preset_io"})
                return
        self.send_json(200, fields)

    def change_intranet_settings(self, payload):
        action = payload.get("action")
        if action not in ("apply", "test", "disconnect"):
            raise ValueError("intranet_settings_action")
        server = self.server
        if not server.outbound.acquire(blocking=False):
            self.send_json(409, {"error": "external_busy"})
            return
        try:
            current = server.ai.embedding_api
            if not isinstance(current, IntranetAI):
                current = None
            candidate = None
            local_model = server.ai.embedding_model
            fields = server.gateway_fields
            unverified_origin = ""
            rerank_unverified_origin = ""
            selections = {"answer": "", "rerank": "", "embedding": LOCAL_EMBEDDING if local_model == BGE_MODEL else ""}
            if action != "disconnect":
                key = payload.get("key", "")
                if not isinstance(key, str):
                    raise ValueError("embedding_api_key")
                try:
                    selections = payload.get("selections")
                    fields = gateway_fields(payload, fields)
                    skip_verify = payload.get("test_unverified", False)
                    if type(skip_verify) is not bool or (skip_verify and action != "test"):
                        raise ValueError("gateway_settings")
                    runtime_unverified = payload.get("runtime_unverified", False)
                    if type(runtime_unverified) is not bool or (runtime_unverified and not fields["gateway_url"]):
                        raise ValueError("gateway_settings")
                    rerank_unverified = payload.get("rerank_unverified", False)
                    if type(rerank_unverified) is not bool or (rerank_unverified and not fields["gateway_url"]):
                        raise ValueError("gateway_settings")
                    unverified_origin = fields["gateway_url"] if runtime_unverified else ""
                    rerank_unverified_origin = fields["gateway_url"] if rerank_unverified else ""
                    tls = gateway_tls(fields["ca_file"], runtime_unverified, skip_verify,
                                      rerank_unverified=rerank_unverified)
                    candidate = IntranetAI.from_settings(
                        key.strip(), selections, current, origin=fields["gateway_url"],
                        answer_models=fields["answer_models"], tls=tls,
                    )
                    local_model = BGE_MODEL if selections["embedding"] == LOCAL_EMBEDDING else ""
                except ValueError as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
            if action == "test":
                checks = candidate.test_models() if candidate else {}
                if local_model:
                    try:
                        vectors = server.ai.embed_local(local_model, ["Hello"], 8)
                        pack_vector(vectors[0])
                        checks["embedding"] = {"ok": True}
                    except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, http.client.HTTPException) as exc:
                        known = {"ollama_not_found", "ollama_not_started", "local_model_missing", "local_model_http"}
                        reason = str(exc)
                        if reason not in known:
                            reason = "timeout" if isinstance(exc, TimeoutError) else (
                                "local_connection" if isinstance(exc, OSError) else "invalid_response")
                        checks["embedding"] = {"ok": False, "reason": reason}
                self.send_json(200, {"checks": checks})
                return
            with server.indexer.lock:
                if server.indexer.events:
                    self.send_json(409, {"error": "gateway_indexing"})
                    return
                answer = server.external
                if candidate and candidate.answer_client:
                    answer = candidate.answer_client
                elif current and answer is current.answer_client:
                    answer = server.generic_answer
                try:
                    server.remember_api("gateway", {**fields, "selections": selections, "unverified_origin": unverified_origin,
                                                   "rerank_unverified_origin": rerank_unverified_origin,
                                                   "key": candidate._key if candidate else ""}, answer)
                except (OSError, ValueError):
                    self.send_json(500, {"error": "api_settings_save_failed"})
                    return
                server.gateway_fields = fields
                server.gateway_unverified_origin = unverified_origin
                server.rerank_unverified_origin = rerank_unverified_origin
                server.ai.configure_embedding(local_model, candidate)
                server.embedding_selection = LOCAL_EMBEDDING if local_model == BGE_MODEL else ""
                server.external = answer
                server.reviews.clear()
            self.send_json(200, self.intranet_settings())
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
            current = server.generic_provider()
            if action == "disconnect":
                answer = None if server.external is current else server.external
                try:
                    server.remember_api("external", {}, answer)
                except (OSError, ValueError):
                    self.send_json(500, {"error": "api_settings_save_failed"})
                    return
                server.external = answer
                server.generic_answer = None
                server.reviews.clear()
                self.send_json(200, {"configured": None})
                return
            url, model, key = (payload.get(name, "") for name in ("destination", "model", "key"))
            if not all(isinstance(value, str) for value in (url, model, key)):
                raise ValueError("external_settings")
            try:
                external = ExternalAI.from_settings(url.strip(), model.strip(), key.strip(), current)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if action == "apply":
                try:
                    server.remember_api("external", {**external.public, "key": external._key}, external)
                except (OSError, ValueError):
                    self.send_json(500, {"error": "api_settings_save_failed"})
                    return
                server.generic_answer = server.external = external
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


@contextmanager
def file_diagnostics(path, logger):
    try:
        handler = RotatingFileHandler(path, maxBytes=256 * 1024, backupCount=1, encoding="utf-8")
    except OSError as exc:
        print(f"Cannot open {path.name} ({type(exc).__name__}).", file=sys.stderr)
        yield
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()


def gateway_diagnostics(root):
    return file_diagnostics(root / "api-connection.log", gateway_logger)


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
    embedding_key = os.environ.pop("LITELLM_API_KEY", "")
    embedding_api = None
    if embedding_key:
        try:
            models = dict.fromkeys(available_models(), "")
            if not args.embedding_model:
                models["embedding"] = BGE_MODEL
            elif args.embedding_model.split(":")[0] == BGE_MODEL:
                models["embedding"] = LOCAL_EMBEDDING
            embedding_api = IntranetAI(embedding_key, models)
        except ValueError:
            print("Embedding API disabled: check LITELLM_API_KEY and MAILSEARCH_GATEWAY_URL.", file=sys.stderr)
    del embedding_key
    try:
        root = local_path(args.data)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        ai = LocalAI(args.model, args.embedding_model, args.ollama_port, embedding_api=embedding_api)
        with SessionLock(root), gateway_diagnostics(root), file_diagnostics(root / "app-runtime.log", runtime_logger):
            runtime_logger.setLevel(logging.INFO)
            runtime_logger.info("App starting (pid=%d)", os.getpid())
            try:
                store = Store(root)
                indexer = Indexer(store, args.readpst, args.pdftotext, ai)
                preset = Path(__file__).resolve().parent.parent / "gateway-defaults.json"
                with App(args.port, store, indexer, ai, external, api_settings=ApiSettings(root), gateway_preset=preset) as app:
                    stopped = threading.Event()
                    try:
                        indexer.wake()
                        print(app.origin, flush=True)
                        if not args.no_browser:
                            webbrowser.open(app.origin)
                            threading.Thread(target=app.watch_page, args=(stopped,), daemon=True).start()
                        app.serve_forever()
                    except KeyboardInterrupt:
                        runtime_logger.info("App shutdown requested (Ctrl+C)")
                    finally:
                        stopped.set()
                        indexer.stop()
                        ai.shutdown()
            except Exception as exc:
                runtime_logger.error("App stopped with an error (type=%s, errno=%s, winerror=%s, sqlite=%s)",
                                     type(exc).__name__, getattr(exc, "errno", None), getattr(exc, "winerror", None),
                                     getattr(exc, "sqlite_errorname", None))
                raise
            else:
                runtime_logger.info("App stopped normally")
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"App failed ({type(exc).__name__}). Check app-runtime.log and the data folder, port, and SQLite 3.42+ with FTS5.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
