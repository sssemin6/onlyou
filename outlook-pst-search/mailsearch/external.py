"""Optional HTTPS generation with explicit review of each outbound request."""
import http.client
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .answer import ANSWER_INSTRUCTIONS, DESIGN_INSTRUCTIONS, MODEL_NAME, map_citations

DEFAULT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4.1-mini"
REVIEW_TTL = 300
MAX_EXCERPTS = 5
MAX_EXCERPT_LENGTH = 800
MAX_RESPONSE = 128 * 1024
HOST = re.compile(r"(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+", re.ASCII)
PATH = re.compile(r"/(?:[A-Za-z0-9._~-]+/)*(?:chat/completions|messages)", re.ASCII)
JSON_INSTRUCTIONS = """
입력 JSON에는 question과 evidence가 있다. 메일 발신자·제목·발송 시간은 별도로 주어지지 않는다.
응답은 아래 구조의 JSON 객체 하나여야 한다.
{"status":"answered","claims":[{"text":"메일 근거를 확인한 한국어 문장","citations":[{"source":1,"quote":"자료에서 연속된 실제 인용"}]}]}
각 문장에 인용 1~3개를 넣고 quote는 4~300자 사이로 유지한다. source는 evidence 항목의 source 번호다.
답을 확인할 수 없으면 status를 insufficient 또는 ambiguous로 하고 claims를 빈 목록으로 설정한다.
"""


class ExternalRequestError(RuntimeError):
    def __init__(self, status):
        super().__init__("external_api_http")
        self.status = status


class ExternalAI:
    @classmethod
    def from_environment(cls):
        model = os.environ.get("MAILSEARCH_EXTERNAL_MODEL", "").strip()
        key = os.environ.get("MAILSEARCH_EXTERNAL_API_KEY", "")
        if not model and not key and not os.environ.get("MAILSEARCH_EXTERNAL_URL"):
            return None
        url = os.environ.get("MAILSEARCH_EXTERNAL_URL") or DEFAULT_URL
        return cls(url, model, key)

    def __init__(self, url, model, key):
        if not isinstance(url, str) or not 1 <= len(url) <= 512 or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in url):
            raise ValueError("external_url")
        try:
            endpoint = urlsplit(url)
            port = endpoint.port
        except ValueError as exc:
            raise ValueError("external_url") from exc
        if (endpoint.scheme != "https" or not endpoint.hostname or not HOST.fullmatch(endpoint.hostname)
                or "@" in endpoint.netloc or "?" in url or "#" in url or not PATH.fullmatch(endpoint.path)):
            raise ValueError("external_url")
        if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
            raise ValueError("external_model")
        if not isinstance(key, str) or not re.fullmatch(r"[\x21-\x7e]{1,2048}", key):
            raise ValueError("external_key")
        if port == 0 or any(part in {".", ".."} for part in endpoint.path.split("/")):
            raise ValueError("external_url")
        self.host = endpoint.hostname
        self.port = port if port is not None else 443
        self.path = endpoint.path
        self.anthropic = endpoint.path.endswith("/messages")
        self.model = model
        self._key = key
        self.review_generation = object()

    @classmethod
    def from_settings(cls, url, model, key, current):
        if key:
            return cls(url, model, key)
        if current is None:
            raise ValueError("external_key_required")
        candidate = cls(url, model, current._key)
        if (candidate.host, candidate.port, candidate.path) != (current.host, current.port, current.path):
            raise ValueError("external_key_required")
        return candidate

    @property
    def public(self):
        destination = self.host if self.port == 443 else f"{self.host}:{self.port}"
        return {"destination": f"https://{destination}{self.path}", "model": self.model}

    def answer(self, question, excerpts, instructions=ANSWER_INSTRUCTIONS, token_limit=None, timeout=35):
        messages = {"question": question, "evidence": [
            {"source": item.number, "text": item.text} for item in excerpts
        ]}
        result = self.complete(instructions + JSON_INSTRUCTIONS, json.dumps(messages, ensure_ascii=False),
                               token_limit=token_limit or (2800 if self.host == "api.openai.com" else 1500),
                               timeout=timeout)
        return map_citations(result, {item.number: item.passage for item in excerpts})

    def test(self):
        reply = self.complete('Reply with the JSON object {"ok": true}.', "Connection test", token_limit=600, timeout=15)
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise ValueError("external_test_reply")

    def complete(self, instructions, content, *, token_limit, timeout):
        if self.anthropic:
            # Anthropic Messages protocol: system is a top-level field, max_tokens is
            # required, and response_format is not supported. The instructions already
            # request a single JSON object.
            options = {
                "model": self.model,
                "max_tokens": token_limit,
                "system": instructions,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
            }
            headers = {
                "Authorization": f"Bearer {self._key}",
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        else:
            options = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            if self.host == "api.openai.com":
                options.update(max_completion_tokens=token_limit, store=False)
            else:
                options["max_tokens"] = token_limit
            headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        # HTTPSConnection uses normal certificate verification and follows neither redirects nor proxies.
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request(
                "POST", self.path, json.dumps(options, ensure_ascii=False).encode("utf-8"), headers,
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ExternalRequestError(response.status)
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("external_response_size")
            payload = json.loads(raw)
            if self.anthropic:
                text = "".join(block.get("text", "") for block in payload["content"] if isinstance(block, dict))
            else:
                text = payload["choices"][0]["message"]["content"]
            return json.loads(text)
        finally:
            connection.close()


@dataclass(frozen=True)
class Excerpt:
    number: int
    passage: int
    text: str


@dataclass(frozen=True)
class Review:
    expires: float
    question: str
    excerpts: tuple[Excerpt, ...]
    generation: object | None


class Reviews:
    def __init__(self):
        self._pending = {}
        self._lock = threading.Lock()

    def create(self, question, rows, generation=None, limit=MAX_EXCERPT_LENGTH):
        texts = {}
        for row in rows:
            # Keep path, mail metadata, filename and account identifiers out of the request envelope.
            if row["location"] in {"파일명", "제목"}:
                continue
            text = row["text"].strip()[:limit]
            if text and text != "본문 없음":
                texts.setdefault(text, row["passage"])
            if len(texts) == MAX_EXCERPTS:
                break
        excerpts = tuple(Excerpt(n, passage, text) for n, (text, passage) in enumerate(texts.items(), 1))
        if not excerpts:
            return None, None
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        review = Review(now + REVIEW_TTL, question, excerpts, generation)
        with self._lock:
            self._pending = {key: value for key, value in self._pending.items() if value.expires > now}
            if len(self._pending) >= 8:
                del self._pending[next(iter(self._pending))]
            self._pending[token] = review
        return token, review

    def take(self, token):
        with self._lock:
            review = self._pending.pop(token, None)
        return review if review and review.expires > time.monotonic() else None

    def clear(self):
        with self._lock:
            self._pending.clear()
