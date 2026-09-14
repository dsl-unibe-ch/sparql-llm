"""Saved conversations: each user's chat history, private to its owner.

The chat UI saves the whole thread after every turn — the text, steps and links exactly
as it rendered them — under its session id. The server does not rebuild the thread from
the agent's stream, because the UI's rendering (fixed answers, folded tool activity,
replaced drafts) lives in ``providers.ts`` and would otherwise be duplicated here.

Nobody but the owner can read, change or even detect a chat: every query is filtered by
the logged-in user, and another user's chat answers 404, never 403. Admins get no view
of other users' chats either.

Mounted only when auth is enabled — without a user there is nobody to own a chat.
"""

import html
import re
import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi_users_db_sqlalchemy.generics import now_utc
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sparql_llm.agent.auth import Conversation, User, current_active_user, get_async_session

#: A saved chat carries every tool result, SPARQL JSON included, so it is capped.
MAX_BODY_BYTES = 2_000_000
#: The sidebar shows this many of the most recent chats.
LIST_LIMIT = 200
TITLE_MAX = 80

ConversationId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

router = APIRouter(prefix="/conversations", tags=["conversations"])


class StoredMessage(BaseModel):
    """One message of a saved chat, in the shape ``ChatState.serialize`` produces."""

    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"]
    content: str
    steps: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []


class SaveRequest(BaseModel):
    messages: list[StoredMessage]


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


def title_from(messages: list[dict[str, Any]]) -> str:
    """A chat's default title: its first question, on one line and shortened."""
    question = next((m["content"] for m in messages if m["role"] == "user"), "")
    text = " ".join(question.split())
    if len(text) > TITLE_MAX:
        text = text[: TITLE_MAX - 1].rstrip() + "…"
    return text or "New chat"


def conversation_to_markdown(
    title: str, messages: list[dict[str, Any]], created_at: datetime | None = None
) -> str:
    """Render a saved chat as Markdown: the answers in full, each step folded away.

    Steps go in ``<details>`` blocks, so the SPARQL queries and results the agent used
    are kept without drowning the prose; step text is inserted as-is, so the fences the
    UI chose to wrap tool results stay intact.
    """
    parts = [f"# {title}"]
    if created_at:
        parts.append(f"_Swiss Elites Chat · started {created_at:%Y-%m-%d %H:%M} UTC_")
    for msg in messages:
        if msg["role"] == "user":
            parts.append(f"## You\n\n{msg['content']}")
            continue
        section = ["## Assistant"]
        section += [block for step in msg.get("steps") or [] if (block := _step_markdown(step))]
        if msg.get("content"):
            section.append(msg["content"])
        links = [f"- [{link.get('label') or link['url']}]({link['url']})" for link in msg.get("links") or [] if link.get("url")]
        if links:
            section.append("\n".join(links))
        parts.append("\n\n".join(section))
    return "\n\n".join(parts) + "\n"


def _step_markdown(step: dict[str, Any]) -> str:
    """A step as a folded block; a bare status label with nothing behind it is dropped."""
    details = (step.get("details") or "").strip()
    substeps = [s for s in step.get("substeps") or [] if s.get("details")]
    if not details and not substeps:
        return ""
    body = [details] if details else []
    body += [f"**{html.escape(s.get('label', ''))}**\n\n{s['details'].strip()}" for s in substeps]
    summary = html.escape(step.get("label", ""))
    return f"<details>\n<summary>{summary}</summary>\n\n" + "\n\n".join(body) + "\n\n</details>"


def _filename(title: str, extension: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_title).strip("-").lower()[:60]
    return f"{slug or 'conversation'}.{extension}"


def _summary(conv: Conversation) -> dict[str, Any]:
    return {"id": conv.id, "title": conv.title, "updated_at": conv.updated_at.isoformat()}


async def _owned(session: AsyncSession, conversation_id: str, user: User) -> Conversation:
    """The user's own chat, or 404 — also when the chat exists but belongs to someone else."""
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


async def delete_conversations_of(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Remove all of a user's chats; the caller commits. Used when an account is removed.

    SQLite does not enforce foreign keys unless asked to, so a cascade cannot be relied on.
    """
    await session.execute(delete(Conversation).where(Conversation.user_id == user_id))


@router.get("")
async def list_conversations(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> list[dict[str, Any]]:
    """The user's chats for the sidebar, most recently active first."""
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(LIST_LIMIT)
    )
    return [_summary(conv) for conv in result.scalars()]


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: ConversationId,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    conv = await _owned(session, conversation_id, user)
    return {**_summary(conv), "created_at": conv.created_at.isoformat(), "messages": conv.messages}


@router.put("/{conversation_id}")
async def save_conversation(
    conversation_id: ConversationId,
    request: Request,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Create or replace the user's chat with the thread the UI now shows.

    The body is size-checked before it is parsed, so an oversized chat costs no parsing.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="This conversation is too large to save.")
    try:
        payload = SaveRequest.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    messages = [message.model_dump() for message in payload.messages]

    conv = await session.get(Conversation, conversation_id)
    if conv is not None and conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv is None:
        conv = Conversation(id=conversation_id, user_id=user.id, title=title_from(messages))
        session.add(conv)
    elif not conv.title:
        conv.title = title_from(messages)
    conv.messages = messages
    conv.updated_at = now_utc()
    await session.commit()
    return _summary(conv)


@router.patch("/{conversation_id}")
async def rename_conversation(
    conversation_id: ConversationId,
    rename: RenameRequest,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    conv = await _owned(session, conversation_id, user)
    conv.title = " ".join(rename.title.split())[:120] or conv.title
    await session.commit()
    return _summary(conv)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: ConversationId,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    conv = await _owned(session, conversation_id, user)
    await session.delete(conv)
    await session.commit()
    return Response(status_code=204)


@router.get("/{conversation_id}/export")
async def export_conversation(
    conversation_id: ConversationId,
    export_format: Literal["md", "json"] = Query("md", alias="format"),
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Download one chat, as readable Markdown or as the full saved JSON."""
    conv = await _owned(session, conversation_id, user)
    headers = {"Content-Disposition": f'attachment; filename="{_filename(conv.title, export_format)}"'}
    if export_format == "json":
        exported = {
            "id": conv.id,
            "title": conv.title,
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
            "messages": conv.messages,
        }
        return JSONResponse(exported, headers=headers)
    markdown = conversation_to_markdown(conv.title, conv.messages, conv.created_at)
    return Response(markdown, media_type="text/markdown; charset=utf-8", headers=headers)
