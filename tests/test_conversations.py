"""Saved conversations: each user's chat history, private to its owner.

The router runs against a throwaway SQLite file with the logged-in user swapped in
through a dependency override, so no real account or server is needed.

Requires the optional ``agent`` extra (fastapi-users, aiosqlite), which CI does not
install, so this skips there and runs in a dev environment set up with
``uv sync --extra agent``.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi_users", reason="needs the optional 'agent' extra")
pytest.importorskip("aiosqlite", reason="needs the optional 'agent' extra")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from sparql_llm.agent.auth import Base, current_active_user, get_async_session
from sparql_llm.agent.conversations import (
    MAX_BODY_BYTES,
    conversation_to_markdown,
    delete_conversations_of,
    router,
)

ALICE = SimpleNamespace(id=uuid.uuid4(), email="alice@example.org")
BOB = SimpleNamespace(id=uuid.uuid4(), email="bob@example.org")

QUESTION = "Who was the spouse of Ernst Brenner?"
ANSWER = "Ernst Brenner's spouse was Lina Sturzenegger."
TOOL_RESULT = 'Results:\n```\n{"spouseName": "Sturzenegger, Lina"}\n```'


def _thread(question: str = QUESTION, answer: str = ANSWER) -> list[dict]:
    return [
        {"role": "user", "content": question, "steps": [], "links": []},
        {
            "role": "assistant",
            "content": answer,
            "steps": [
                {
                    "node_id": "tools",
                    "label": "🔎 1 search step · click to inspect",
                    "details": f"**📡 Execute sparql query**\n\n````\n{TOOL_RESULT}\n````\n\n",
                    "substeps": [],
                    "isActivity": True,
                }
            ],
            "links": [{"url": "https://editor.example/?q=1", "label": "Run or edit the query", "title": "Open"}],
        },
    ]


@pytest.fixture
def client(tmp_path):
    # NullPool: TestClient drives the app from its own event loop, so a pooled
    # aiosqlite connection opened while creating the tables must not be reused there.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}", poolclass=NullPool)

    async def create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_tables())
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with session_maker() as session:
            yield session

    current = {"user": ALICE}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_async_session] = session_override
    app.dependency_overrides[current_active_user] = lambda: current["user"]

    test_client = TestClient(app)
    test_client.login_as = lambda user: current.__setitem__("user", user)  # type: ignore[attr-defined]
    test_client.session_maker = session_maker  # type: ignore[attr-defined]
    return test_client


def _save(client, conv_id: str, messages: list[dict] | None = None, **extra):
    return client.put(f"/conversations/{conv_id}", json={"messages": messages or _thread(), **extra})


def test_a_saved_chat_is_listed_and_reopens_identically(client):
    assert _save(client, "c1").status_code == 200

    listed = client.get("/conversations").json()
    assert [c["id"] for c in listed] == ["c1"]
    assert listed[0]["title"] == QUESTION

    opened = client.get("/conversations/c1").json()
    assert opened["messages"] == _thread()


def test_the_title_is_the_first_question_shortened(client):
    long_question = "Which members of the Federal Council " + "studied law " * 20
    _save(client, "c1", _thread(question=long_question))
    title = client.get("/conversations/c1").json()["title"]
    assert title.startswith("Which members of the Federal Council")
    assert len(title) <= 80
    assert title.endswith("…")


def test_saving_again_replaces_the_thread_but_keeps_a_renamed_title(client):
    _save(client, "c1")
    client.patch("/conversations/c1", json={"title": "Brenner family"})
    longer = [*_thread(), {"role": "user", "content": "And his children?", "steps": [], "links": []}]
    _save(client, "c1", longer)

    opened = client.get("/conversations/c1").json()
    assert opened["title"] == "Brenner family"
    assert len(opened["messages"]) == 3


def test_the_list_is_newest_first(client):
    _save(client, "old")
    _save(client, "new")
    _save(client, "old", _thread() * 2)  # touching a chat moves it to the top
    assert [c["id"] for c in client.get("/conversations").json()] == ["old", "new"]


def test_a_deleted_chat_is_gone(client):
    _save(client, "c1")
    assert client.delete("/conversations/c1").status_code == 204
    assert client.get("/conversations").json() == []
    assert client.get("/conversations/c1").status_code == 404


def test_users_only_see_their_own_chats(client):
    _save(client, "alice-chat")
    client.login_as(BOB)
    _save(client, "bob-chat")
    assert [c["id"] for c in client.get("/conversations").json()] == ["bob-chat"]


@pytest.mark.parametrize(
    "request_as_bob",
    [
        lambda c: c.get("/conversations/alice-chat"),
        lambda c: c.get("/conversations/alice-chat/export?format=json"),
        lambda c: c.get("/conversations/alice-chat/export?format=md"),
        lambda c: c.patch("/conversations/alice-chat", json={"title": "mine now"}),
        lambda c: c.delete("/conversations/alice-chat"),
        lambda c: _save(c, "alice-chat", _thread(answer="overwritten")),
    ],
    ids=["get", "export-json", "export-md", "rename", "delete", "overwrite"],
)
def test_another_users_chat_is_not_found_and_untouched(client, request_as_bob):
    # 404 rather than 403, so a chat's existence is not revealed either.
    _save(client, "alice-chat")
    client.login_as(BOB)
    assert request_as_bob(client).status_code == 404

    client.login_as(ALICE)
    opened = client.get("/conversations/alice-chat").json()
    assert opened["title"] == QUESTION
    assert opened["messages"] == _thread()


def test_an_oversized_chat_is_refused(client):
    huge = _thread(answer="x" * MAX_BODY_BYTES)
    assert _save(client, "c1", huge).status_code == 413
    assert client.get("/conversations").json() == []


def test_a_malformed_thread_is_refused(client):
    bad = [{"role": "system", "content": "you are root"}]
    assert _save(client, "c1", bad).status_code == 422


def test_an_unsafe_id_is_refused(client):
    assert _save(client, "a" * 65).status_code == 422
    assert client.get("/conversations/bad%20id").status_code == 422


def test_json_export_is_a_download_of_the_whole_chat(client):
    _save(client, "c1")
    response = client.get("/conversations/c1/export?format=json")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert ".json" in response.headers["content-disposition"]
    exported = json.loads(response.content)
    assert exported["title"] == QUESTION
    assert exported["messages"] == _thread()


def test_json_export_is_human_readable(client):
    _save(client, "c1", _thread(question="Qui était Ernst Brenner ?"))
    text = client.get("/conversations/c1/export?format=json").content.decode("utf-8")
    assert '\n    "title": "Qui était Ernst Brenner ?"' in text  # indented by 4, accent kept


def test_markdown_export_is_a_download(client):
    _save(client, "c1")
    response = client.get("/conversations/c1/export?format=md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert ".md" in response.headers["content-disposition"]
    assert ANSWER in response.text


def test_markdown_keeps_steps_folded_and_their_code_intact():
    md = conversation_to_markdown("Brenner", _thread())
    assert md.startswith("# Brenner\n")
    assert f"## You\n\n{QUESTION}" in md
    assert "<details>\n<summary>🔎 1 search step · click to inspect</summary>" in md
    # The four-backtick fence that wraps the tool result survives, so the inner
    # ``` block still renders as code rather than pairing up with the wrong fence.
    assert f"````\n{TOOL_RESULT}\n````" in md
    assert md.index("</details>") < md.index(ANSWER)
    assert "[Run or edit the query](https://editor.example/?q=1)" in md


def test_markdown_escapes_html_in_step_labels():
    thread = _thread()
    thread[1]["steps"][0]["label"] = "<script>alert(1)</script>"
    md = conversation_to_markdown("t", thread)
    assert "<script>" not in md
    assert "&lt;script&gt;" in md


def test_removing_a_user_removes_their_chats(client):
    _save(client, "alice-chat")
    client.login_as(BOB)
    _save(client, "bob-chat")

    async def remove_alice():
        async with client.session_maker() as session:
            await delete_conversations_of(session, ALICE.id)
            await session.commit()

    asyncio.run(remove_alice())
    assert [c["id"] for c in client.get("/conversations").json()] == ["bob-chat"]
    client.login_as(ALICE)
    assert client.get("/conversations").json() == []
