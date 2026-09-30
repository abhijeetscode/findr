import math


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(v * v for v in vector))


def test_embeddings_have_mapping_dimension_and_are_normalised(real_embedding_provider):
    # ~unit length (the model computes in reduced precision; cosine
    # similarity is scale-invariant anyway).
    document = real_embedding_provider.embed_document("Please review the renewal terms.")
    query = real_embedding_provider.embed_query("renewal terms")

    assert len(document) == 1024
    assert len(query) == 1024
    assert math.isclose(_norm(document), 1.0, rel_tol=1e-2)
    assert math.isclose(_norm(query), 1.0, rel_tol=1e-2)


def test_query_and_document_encodings_differ(real_embedding_provider):
    # Asymmetric model: queries get the retrieval instruction, documents
    # don't (specs/semantic-search.md §2) — same text, different vectors.
    text = "renewal terms"
    assert real_embedding_provider.embed_query(text) != real_embedding_provider.embed_document(text)


def test_empty_text_does_not_error(real_embedding_provider):
    assert len(real_embedding_provider.embed_document("")) == 1024


def test_detect_device_prefers_mps_then_cuda_then_cpu(monkeypatch):
    import torch

    from findr.adapters.outbound.embeddings.sentence_transformer_provider import detect_device

    def available(mps: bool, cuda: bool) -> str:
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: mps)
        monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)
        return detect_device()

    assert available(mps=True, cuda=True) == "mps"
    assert available(mps=False, cuda=True) == "cuda"
    assert available(mps=False, cuda=False) == "cpu"
