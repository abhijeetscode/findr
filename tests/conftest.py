from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from findr.adapters.outbound.sqlite.db import create_db_engine, init_db


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_db_engine(":memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
