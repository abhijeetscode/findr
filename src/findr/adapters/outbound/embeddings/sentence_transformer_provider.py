from __future__ import annotations

import logging
import time

from findr.adapters.outbound.elasticsearch.es_client import EMBEDDING_DIMS
from findr.observability import log_event
from findr.observability.setup import reclaim_console_handlers
from findr.observability.events import elapsed_ms

logger = logging.getLogger(__name__)


def detect_device() -> str:
    """Best available torch device: Apple-silicon GPU (MPS), then NVIDIA GPU
    (CUDA), then CPU. The Docker image ships CPU-only torch, so it always
    lands on "cpu" there; a Mac dev machine gets "mps"."""
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class SentenceTransformerEmbeddingProvider:
    """Implements ports.embedding_provider.EmbeddingProvider with a local,
    in-process sentence-transformers model (Qwen3-Embedding-0.6B by default).
    See specs/semantic-search.md §3.

    Loading takes seconds and ~2GB+ of RAM, so construct one per process
    (app startup, the reindex script) and share it — never per request.
    """

    def __init__(self, model_name: str, max_seq_length: int) -> None:
        # Imported here so modules that only need the type (and tests using a
        # fake) don't pay torch's import cost.
        from sentence_transformers import SentenceTransformer

        # transformers/huggingface_hub attach console handlers when first
        # imported; send them to the log file before the model load logs
        # anything (specs/logging-telemetry.md §4.7).
        reclaim_console_handlers()

        device = detect_device()
        start = time.perf_counter()
        self._model = SentenceTransformer(model_name, device=device)
        log_event(
            logger,
            "embedding.model.loaded",
            f"Loaded embedding model {model_name} on {device}",
            model=model_name,
            device=device,
            duration_ms=elapsed_ms(start),
        )
        # Qwen3's native context is ~32k tokens; embedding that every sync is
        # far too slow, especially on CPU. Longer text is truncated (no chunking yet —
        # specs/semantic-search.md §1/§6).
        self._model.max_seq_length = max_seq_length

        dims = self._model.get_embedding_dimension()
        if dims != EMBEDDING_DIMS:
            raise ValueError(
                f"Embedding model {model_name!r} produces {dims}-dim vectors but the "
                f"index mapping expects {EMBEDDING_DIMS}"
            )
        if "query" not in self._model.prompts:
            raise ValueError(f"Embedding model {model_name!r} has no 'query' prompt configured")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # One batched encode: much faster than per-text calls for the many
        # chunks of one file (specs/upload-chunking.md §4.3).
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        # prompt_name="query" applies the model's bundled retrieval
        # instruction; documents are encoded plain (asymmetric model).
        return self._model.encode(text, prompt_name="query", normalize_embeddings=True).tolist()


def create_embedding_provider(settings) -> SentenceTransformerEmbeddingProvider:
    """One per process — shared by the API, the upload worker and the
    reindex script."""
    return SentenceTransformerEmbeddingProvider(
        settings.embedding_model, settings.embedding_max_seq_length
    )
