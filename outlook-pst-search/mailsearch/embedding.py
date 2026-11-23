"""Client for the configured HD AIC embedding service."""
import http.client
import json
import math
import re

HOST = "api.model.hd-aic.com"
PATH = "/v1/embeddings"
ENDPOINT = f"https://{HOST}{PATH}"
MODEL = "bge-m3"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class EmbeddingAPI:
    def __init__(self, key):
        if not re.fullmatch(r"[!-~]{1,2048}", key):
            raise ValueError("embedding_api_key")
        self._key = key

    def embed(self, texts, timeout):
        # The fixed HTTPS endpoint verifies certificates and accepts no redirects.
        payload = json.dumps({"model": MODEL, "input": texts[0] if len(texts) == 1 else texts},
                             ensure_ascii=False).encode("utf-8")
        connection = http.client.HTTPSConnection(HOST, timeout=timeout)
        try:
            connection.request("POST", PATH, payload, {
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("embedding_api_http")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("embedding_response_size")
            data = json.loads(body)
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
                        or any(type(value) not in (float, int) or not math.isfinite(value) for value in vector)):
                    raise ValueError("embedding_vector")
                vectors[index] = vector
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("embedding_dimensions")
            return vectors
        except OverflowError as exc:
            raise ValueError("embedding_vector") from exc
        finally:
            connection.close()
