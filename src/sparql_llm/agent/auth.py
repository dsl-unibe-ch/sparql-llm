"""Authentication module using fastapi-users with SQLite backend and JWT cookies."""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.generics import GUID, TIMESTAMPAware, now_utc
from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, false
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sparql_llm.agent.roles import ensure_role_column
from sparql_llm.config import Settings, SettingsError, settings

# ── Database setup ────────────────────────────────────────────────────────────

DATABASE_URL = f"sqlite+aiosqlite:///{settings.auth_db_path}"

engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    # Curator role (see sparql_llm.agent.roles). Admin stays fastapi-users' is_superuser.
    is_curator: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class Conversation(Base):
    """One saved chat, private to its owner (see sparql_llm.agent.conversations).

    The thread is stored whole, as the chat UI showed it — each message's text, steps
    and links — because the client always rewrites all of it and nothing queries
    inside it. Being a new table, ``create_all`` adds it to an existing database.
    """

    __tablename__ = "conversation"
    __table_args__ = (Index("ix_conversation_user_updated", "user_id", "updated_at"),)

    #: The chat UI's session id, which also groups the chat's traces in Langfuse.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("user.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    #: The dataset the chat asked about; the history lists only the selected dataset's chats.
    project: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    #: The chat's model, MCP tools switch and limits, restored when the chat is resumed.
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPAware(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPAware(timezone=True), default=now_utc, nullable=False)


class UserLLMConfig(Base):
    """Per-user LLM provider configuration (provider type, encrypted API key, base URL).

    Each user has at most one row.  When the user changes provider the row is
    updated in place.  The API key is Fernet-encrypted at rest using
    ``AUTH_SECRET`` (see ``sparql_llm.agent.crypto``).
    """

    __tablename__ = "user_llm_config"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("user.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="gpustack")
    encrypted_api_key: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    base_url: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPAware(timezone=True), default=now_utc, nullable=False)


async def create_db_and_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all never alters an existing table, so a database created before the
        # curator role existed gets the column added here.
        await conn.run_sync(ensure_role_column)


async def migrate_conversation_project(engine: Any, first_project_id: str) -> bool:
    """Bring an older conversation table up to date in place. Idempotent.

    ``create_all`` never alters an existing table, so the columns added since are added
    here: ``project`` (then every chat without a dataset gets the first one) and
    ``settings`` (empty for older chats). Returns whether anything changed.
    """
    async with engine.begin() as conn:
        rows = (await conn.exec_driver_sql('PRAGMA table_info("conversation")')).fetchall()
        columns = {row[1] for row in rows}
        if not columns:
            return False
        added = "project" not in columns
        if added:
            await conn.exec_driver_sql(
                "ALTER TABLE conversation ADD COLUMN project VARCHAR(64) NOT NULL DEFAULT ''"
            )
        if "settings" not in columns:
            await conn.exec_driver_sql("ALTER TABLE conversation ADD COLUMN settings JSON NOT NULL DEFAULT '{}'")
            added = True
        updated = await conn.exec_driver_sql(
            "UPDATE conversation SET project = ? WHERE project = ''", (first_project_id,)
        )
        return added or bool(updated.rowcount)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User)


# ── UserManager ───────────────────────────────────────────────────────────────

SECRET = settings.auth_secret

#: Shorter secrets are guessable; the placeholder is public. Either would let anyone forge
#: login tokens and decrypt the saved provider keys, which are encrypted with it.
MIN_SECRET_LENGTH = 32


def check_auth_secret() -> None:
    """Refuse to run with login on and a placeholder or short ``auth_secret``."""
    if not settings.auth_enabled:
        return
    if settings.auth_secret == Settings.model_fields["auth_secret"].default or len(settings.auth_secret) < MIN_SECRET_LENGTH:
        raise SettingsError(
            f"AUTH_SECRET is the placeholder or shorter than {MIN_SECRET_LENGTH} characters. "
            "Set a long random secret (e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`), "
            "and keep it: the saved provider keys are encrypted with it."
        )


def cookie_secure() -> bool:
    """Whether the login cookie is sent over HTTPS only: the setting, else on under a public name."""
    return settings.cookie_secure if settings.cookie_secure is not None else bool(settings.app_public_host)


class UserManager(UUIDIDMixin, BaseUserManager[User, "uuid.UUID"]):  # type: ignore[type-arg]
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


# ── Auth backend: cookie + JWT ────────────────────────────────────────────────

cookie_transport = CookieTransport(
    cookie_name="sparql_llm_auth",
    cookie_max_age=60 * 60 * 24 * 7,  # 7 days
    cookie_secure=cookie_secure(),
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
