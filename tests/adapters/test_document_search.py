from datetime import datetime

import uuid

from findr.adapters.outbound.elasticsearch.es_client import INDEX_MAPPING, ensure_index
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import (
    ElasticsearchIndex,
    _reciprocal_rank_fusion,
)
from findr.application.search.search_documents import SearchDocuments
from findr.domain.entities import Document
from findr.domain.value_objects import SourceType

# Every document in these tests lives in this workspace unless a test says
# otherwise; searches look inside it.
WS = 7


def _make_document(
    *, doc_id, user_id, connection_id, external_id, subject, body_text, thread_id=None, chunks=()
) -> Document:
    return Document(
        id=doc_id,
        user_id=user_id,
        connection_id=connection_id,
        external_id=external_id,
        subject=subject,
        sender="someone@example.com",
        recipients=None,
        body_text=body_text,
        sent_at=datetime(2024, 1, 1),
        thread_id=thread_id,
        chunks=list(chunks),
    )


def _upload(doc_id, chunk_texts, *, user_id=1, subject="notes.txt", body_text=None, chunk_factory):
    """An uploaded-file document with the given chunks."""
    chunks = [chunk_factory(text, i) for i, text in enumerate(chunk_texts)]
    return _make_document(
        doc_id=doc_id,
        user_id=user_id,
        connection_id=9,
        external_id=f"upload-{doc_id}",
        subject=subject,
        body_text=body_text if body_text is not None else "\n\n".join(chunk_texts),
        chunks=chunks,
    )


def test_search_is_isolated_per_user(es_client, es_index, fake_embedding_provider):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-1",
                subject="Renewal terms",
                body_text="Please review the renewal terms.",
            ),
            _make_document(
                doc_id=2,
                user_id=2,
                connection_id=2,
                external_id="msg-1",
                subject="Renewal terms",
                body_text="Please review the renewal terms.",
            ),
        ],
        SourceType.GMAIL,
        "a@gmail.com",
        WS,
    )

    use_case = SearchDocuments(search_index)
    a_hits = use_case.execute(1, WS, "renewal")
    b_hits = use_case.execute(2, WS, "renewal")

    assert len(a_hits) == 1
    assert a_hits[0].document.user_id == 1
    assert a_hits[0].source_type == SourceType.GMAIL
    assert len(b_hits) == 1
    assert b_hits[0].document.user_id == 2


def test_upsert_dedups_on_document_id(es_client, es_index, fake_embedding_provider):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-1",
                subject="Old subject",
                body_text="old body",
            )
        ],
        SourceType.GMAIL,
        "a@gmail.com",
        WS,
    )
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-1",
                subject="New subject",
                body_text="new body",
            )
        ],
        SourceType.GMAIL,
        "a@gmail.com",
        WS,
    )

    search = SearchDocuments(search_index)
    new_hits = search.execute(1, WS, "new")
    assert len(new_hits) == 1
    assert new_hits[0].document.subject == "New subject"

    assert search.execute(1, WS, "old") == []


def test_delete_documents_removes_from_search(es_client, es_index, fake_embedding_provider):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-1",
                subject="Trashed",
                body_text="this will be deleted",
            )
        ],
        SourceType.GMAIL,
        "a@gmail.com",
        WS,
    )

    search = SearchDocuments(search_index)
    assert len(search.execute(1, WS, "trashed")) == 1

    search_index.delete_documents(connection_id=1, external_ids=["msg-1"])

    assert search.execute(1, WS, "trashed") == []


def test_thread_id_round_trips_through_index_and_search(es_client, es_index, fake_embedding_provider):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-1",
                subject="Q3 renewal terms",
                body_text="please review the renewal terms",
                thread_id="thread-abc",
            ),
            _make_document(
                doc_id=2,
                user_id=1,
                connection_id=1,
                external_id="msg-2",
                subject="Re: approved",
                body_text="sounds good, approved",
                thread_id="thread-abc",
            ),
        ],
        SourceType.GMAIL,
        "a@gmail.com",
        WS,
    )

    search = SearchDocuments(search_index)
    hits = search.execute(1, WS, "renewal")

    assert len(hits) == 1
    assert hits[0].document.thread_id == "thread-abc"


def test_search_highlights_matches_with_bracket_markers(es_client, es_index, fake_embedding_provider):
    # The frontend's renderSnippet() JS turns "[" / "]" into <strong> tags —
    # this pins ES's highlight config to those exact markers (see
    # specs/elasticsearch-search.md §5), not its default <em> tags.
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-1",
                subject="Q3 renewal terms",
                body_text="please review the renewal terms",
            )
        ],
        SourceType.GMAIL,
        "a@gmail.com",
        WS,
    )

    hits = SearchDocuments(search_index).execute(1, WS, "renewal")

    assert len(hits) == 1
    assert "[renewal]" in hits[0].snippet.lower()


