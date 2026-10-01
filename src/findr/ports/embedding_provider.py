from typing import Protocol


class EmbeddingProvider(Protocol):
    """Two methods, not one: the model is asymmetric/instruction-tuned, so
    queries and documents are encoded differently. Callers pass plain text;
    applying the query instruction is the adapter's job. See
    specs/semantic-search.md §2/§3."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """One batch call for many passages (e.g. every chunk of a file) —
        much faster than one call each. Returns vectors in input order."""
        ...
    def embed_query(self, text: str) -> list[float]: ...
