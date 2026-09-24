from dataclasses import dataclass
from typing import Protocol

from findr.domain.entities import Credentials, Document


@dataclass
class ChangeBatch:
    upserts: list[Document]
    deleted_external_ids: list[str]
    new_cursor: str


class SourceConnector(Protocol):
    """Implemented once per connected source (Gmail now; Slack/Drive/etc. later)."""

    def fetch_changes(self, credentials: Credentials, cursor: str | None) -> ChangeBatch: ...
