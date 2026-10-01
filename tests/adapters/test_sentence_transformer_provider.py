import math


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(v * v for v in vector))


def test_embeddings_have_mapping_dimension_and_are_normalised(real_embedding_provider):
    # ~unit length (the model computes in reduced precision; cosine
    # similarity is scale-invariant anyway).
    document = real_embedding_provider.embed_documents(["Please review the renewal terms."])[0]
    query = real_embedding_provider.embed_query("renewal terms")

    assert len(document) == 1024
    assert len(query) == 1024
    assert math.isclose(_norm(document), 1.0, rel_tol=1e-2)
    assert math.isclose(_norm(query), 1.0, rel_tol=1e-2)


def test_query_and_document_encodings_differ(real_embedding_provider):
    # Asymmetric model: queries get the retrieval instruction, documents
    # don't (specs/semantic-search.md §2) — same text, different vectors.
    text = "renewal terms"
    assert real_embedding_provider.embed_query(text) != real_embedding_provider.embed_documents([text])[0]


def test_empty_text_does_not_error(real_embedding_provider):
    assert len(real_embedding_provider.embed_documents([""])[0]) == 1024


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


def test_embed_documents_batches_in_input_order(real_embedding_provider):
    texts = ["renewal terms for the contract", "team hiking photos"]
    batch = real_embedding_provider.embed_documents(texts)
    one_by_one = [real_embedding_provider.embed_documents([t])[0] for t in texts]

    assert len(batch) == 2
    assert real_embedding_provider.embed_documents([]) == []
    for batched, single in zip(batch, one_by_one):
        # Batching pads inputs, so values differ in the last few digits only.
        assert sum(a * b for a, b in zip(batched, single)) > 0.99
