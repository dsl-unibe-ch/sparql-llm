"""An LLM failure reaches the user as a chat message, not a bare HTTP 500 (F13)."""

from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main

USER = SimpleNamespace(id=1, email="user@example.org", is_superuser=False, is_curator=False, is_active=True)


class _NoSession:
    async def get(self, *args, **kwargs):
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    main.app.dependency_overrides[main.current_active_user] = lambda: USER
    main.app.dependency_overrides[main.get_async_session] = lambda: _NoSession()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _failing_graph(exc: Exception):
    class Graph:
        async def ainvoke(self, *args, **kwargs):
            raise exc

    return lambda use_tools=False: Graph()


def _ask(client: TestClient):
    return client.post("/chat", json={"messages": [{"role": "user", "content": "How many persons?"}], "stream": False, "model": "p/m"})


def test_llm_timeout_becomes_chat_message(client, monkeypatch: pytest.MonkeyPatch):
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://llm.example/v1/chat/completions"))
    monkeypatch.setattr(main, "get_graph", _failing_graph(timeout))
    response = _ask(client)
    assert response.status_code == 200
    message = response.json()["messages"][-1]
    assert message["role"] == "assistant"
    assert message["content"] == "The language model did not answer in time. Please try again, or pick another model."


def test_other_failure_becomes_one_line_chat_message(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "get_graph", _failing_graph(ValueError("bad JSON from the model\nsecond line")))
    response = _ask(client)
    assert response.status_code == 200
    assert response.json()["messages"][-1]["content"] == "Something went wrong while answering: bad JSON from the model."
