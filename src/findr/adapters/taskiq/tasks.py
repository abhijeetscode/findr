"""The upload worker — run with:

    taskiq worker findr.adapters.taskiq.tasks:broker --workers 1 \
        --max-async-tasks 1 --max-prefetch 1 --ack-type when_executed

One process, one upload at a time: the work is CPU-bound (OCR, layout and
table models, embeddings). See specs/upload-chunking.md §5.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from elasticsearch import Elasticsearch
from sqlalchemy.orm import sessionmaker
from taskiq import Context, TaskiqDepends, TaskiqEvents, TaskiqState

from findr.adapters.outbound.elasticsearch.es_client import create_es_client, ensure_index
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.files.local_file_storage import LocalFileStorage
from findr.adapters.outbound.postgres.db import create_db_engine, init_db
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.unit_of_work_postgres import UnitOfWorkPostgres
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.adapters.outbound.system_clock import SystemClock
from findr.adapters.taskiq.broker import broker
from findr.application.uploads.process_upload import Outcome, ProcessUpload
from findr.config import Settings
from findr.ports.document_parser import DocumentParser
from findr.ports.embedding_provider import EmbeddingProvider

logger = logging.getLogger(__name__)

# Delay before re-enqueuing an upload that hit an infrastructure error.
RETRY_DELAY_SECONDS = 30


@dataclass
class WorkerResources:
    """Loaded once per worker process — the models are GBs and slow to load."""

    settings: Settings
    session_factory: sessionmaker
    es_client: Elasticsearch
    embedding_provider: EmbeddingProvider
    document_parser: DocumentParser


def build_worker_resources(settings: Settings) -> WorkerResources:
    # Imported here so the API process (which imports this module for the
    # task definition) never loads the parser's heavy dependencies.
    from findr.adapters.outbound.embeddings.sentence_transformer_provider import (
        create_embedding_provider,
    )
    from findr.adapters.outbound.files.unstructured_document_parser import (
        UnstructuredDocumentParser,
    )

    engine = create_db_engine(settings.database_url)
    init_db(engine)
    es_client = create_es_client(settings.elasticsearch_url)
    ensure_index(es_client, settings.elasticsearch_index)
    parser = UnstructuredDocumentParser()
    # Fail fast: a model that can't load would otherwise silently drop
    # tables from every PDF (see warm_up).
    parser.warm_up()
    return WorkerResources(
        settings=settings,
        session_factory=sessionmaker(bind=engine),
        es_client=es_client,
        embedding_provider=create_embedding_provider(settings),
        document_parser=parser,
    )


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    logging.basicConfig(level=logging.INFO)
    state.resources = await asyncio.to_thread(build_worker_resources, Settings())
    logger.info("Upload worker ready")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    resources: WorkerResources | None = getattr(state, "resources", None)
    if resources is not None:
        resources.es_client.close()


def run_process_upload(resources: WorkerResources, upload_id: int) -> Outcome:
    """Blocking: one ProcessUpload run with its own DB session."""
    settings = resources.settings
    db = resources.session_factory()
    try:
        use_case = ProcessUpload(
            UploadedFileRepositoryPostgres(db),
            LocalFileStorage(settings.upload_storage_root),
            resources.document_parser,
            SourceConnectionRepositoryPostgres(db),
            DocumentRepositoryPostgres(db),
            ElasticsearchIndex(
                resources.es_client,
                settings.elasticsearch_index,
                resources.embedding_provider,
                min_similarity=settings.semantic_min_similarity,
            ),
            UnitOfWorkPostgres(db),
            SystemClock(),
        )
        return use_case.execute(upload_id)
    finally:
        db.close()


@broker.task(task_name="findr.process_upload")
async def process_upload(upload_id: int, context: Context = TaskiqDepends()) -> None:
    resources: WorkerResources = context.state.resources
    logger.info("Processing upload %s", upload_id)
    # CPU-bound and blocking: off the event loop, so the worker keeps its
    # Redis connection alive during a minutes-long OCR run.
    outcome = await asyncio.to_thread(run_process_upload, resources, upload_id)
    logger.info("Upload %s: %s", upload_id, outcome.value)
    if outcome == Outcome.RETRY:
        await asyncio.sleep(RETRY_DELAY_SECONDS)
        await process_upload.kiq(upload_id)