def test_query_with_special_characters_does_not_error(es_client, es_index, fake_embedding_provider):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-1",
                subject="Q3 renewal",
                body_text="terms: 24 months",
            )
        ],
        SourceType.GMAIL,
        "a@gmail.com",
        WS,
    )

    search = SearchDocuments(search_index)
    hits = search.execute(1, WS, 'terms: "24" -months*')

    assert isinstance(hits, list)  # must not raise a query-syntax error


def test_only_uploads_get_chunk_vectors(es_client, es_index, fake_embedding_provider, chunk_factory):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    email = _make_document(
        doc_id=1, user_id=1, connection_id=1, external_id="msg-1",
        subject="Renewal", body_text="renewal terms",
        # Even if a Gmail document somehow carried chunks, it isn't embedded.
        chunks=[chunk_factory("renewal terms")],
    )
    search_index.index_documents([email], SourceType.GMAIL, "a@gmail.com", WS)
    assert fake_embedding_provider.document_batches == []
    assert "chunks" not in es_client.get(index=es_index, id="1")["_source"]

    upload = _upload(2, ["first chunk", "second chunk"], chunk_factory=chunk_factory)
    search_index.index_documents([upload], SourceType.FILE, None, WS)

    # One batch call per document, not one per chunk.
    assert fake_embedding_provider.document_batches == [["first chunk", "second chunk"]]
    stored = es_client.get(index=es_index, id="2")["_source"]["chunks"]
    assert [c["text"] for c in stored] == ["first chunk", "second chunk"]
    assert all(len(c["embedding"]) == 1024 for c in stored)
    assert stored[1]["metadata"]["chunk_index"] == 1
    assert stored[0]["metadata"]["document_version"] == 1
    assert stored[0]["metadata"]["parser_version"] == "unstructured-test/chunking-v1"


def test_semantic_match_on_a_chunk_uses_it_as_the_snippet(
    es_client, es_index, fake_embedding_provider, chunk_factory
):
    # The fake embedder is a bag of words: "zebra migration" matches the
    # second chunk exactly, but keyword search can't see it because this
    # document's body_text deliberately doesn't contain those words.
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    upload = _upload(
        1, ["opening paragraph about budgets", "zebra migration"],
        body_text="opening paragraph about budgets", chunk_factory=chunk_factory,
    )
    search_index.index_documents([upload], SourceType.FILE, None, WS)

    hits = SearchDocuments(search_index).execute(1, WS, "zebra migration")

    assert [h.document.external_id for h in hits] == ["upload-1"]
    assert hits[0].snippet == "zebra migration"
    assert hits[0].source_type == SourceType.FILE
    # Vectors never come back in results.
    assert hits[0].document.chunks == []


def test_semantic_search_is_isolated_per_user_and_ignores_non_upload_vectors(
    es_client, es_index, fake_embedding_provider
):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    # Another user's upload.
    es_client.index(
        index=es_index, id="1", refresh=True,
        document={
            "user_id": 2, "workspace_id": WS, "connection_id": 2, "external_id": "u-1", "source_type": "file",
            "subject": "x", "body_text": "unrelated",
            "chunks": [{"text": "zebra migration", "kind": "text",
                        "embedding": fake_embedding_provider.embed_query("zebra migration")}],
        },
    )
    # A Gmail document carrying a (stale, pre-revision) vector.
    es_client.index(
        index=es_index, id="2", refresh=True,
        document={
            "user_id": 1, "workspace_id": WS, "connection_id": 1, "external_id": "m-1", "source_type": "gmail",
            "subject": "x", "body_text": "unrelated",
            "chunks": [{"text": "zebra migration", "kind": "text",
                        "embedding": fake_embedding_provider.embed_query("zebra migration")}],
        },
    )

    assert SearchDocuments(search_index).execute(1, WS, "zebra migration") == []


def test_keyword_search_still_finds_uploads_and_email(
    es_client, es_index, fake_embedding_provider, chunk_factory
):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [_make_document(doc_id=1, user_id=1, connection_id=1, external_id="m-1",
                        subject="Invoice INV-2291", body_text="payment due")],
        SourceType.GMAIL, "a@gmail.com",
        WS,
    )
    search_index.index_documents(
        [_upload(2, ["invoice INV-2291 attached"], chunk_factory=chunk_factory)],
        SourceType.FILE, None, WS,
    )

    hits = SearchDocuments(search_index).execute(1, WS, "INV-2291")

    assert {h.source_type for h in hits} == {SourceType.GMAIL, SourceType.FILE}
    assert all("[" in h.snippet for h in hits)


def test_upload_without_chunks_stays_keyword_only(es_client, es_index, fake_embedding_provider):
    # e.g. an upload from before chunking, reindexed.
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [_make_document(doc_id=1, user_id=1, connection_id=9, external_id="u-1",
                        subject="old.txt", body_text="lunch plans")],
        SourceType.FILE, None, WS,
    )

    assert "chunks" not in es_client.get(index=es_index, id="1")["_source"]
    assert [h.document.external_id for h in SearchDocuments(search_index).execute(1, WS, "lunch")] == ["u-1"]


