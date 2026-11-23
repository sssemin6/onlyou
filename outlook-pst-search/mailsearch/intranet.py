"""Configured gateway text model roles and reranking calls."""
import http.client
import os
import socket
import ssl

from .answer import MODEL_NAME
from .embedding import EmbeddingAPI, GatewayRequestError, MODEL, gateway_origin
from .external import ExternalAI, ExternalRequestError

LOCAL_EMBEDDING = "ollama:" + MODEL
CHAT_PATH = "/v1/chat/completions"
MODELS = {
    "embedding": {LOCAL_EMBEDDING: "로컬 Ollama · BGE-M3", MODEL: "사내 API · BGE-M3"},
    "rerank": {"bge-reranker-v2-m3": "BGE-Reranker-v2-M3"},
}


def available_models(answer_models=None):
    names = (os.environ.get("MAILSEARCH_GATEWAY_ANSWER_MODELS", "") if answer_models is None else answer_models).split(",")
    answers = {name: name for value in names if MODEL_NAME.fullmatch(name := value.strip())}
    return {"answer": answers, **MODELS}


def validate_selections(selections, answer_models=None):
    models = available_models(answer_models)
    if not isinstance(selections, dict) or set(selections) != set(models):
        raise ValueError("gateway_models")
    for role, options in models.items():
        model = selections[role]
        if not isinstance(model, str) or (model and model not in options):
            raise ValueError("gateway_models")


class IntranetAI(EmbeddingAPI):
    def __init__(self, key, selections, *, origin=None, answer_models=None, tls=None):
        super().__init__(key, origin=origin, tls=tls)
        validate_selections(selections, answer_models)
        self.models = dict(selections)
        self.model = MODEL if selections["embedding"] == MODEL else ""
        self.answer_client = None
        if selections["answer"]:
            self.answer_client = ExternalAI(self.origin + CHAT_PATH, selections["answer"], key,
                                            intranet=True, gateway=self.origin, tls=tls)

    @classmethod
    def from_settings(cls, key, selections, current, *, origin=None, answer_models=None, tls=None):
        validate_selections(selections, answer_models)
        if not key and current and current.origin == gateway_origin(origin):
            key = current._key
        if not key:
            if selections["answer"] or selections["rerank"] or selections["embedding"] == MODEL:
                raise ValueError("external_key_required")
            return None
        return cls(key, selections, origin=origin, answer_models=answer_models, tls=tls)

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
            except (GatewayRequestError, ExternalRequestError) as exc:
                checks[role] = {"ok": False, "http_status": exc.status}
            except ssl.SSLCertVerificationError:
                checks[role] = {"ok": False, "reason": "tls_certificate"}
            except FileNotFoundError:
                checks[role] = {"ok": False, "reason": "missing_ca"}
            except ssl.SSLError:
                checks[role] = {"ok": False, "reason": "tls_error"}
            except socket.gaierror:
                checks[role] = {"ok": False, "reason": "dns"}
            except TimeoutError:
                checks[role] = {"ok": False, "reason": "timeout"}
            except OSError:
                checks[role] = {"ok": False, "reason": "connection"}
            except (ValueError, KeyError, IndexError, TypeError, http.client.HTTPException):
                checks[role] = {"ok": False, "reason": "invalid_response"}
        return checks
