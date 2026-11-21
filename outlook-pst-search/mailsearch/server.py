"""Loopback-only web application."""
import argparse
import datetime
import hmac
import http.client
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .answer import LocalAI, delivery_date_answer, embedding_target, evidence_for, merge_rows, public_row, validate
from .external import DEFAULT_MODEL, DEFAULT_URL, MAX_EXCERPTS, ExternalAI, ExternalRequestError, Reviews
from .filesystem import local_path
from .indexer import ImportIssue, Indexer
from .store import Store, quoted_plan

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
            ids = set(self.server.store.allowed_sources())
            external = self.server.external
            self.send_json(200, {
                "sources": [{**row, "available": row["id"] in ids} for row in sources],
                "model": self.server.ai.model, "embedding": self.server.ai.embedding_model,
                "external": external.public if external else None,
            })
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
                self.send_json(200, {**public_row(row), "text": row["text"], "body": row["body"],
                                     "start": row["start"], "attachments": row["attachments"]})
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
            elif route in {"/api/search", "/api/candidates", "/api/answer", "/api/external/preview"}:
                self.query(route, payload)
            elif route == "/api/external/send":
                self.send_external(payload)
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
            distinct = {}
            for row in rows:
                distinct.setdefault(row["fingerprint"], public_row(row))
            self.send_json(200, {"results": list(distinct.values())[:20]})
            return
        if not query:
            self.send_json(400, {"error": "question_required"})
            return
        external = self.server.external
        if route == "/api/external/preview" and not external:
            self.send_json(403, {"error": "external_disabled"})
            return
        evidence, mode = evidence_for(store, ai, query, filters, context,
                                      semantic=(route in {"/api/answer", "/api/external/preview"}))
        if route == "/api/external/preview":
            token, review = self.server.reviews.create(query, evidence, external.review_generation)
            self.send_json(200, {
                **external.public, "review": token, "question": query,
                "excerpts": [{"id": item.number, "text": item.text} for item in review.excerpts] if review else [],
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

    def send_external(self, payload):
        server = self.server
        if not server.external:
            self.send_json(403, {"error": "external_disabled"})
            return
        token, selected = payload.get("review"), payload.get("excerpts")
        if (not isinstance(token, str) or not re.fullmatch(r"[\w-]{32}", token, re.ASCII)
                or not isinstance(selected, list) or not 1 <= len(selected) <= MAX_EXCERPTS
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
            if review is None or review.generation is not external.review_generation:
                self.send_json(409, {"error": "external_review_expired"})
                return
            excerpts = [item for item in review.excerpts if item.number in selected]
            if len(excerpts) != len(selected):
                raise ValueError("external_excerpts")
            ids = [item.passage for item in excerpts]
            rows = server.store.passages(ids)
            if len(rows) != len(ids):
                self.send_json(409, {"error": "external_source_changed"})
                return
            try:
                result = external.answer(review.question, excerpts)
            except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, http.client.HTTPException):
                self.send_json(200, {
                    "status": "external_unavailable", "claims": [], "results": [], "provider": "external",
                })
                return
            visible = {row["passage"]: row for row in server.store.passages(ids)}
            verified = [{**visible[item.passage], "text": item.text} for item in excerpts if item.passage in visible]
            checked = validate(result, verified, review.question, text_for=lambda row: row["text"])
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
    parser.add_argument("--embedding-url", default="",
                        help="shared Ollama embedding server, e.g. http://192.168.0.10:11434")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.ollama_port < 65536 or not 0 <= args.port < 65536 or args.port == args.ollama_port:
        parser.error("Invalid port configuration")
    if args.embedding_url:
        try:
            embedding_target(args.embedding_url)
        except ValueError:
            parser.error("--embedding-url must look like http://nas-ip:11434")
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
        ai = LocalAI(args.model, args.embedding_model, args.ollama_port, embedding_url=args.embedding_url)
        with SessionLock(root):
            store = Store(root)
            indexer = Indexer(store, args.readpst, args.pdftotext, ai)
            with App(args.port, store, indexer, ai, external) as app:
                indexer.wake()
                print(app.origin, flush=True)
                if not args.no_browser:
                    webbrowser.open(app.origin)
                try:
                    app.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    indexer.stop()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"Startup failed ({type(exc).__name__}). Check the data folder, port, and SQLite 3.42+ with FTS5.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
