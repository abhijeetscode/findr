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

    slack_client_id: str = Field(default="", validation_alias="SLACK_CLIENT_ID")
    slack_client_secret: str = Field(default="", validation_alias="SLACK_CLIENT_SECRET")
    slack_redirect_uri: str = Field(
        default="http://localhost:8000/sources/slack/callback",
        validation_alias="SLACK_REDIRECT_URI",
    )

    notion_client_id: str = Field(default="", validation_alias="NOTION_CLIENT_ID")
    notion_client_secret: str = Field(default="", validation_alias="NOTION_CLIENT_SECRET")
    notion_redirect_uri: str = Field(
        default="http://localhost:8000/sources/notion/callback",
        validation_alias="NOTION_REDIRECT_URI",
    )
