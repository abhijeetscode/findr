from __future__ import annotations

from findr.adapters.outbound.gmail.gmail_connector import GmailConnector
from findr.adapters.outbound.gmail.gmail_oauth_provider import GmailOAuthProvider
from findr.config import Settings
from findr.domain.value_objects import SourceType
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.source_connector import SourceConnector

"""Per-source-type wiring, shared by anything that needs to sync or
authenticate a connection generically (the background scheduler, and the
manual "resync now" endpoint) — kept in one place so a new connector only
needs a branch added here, not in every caller."""


def oauth_provider_for(source_type: SourceType, settings: Settings) -> OAuthProvider:
    if source_type == SourceType.GMAIL:
        return GmailOAuthProvider(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
    raise ValueError(f"No OAuthProvider configured for source type {source_type!r}")


def connector_for(source_type: SourceType, user_id: int, connection_id: int) -> SourceConnector:
    if source_type == SourceType.GMAIL:
        return GmailConnector(user_id=user_id, connection_id=connection_id)
    raise ValueError(f"No SourceConnector configured for source type {source_type!r}")
