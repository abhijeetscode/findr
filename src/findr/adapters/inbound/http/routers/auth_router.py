from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    SESSION_COOKIE_NAME,
    get_db_session,
    get_settings,
)
from findr.adapters.outbound.crypto.password_hasher_argon2 import Argon2Hasher
from findr.adapters.outbound.sqlite.session_store_sqlite import SessionStoreSqlite
from findr.adapters.outbound.sqlite.user_repository_sqlite import UserRepositorySqlite
from findr.application.auth.login_user import LoginUser
from findr.application.auth.logout_user import LogoutUser
from findr.application.auth.register_user import RegisterUser
from findr.config import Settings
from findr.domain.exceptions import DuplicateUser, InvalidCredentials

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_db_session)) -> UserResponse:
    use_case = RegisterUser(UserRepositorySqlite(db), Argon2Hasher())
    try:
        user = use_case.execute(body.email, body.password)
    except DuplicateUser as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return UserResponse(id=user.id, email=user.email)


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


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db_session)) -> None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        LogoutUser(SessionStoreSqlite(db)).execute(token)
        db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
