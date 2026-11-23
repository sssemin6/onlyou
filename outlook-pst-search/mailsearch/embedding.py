"""Configured gateway HTTPS transport and embedding response handling."""
import http.client
import json
import logging
import os
import re
import ssl
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from urllib.parse import urlsplit

PATH = "/v1/embeddings"
MODEL = "bge-m3"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
VERIFY_ENV = "MAILSEARCH_GATEWAY_TLS_VERIFY"
CA_FILE_ENV = "MAILSEARCH_GATEWAY_CA_FILE"
logger = logging.getLogger("mailsearch.gateway")


class GatewayRequestError(RuntimeError):
    def __init__(self, status):
        super().__init__("gateway_request_failed")
        self.status = status


def gateway_origin(raw=None):
    if raw is None:
        raw = os.environ.get("MAILSEARCH_GATEWAY_URL", "")
    raw = raw.strip()
    if not raw or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in raw):
        return ""
    try:
        endpoint = urlsplit(raw)
        port = endpoint.port
    except ValueError:
        return ""
    host = endpoint.hostname
    if (endpoint.scheme != "https" or not host or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
            or "@" in endpoint.netloc or "?" in raw or "#" in raw
            or endpoint.path not in {"", "/", "/v1", "/v1/"} or port == 0):
        return ""
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


@dataclass(frozen=True)
class GatewayTLSOptions:
    ca_file: str = ""
    verify: bool = True


@lru_cache(maxsize=4)
def tls_context(verify, ca_file):
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


@lru_cache(maxsize=8)
def warn_unverified(destination):
    logger.warning("TLS certificate verification disabled for %s by %s=0 (test only)", destination, VERIFY_ENV)


def request_gateway_json(origin, path, payload, headers, *, timeout, max_bytes, tls=None):
    """Send to the configured gateway and log transport errors without request or response data."""
    endpoint = urlsplit(origin)
    destination = endpoint.netloc + path
    verify = os.environ.get(VERIFY_ENV, "1").strip() if tls is None else ("1" if tls.verify else "0")
    ca_file = os.environ.get(CA_FILE_ENV, "").strip() if tls is None else tls.ca_file
    if verify not in {"0", "1"} or (verify == "0" and ca_file):
        logger.error("TLS configuration error: set %s to 0 or 1 and use %s only with verification enabled",
                     VERIFY_ENV, CA_FILE_ENV)
        raise ValueError("gateway_tls_configuration")
    try:
        context = tls_context(verify == "1", ca_file)
    except OSError as exc:
        logger.error("TLS CA configuration error (%s): %s", CA_FILE_ENV, exc.strerror or type(exc).__name__)
        raise

    if verify == "0":
        warn_unverified(endpoint.netloc)
    connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port or 443, timeout=timeout, context=context)
    try:
        connection.request("POST", path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers)
        response = connection.getresponse()
        if response.status != 200:
            try:
                reason = HTTPStatus(response.status).phrase
            except ValueError:
                reason = "Unknown status"
            logger.error("POST %s: HTTP %d %s", destination, response.status, reason)
            raise GatewayRequestError(response.status)
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("gateway_response_size")
        return json.loads(body)
    except ssl.SSLCertVerificationError as exc:
        logger.error("POST %s: SSLCertVerificationError (code=%s) %s",
                     destination, exc.verify_code, exc.verify_message)
        raise
    except ssl.SSLError as exc:
        logger.error("POST %s: %s (library=%s, reason=%s)",
                     destination, type(exc).__name__, exc.library, exc.reason)
        raise
    except (OSError, http.client.HTTPException) as exc:
        logger.error("POST %s: %s (errno=%s)", destination, type(exc).__name__, getattr(exc, "errno", None))
        raise
    finally:
        connection.close()


class EmbeddingAPI:
    model = MODEL

    def __init__(self, key, *, origin=None, tls=None):
        if not re.fullmatch(r"[!-~]{1,2048}", key):
            raise ValueError("embedding_api_key")
        self.origin = gateway_origin(origin)
        self.tls = tls
        if not self.origin:
            raise ValueError("gateway_url")
        self._key = key

    @property
    def endpoint(self):
        return self.origin + PATH

    @property
    def index_key(self):
        return f"{self.endpoint}#{self.model}" if self.model else ""

    def request(self, path, payload, timeout):
        return request_gateway_json(self.origin, path, payload, {
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
        }, timeout=timeout, max_bytes=MAX_RESPONSE_BYTES, tls=self.tls)

    def embed(self, texts, timeout):
        data = self.request(PATH, {"model": self.model, "input": texts[0] if len(texts) == 1 else texts}, timeout)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            raise ValueError("embedding_response")
        vectors = [None] * len(texts)
        for item in items:
            index = item.get("index") if isinstance(item, dict) else None
            if type(index) is not int or not 0 <= index < len(vectors) or vectors[index] is not None:
                raise ValueError("embedding_index")
            vector = item.get("embedding")
            if (not isinstance(vector, list) or not 1 <= len(vector) <= 4096
                    or any(type(value) not in (float, int) or not -1e20 <= value <= 1e20 for value in vector)):
                raise ValueError("embedding_vector")
            vectors[index] = vector
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError("embedding_dimensions")
        return vectors
