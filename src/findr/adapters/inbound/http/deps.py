from collections.abc import Iterator

from elasticsearch import Elasticsearch
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from findr.adapters.outbound.postgres.session_store_postgres import SessionStorePostgres
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.config import Settings
from findr.domain.entities import User

SESSION_COOKIE_NAME = "findr_session"


def get_db_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_es_client(request: Request) -> Elasticsearch:
    return request.app.state.es_client


def get_current_user(request: Request, db: Session = Depends(get_db_session)) -> User:
    """Dependency for any route that requires a logged-in user (e.g. the
    Gmail-connect and search routers added in later steps)."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user_id = SessionStorePostgres(db).get_user_id(token) if token else None
    user = UserRepositoryPostgres(db).get_by_id(user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user
