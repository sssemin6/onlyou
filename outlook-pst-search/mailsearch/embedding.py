"""Client for the configured HD AIC embedding service."""
import http.client
import json
import re

HOST = "api.model.hd-aic.com"
PATH = "/v1/embeddings"
ENDPOINT = f"https://{HOST}{PATH}"
MODEL = "bge-m3"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class HDRequestError(RuntimeError):
    def __init__(self, status):
        super().__init__("hd_request_failed")
        self.status = status


class EmbeddingAPI:
    model = MODEL

    def __init__(self, key):
        if not re.fullmatch(r"[!-~]{1,2048}", key):
            raise ValueError("embedding_api_key")
        self._key = key

    @property
    def index_key(self):
        return f"{ENDPOINT}#{self.model}" if self.model else ""

    def request(self, path, payload, timeout):
        # Use the fixed HTTPS host with normal certificate verification and no redirects.
        connection = http.client.HTTPSConnection(HOST, timeout=timeout)
        try:
            connection.request("POST", path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), {
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise HDRequestError(response.status)
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("hd_response_size")
            return json.loads(body)
        finally:
            connection.close()

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
