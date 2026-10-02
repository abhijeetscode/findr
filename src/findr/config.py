from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App configuration, loaded from environment variables (and .env in dev).

    Env var names intentionally don't share one prefix: FINDR_* for
    Findr-internal config, GOOGLE_OAUTH_* for values that name a Google Cloud
    OAuth client. See .env.example.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://findr:findr@localhost:5432/findr",
        validation_alias="FINDR_DATABASE_URL",
    )
    elasticsearch_url: str = Field(
        default="http://localhost:9200", validation_alias="FINDR_ELASTICSEARCH_URL"
    )
    elasticsearch_index: str = Field(
        default="findr_documents", validation_alias="FINDR_ELASTICSEARCH_INDEX"
    )
    session_secret: str = Field(
        default="dev-only-change-me", validation_alias="FINDR_SESSION_SECRET"
    )
    token_encryption_key: str = Field(
        default="", validation_alias="FINDR_TOKEN_ENCRYPTION_KEY"
    )
    sync_interval_seconds: int = Field(
        default=300, validation_alias="FINDR_SYNC_INTERVAL_SECONDS"
    )
    environment: str = Field(default="development", validation_alias="FINDR_ENV")
    session_ttl_days: int = Field(default=14, validation_alias="FINDR_SESSION_TTL_DAYS")

    # Where uploaded files' original bytes live — a Docker volume in
    # docker-compose.yml. See specs/file-upload.md §3.
    upload_storage_root: str = Field(
        default="/data/uploads", validation_alias="FINDR_UPLOAD_STORAGE_ROOT"
    )

    # Local sentence-transformers model for semantic search — see
    # specs/semantic-search.md §3. Its output dimension must match
    # es_client.EMBEDDING_DIMS.
    embedding_model: str = Field(
        default="Qwen/Qwen3-Embedding-0.6B", validation_alias="FINDR_EMBEDDING_MODEL"
    )
    embedding_max_seq_length: int = Field(
        default=512, validation_alias="FINDR_EMBEDDING_MAX_SEQ_LENGTH"
    )
    # Queue for background upload processing (specs/upload-chunking.md §5).
    # "memory://" gives Taskiq's in-process broker — for tests only.
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="FINDR_REDIS_URL")

    # Cosine-similarity floor for the kNN leg of hybrid search — see
    # search_index_elasticsearch.DEFAULT_MIN_SIMILARITY.
    semantic_min_similarity: float = Field(
        default=0.4, validation_alias="FINDR_SEMANTIC_MIN_SIMILARITY"
    )

    # Logging goes to files only, one per process (specs/logging-telemetry.md
    # §4.6). docker-compose.yml points the directory at its logs volume.
    log_dir: str = Field(default="./data/logs", validation_alias="FINDR_LOG_DIR")
    log_level: str = Field(default="DEBUG", validation_alias="FINDR_LOG_LEVEL")
    # Separate level for noisy third-party libraries (observability.setup.LIBRARY_LOGGERS).
    log_level_libs: str = Field(default="WARNING", validation_alias="FINDR_LOG_LEVEL_LIBS")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, validation_alias="FINDR_LOG_MAX_BYTES")
    log_backup_count: int = Field(default=5, validation_alias="FINDR_LOG_BACKUP_COUNT")

    # There's no public sign-up; the app seeds a single fixed demo account on
    # startup (see app.py's lifespan) rather than exposing a /auth/register
    # endpoint. Override via env if you don't want the published defaults.
    demo_username: str = Field(default="demouser", validation_alias="FINDR_DEMO_USERNAME")
    demo_password: str = Field(default="password@2050", validation_alias="FINDR_DEMO_PASSWORD")

    google_oauth_client_id: str = Field(default="", validation_alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: str = Field(
        default="", validation_alias="GOOGLE_OAUTH_CLIENT_SECRET"
    )
    google_oauth_redirect_uri: str = Field(
        default="http://localhost:8000/sources/gmail/callback",
        validation_alias="GOOGLE_OAUTH_REDIRECT_URI",
    )
