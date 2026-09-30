from typing import Protocol


class EmbeddingProvider(Protocol):
    """Two methods, not one: the model is asymmetric/instruction-tuned, so
    queries and documents are encoded differently. Callers pass plain text;
    applying the query instruction is the adapter's job. See
    specs/semantic-search.md §2/§3."""

    def embed_document(self, text: str) -> list[float]: ...
    def embed_query(self, text: str) -> list[float]: ...
