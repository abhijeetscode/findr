from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    SESSION_COOKIE_NAME,
    get_current_user,
    get_db_session,
    get_settings,
)
from findr.adapters.outbound.crypto.password_hasher_argon2 import Argon2Hasher
from findr.adapters.outbound.sqlite.session_store_sqlite import SessionStoreSqlite
from findr.adapters.outbound.sqlite.user_repository_sqlite import UserRepositorySqlite
from findr.application.auth.login_user import LoginUser
from findr.application.auth.logout_user import LogoutUser
from findr.config import Settings
from findr.domain.entities import User
from findr.domain.exceptions import InvalidCredentials

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # Plain str, not EmailStr — there's no public sign-up, and the seeded
    # demo account's "email" (settings.demo_username) isn't a real address.
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    email: str


@router.post("/login", response_model=UserResponse)
def login(
    body: LoginRequest,
    response: Response,
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> UserResponse:
    user_repo = UserRepositorySqlite(db)
    session_store = SessionStoreSqlite(db, ttl_days=settings.session_ttl_days)
    use_case = LoginUser(user_repo, Argon2Hasher(), session_store)
    try:
        token = use_case.execute(body.email, body.password)
    except InvalidCredentials as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    db.commit()

    user = user_repo.get_by_email(body.email)
    assert user is not None  # just authenticated above; a miss here means a real bug

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path="/",
    )
    return UserResponse(id=user.id, email=user.email)


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> UserResponse:
    """Lets the frontend check for an existing valid session on page load
    (401 via get_current_user if there isn't one) without duplicating
    session-lookup logic."""
    return UserResponse(id=user.id, email=user.email)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db_session)) -> None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        LogoutUser(SessionStoreSqlite(db)).execute(token)
        db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
