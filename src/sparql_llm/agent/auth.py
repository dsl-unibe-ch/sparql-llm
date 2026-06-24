"""Authentication module using fastapi-users with SQLite backend and JWT cookies."""

from collections.abc import AsyncGenerator

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from sparql_llm.config import settings

# ── Database setup ────────────────────────────────────────────────────────────

DATABASE_URL = f"sqlite+aiosqlite:///{settings.auth_db_path}"

engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    pass


async def create_db_and_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User)


# ── UserManager ───────────────────────────────────────────────────────────────

SECRET = settings.auth_secret


class UserManager(UUIDIDMixin, BaseUserManager[User, "uuid.UUID"]):  # type: ignore[type-arg]
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


# ── Auth backend: cookie + JWT ────────────────────────────────────────────────

cookie_transport = CookieTransport(
    cookie_name="sparql_llm_auth",
    cookie_max_age=60 * 60 * 24 * 7,  # 7 days
    cookie_secure=False,  # set True when serving over HTTPS
    cookie_httponly=True,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=SECRET, lifetime_seconds=60 * 60 * 24 * 7)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, "uuid.UUID"](get_user_manager, [auth_backend])  # type: ignore[type-arg]

# ── Dependency helpers ────────────────────────────────────────────────────────

current_active_user = fastapi_users.current_user(active=True)
optional_current_user = fastapi_users.current_user(active=True, optional=True)


async def require_authenticated(
    request: Request,
    user=Depends(optional_current_user),
):
    """Dependency that returns the user or raises a redirect to /login."""
    from fastapi.responses import RedirectResponse

    if user is None:
        raise _LoginRedirect(request.url.path)
    return user


class _LoginRedirect(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


import uuid  # noqa: E402 (needed after forward reference above)