def test_semantic_search_finds_a_passage_deep_in_a_long_upload(
    es_client, es_index, real_embedding_provider, chunk_factory
):
    # The test that proves chunking works (specs/upload-chunking.md §11):
    # the relevant passage sits far past the first 512 tokens, where a single
    # whole-document embedding would never have seen it. The query shares
    # no words with it, so only the real model's semantics can find it.
    filler = [
        f"Section {i}. The office relocation checklist covers desks, cabling and parking permits."
        for i in range(40)
    ]
    relevant = "The subscription cost for next year has been updated; the annual fee is 20% higher."
    search_index = ElasticsearchIndex(es_client, es_index, real_embedding_provider)
    search_index.index_documents(
        [_upload(1, [*filler, relevant], subject="handbook.pdf", chunk_factory=chunk_factory)],
        SourceType.FILE, None, WS,
    )

    hits = SearchDocuments(search_index).execute(1, WS, "renewal pricing")

    assert [h.document.external_id for h in hits] == ["upload-1"]
    assert hits[0].snippet.startswith("The subscription cost")


def test_ensure_index_adds_chunks_field_to_a_pre_existing_index(es_client):
    index_name = f"findr_test_{uuid.uuid4().hex[:12]}"
    legacy_mapping = {
        "properties": {
            name: spec for name, spec in INDEX_MAPPING["properties"].items() if name != "chunks"
        }
    }
    es_client.indices.create(index=index_name, mappings=legacy_mapping)
    try:
        ensure_index(es_client, index_name)
        ensure_index(es_client, index_name)  # idempotent

        properties = es_client.indices.get_mapping(index=index_name)[index_name]["mappings"][
            "properties"
        ]
        chunks = properties["chunks"]
        assert chunks["type"] == "nested"
        assert chunks["properties"]["embedding"]["dims"] == 1024
        assert chunks["properties"]["metadata"]["properties"]["document_version"]["type"] == "integer"
        assert chunks["properties"]["metadata"]["properties"]["content_sha256"]["type"] == "keyword"
    finally:
        es_client.indices.delete(index=index_name, ignore_unavailable=True)


def test_reciprocal_rank_fusion_rewards_agreement_and_keeps_bm25_hit():
    bm25 = [{"_id": "a", "highlight": {"x": ["[a]"]}}, {"_id": "b"}]
    knn = [{"_id": "c"}, {"_id": "a"}]

    fused = _reciprocal_rank_fusion([bm25, knn])

    assert [raw["_id"] for raw, _ in fused] == ["a", "c", "b"]
    assert fused[0][0] is bm25[0]  # the highlighted copy wins
    assert fused[0][1] == 1 / 61 + 1 / 62


def test_search_never_crosses_workspaces(es_client, es_index, fake_embedding_provider, chunk_factory):
    # The client-isolation guarantee (specs/workspaces.md §2): identical
    # content in two workspaces of the same user — each search sees only its
    # own copy, through both the keyword and the semantic leg.
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    for doc_id, workspace in ((1, 100), (2, 200)):
        search_index.index_documents(
            [_upload(doc_id, ["zebra migration report"], chunk_factory=chunk_factory)],
            SourceType.FILE, None, workspace,
        )
    search = SearchDocuments(search_index)

    keyword = search.execute(1, 100, "report")
    semantic = search.execute(1, 200, "zebra migration report")

    assert [h.document.external_id for h in keyword] == ["upload-1"]
    assert keyword[0].document.workspace_id == 100
    assert [h.document.external_id for h in semantic] == ["upload-2"]
    assert search.execute(1, 300, "report") == []


def test_chunks_carry_the_workspace_in_their_metadata(
    es_client, es_index, fake_embedding_provider, chunk_factory
):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    upload = _upload(1, ["a chunk"], chunk_factory=chunk_factory)
    upload.chunks[0].metadata.workspace_id = WS
    search_index.index_documents([upload], SourceType.FILE, None, WS)

    source = es_client.get(index=es_index, id="1")["_source"]
    assert source["workspace_id"] == WS
    assert source["chunks"][0]["metadata"]["workspace_id"] == WS
    mapping = es_client.indices.get_mapping(index=es_index)[es_index]["mappings"]["properties"]
    assert mapping["workspace_id"]["type"] == "keyword"
    assert mapping["chunks"]["properties"]["metadata"]["properties"]["workspace_id"]["type"] == "keyword"


def test_delete_workspace_removes_only_that_workspaces_documents(
    es_client, es_index, fake_embedding_provider
):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    for doc_id, workspace in ((1, 100), (2, 200)):
        search_index.index_documents(
            [_make_document(doc_id=doc_id, user_id=1, connection_id=doc_id,
                            external_id=f"m-{doc_id}", subject="Renewal", body_text="renewal")],
            SourceType.GMAIL, "a@gmail.com", workspace,
        )

    search_index.delete_workspace(100)

    search = SearchDocuments(search_index)
    assert search.execute(1, 100, "renewal") == []
    assert len(search.execute(1, 200, "renewal")) == 1
