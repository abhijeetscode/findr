from __future__ import annotations

from findr.adapters.outbound.elasticsearch.es_client import EMBEDDING_DIMS


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

        self._model = SentenceTransformer(model_name, device="cpu")
        # Qwen3's native context is ~32k tokens; embedding that on CPU every
        # sync is far too slow. Longer text is truncated (no chunking yet —
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

    def embed_document(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        # prompt_name="query" applies the model's bundled retrieval
        # instruction; documents are encoded plain (asymmetric model).
        return self._model.encode(text, prompt_name="query", normalize_embeddings=True).tolist()
