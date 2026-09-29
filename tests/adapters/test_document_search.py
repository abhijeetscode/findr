from datetime import datetime

from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
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


def test_search_is_isolated_per_user(es_client, es_index):
    search_index = ElasticsearchIndex(es_client, es_index)
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


def test_upsert_dedups_on_document_id(es_client, es_index):
    search_index = ElasticsearchIndex(es_client, es_index)
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


def test_delete_documents_removes_from_search(es_client, es_index):
    search_index = ElasticsearchIndex(es_client, es_index)
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


def test_thread_id_round_trips_through_index_and_search(es_client, es_index):
    search_index = ElasticsearchIndex(es_client, es_index)
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


def test_search_highlights_matches_with_bracket_markers(es_client, es_index):
    # The frontend's renderSnippet() JS turns "[" / "]" into <strong> tags —
    # this pins ES's highlight config to those exact markers (see
    # specs/elasticsearch-search.md §5), not its default <em> tags.
    search_index = ElasticsearchIndex(es_client, es_index)
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


def test_query_with_special_characters_does_not_error(es_client, es_index):
    search_index = ElasticsearchIndex(es_client, es_index)
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
