from sqlalchemy import text
from sqlalchemy.orm import Session


def test_db_session_fixture_executes_query(db_session: Session):
    result = db_session.execute(text("SELECT 1")).scalar_one()
    assert result == 1
