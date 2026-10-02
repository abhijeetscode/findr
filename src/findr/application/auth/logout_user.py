import logging

from findr.observability import log_event
from findr.ports.session_store import SessionStore

logger = logging.getLogger(__name__)


class LogoutUser:
    def __init__(self, session_store: SessionStore) -> None:
        self._session_store = session_store

    def execute(self, token: str) -> None:
        self._session_store.delete(token)
        log_event(logger, "auth.logout")
