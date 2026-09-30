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


def _make_document(
    *, doc_id, user_id, connection_id, external_id, subject, body_text, thread_id=None
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
    )

    use_case = SearchDocuments(search_index)
    a_hits = use_case.execute(1, "renewal")
    b_hits = use_case.execute(2, "renewal")

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
    )

    search = SearchDocuments(search_index)
    new_hits = search.execute(1, "new")
    assert len(new_hits) == 1
    assert new_hits[0].document.subject == "New subject"

    assert search.execute(1, "old") == []


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
    )

    search = SearchDocuments(search_index)
    assert len(search.execute(1, "trashed")) == 1

    search_index.delete_documents(connection_id=1, external_ids=["msg-1"])

    assert search.execute(1, "trashed") == []


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
    )

    search = SearchDocuments(search_index)
    hits = search.execute(1, "renewal")

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
    )

    hits = SearchDocuments(search_index).execute(1, "renewal")

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
    )

    search = SearchDocuments(search_index)
    hits = search.execute(1, 'terms: "24" -months*')

    assert isinstance(hits, list)  # must not raise a query-syntax error


def test_semantic_query_finds_document_with_no_keyword_overlap(
    es_client, es_index, real_embedding_provider
):
    # The one test that proves semantic search actually works (see
    # specs/semantic-search.md §8): "renewal pricing" shares no word with
    # the pricing email, so BM25 alone returns nothing — only the kNN leg,
    # with the real model, can find it.
    search_index = ElasticsearchIndex(es_client, es_index, real_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-price",
                subject="Subscription update",
                body_text="The subscription cost for next year has been updated; "
                "the annual fee is 20% higher.",
            ),
            _make_document(
                doc_id=2,
                user_id=1,
                connection_id=1,
                external_id="msg-hike",
                subject="Offsite photos",
                body_text="Team offsite hiking trip photos from the mountains are in the album.",
            ),
        ],
        SourceType.GMAIL,
        "a@gmail.com",
    )

    hits = SearchDocuments(search_index).execute(1, "renewal pricing")

    assert [h.document.external_id for h in hits] == ["msg-price"]
    # kNN-only hits carry no highlight; the snippet falls back to the body.
    assert hits[0].snippet.startswith("The subscription cost")

    # And exact keywords still work, with highlight markers intact.
    keyword_hits = SearchDocuments(search_index).execute(1, "hiking")
    assert keyword_hits[0].document.external_id == "msg-hike"
    assert "[hiking]" in keyword_hits[0].snippet.lower()


def test_semantic_search_is_isolated_per_user(es_client, es_index, fake_embedding_provider):
    # The kNN leg carries its own user_id filter, separate from BM25's.
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=2,
                connection_id=2,
                external_id="msg-1",
                subject="Renewal terms",
                body_text="renewal terms",
            )
        ],
        SourceType.GMAIL,
        "b@gmail.com",
    )

    assert SearchDocuments(search_index).execute(1, "renewal terms") == []


def test_document_with_no_text_is_indexed_without_an_embedding(
    es_client, es_index, fake_embedding_provider
):
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    search_index.index_documents(
        [
            _make_document(
                doc_id=1,
                user_id=1,
                connection_id=1,
                external_id="msg-empty",
                subject=None,
                body_text=None,
            ),
            _make_document(
                doc_id=2,
                user_id=1,
                connection_id=1,
                external_id="msg-subject-only",
                subject="Lunch plans",
                body_text=None,
            ),
        ],
        SourceType.GMAIL,
        "a@gmail.com",
    )

    assert "embedding" not in es_client.get(index=es_index, id="1")["_source"]
    assert len(es_client.get(index=es_index, id="2")["_source"]["embedding"]) == 1024
    hits = SearchDocuments(search_index).execute(1, "lunch")
    assert [h.document.external_id for h in hits] == ["msg-subject-only"]
    # The vector is never shipped back in search results.
    assert all(h.document.body_text is None for h in hits)


def test_ensure_index_adds_embedding_field_to_a_pre_existing_index(es_client):
    index_name = f"findr_test_{uuid.uuid4().hex[:12]}"
    legacy_mapping = {
        "properties": {
            name: spec for name, spec in INDEX_MAPPING["properties"].items() if name != "embedding"
        }
    }
    es_client.indices.create(index=index_name, mappings=legacy_mapping)
    try:
        ensure_index(es_client, index_name)
        ensure_index(es_client, index_name)  # idempotent

        properties = es_client.indices.get_mapping(index=index_name)[index_name]["mappings"][
            "properties"
        ]
        assert properties["embedding"]["type"] == "dense_vector"
        assert properties["embedding"]["dims"] == 1024
    finally:
        es_client.indices.delete(index=index_name, ignore_unavailable=True)


def test_reciprocal_rank_fusion_rewards_agreement_and_keeps_bm25_hit():
    bm25 = [{"_id": "a", "highlight": {"x": ["[a]"]}}, {"_id": "b"}]
    knn = [{"_id": "c"}, {"_id": "a"}]

    fused = _reciprocal_rank_fusion([bm25, knn])

    assert [raw["_id"] for raw, _ in fused] == ["a", "c", "b"]
    assert fused[0][0] is bm25[0]  # the highlighted copy wins
    assert fused[0][1] == 1 / 61 + 1 / 62
