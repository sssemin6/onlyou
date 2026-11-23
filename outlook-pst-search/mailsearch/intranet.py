"""HD AIC text model roles and reranking calls."""
import http.client

from .embedding import EmbeddingAPI, HDRequestError, HOST, MODEL
from .external import ExternalAI, ExternalRequestError

LOCAL_EMBEDDING = "ollama:" + MODEL
CHAT_URL = f"https://{HOST}/v1/chat/completions"
MODELS = {
    "answer": {"qwen3-8-27b": "Qwen3.8-27B", "deepseek-v4-1-flash-1017": "DeepSeek-V4.1-Flash-1017"},
    "embedding": {LOCAL_EMBEDDING: "로컬 Ollama · BGE-M3", MODEL: "사내 HD AIC API · BGE-M3"},
    "rerank": {"bge-reranker-v2-m3": "BGE-Reranker-v2-M3"},
}
DEFAULTS = {role: "" for role in MODELS}
DEFAULTS.update(answer="qwen3-8-27b", embedding=MODEL)


def validate_selections(selections):
    if not isinstance(selections, dict) or set(selections) != set(MODELS):
        raise ValueError("hd_models")
    for role, options in MODELS.items():
        model = selections[role]
        if not isinstance(model, str) or (model and model not in options):
            raise ValueError("hd_models")


class IntranetAI(EmbeddingAPI):
    def __init__(self, key, selections):
        super().__init__(key)
        validate_selections(selections)
        self.models = dict(selections)
        self.model = MODEL if selections["embedding"] == MODEL else ""
        self.answer_client = ExternalAI(CHAT_URL, selections["answer"], key, intranet=True) if selections["answer"] else None

    @classmethod
    def from_settings(cls, key, selections, current):
        validate_selections(selections)
        if not key and current is None:
            if selections["answer"] or selections["rerank"] or selections["embedding"] == MODEL:
                raise ValueError("external_key_required")
            return None
        return cls(key or current._key, selections)

    def rerank(self, query, documents, timeout=6):
        data = self.request("/v1/rerank", {
            "model": self.models["rerank"], "query": query, "documents": documents, "top_n": len(documents),
        }, timeout)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != len(documents):
            raise ValueError("rerank_response")
        scores = {}
        for result in results:
            index = result.get("index") if isinstance(result, dict) else None
            if type(index) is not int or not 0 <= index < len(documents) or index in scores:
                raise ValueError("rerank_index")
            score = result.get("relevance_score")
            if type(score) not in (int, float) or not -1e20 <= score <= 1e20:
                raise ValueError("rerank_score")
            scores[index] = score
        return sorted(scores, key=lambda index: (-scores[index], index))

    def test_models(self):
        probes = {
            "answer": lambda: self.answer_client.test(),
            "embedding": lambda: self.embed(["Hello"], 8),
            "rerank": lambda: self.rerank("Hello", ["Hello, world", "Goodbye"]),
        }
        checks = {}
        for role, model in self.models.items():
            if not model or role == "embedding" and not self.index_key:
                continue
            try:
                probes[role]()
                checks[role] = {"ok": True}
            except (HDRequestError, ExternalRequestError) as exc:
                checks[role] = {"ok": False, "http_status": exc.status}
            except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException):
                checks[role] = {"ok": False}
        return checks
