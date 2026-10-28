"""Chat without a configured LLM says so instead of failing (Task 4)."""

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
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _raising_graph(exc: Exception):
    class Graph:
        async def ainvoke(self, *args, **kwargs):
            raise exc

    return lambda use_tools=False: Graph()


def _ask(client: TestClient, model: str):
    return client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "How many persons?"}], "stream": False, "model": model},
    )


def test_no_key_anywhere_gets_setup_message(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("graph must not be built")

    monkeypatch.setattr(main, "get_graph", _boom)
    response = _ask(client, "")
    assert response.status_code == 200
    assert response.json()["messages"][0]["content"] == main.NO_LLM_MESSAGE


def test_key_but_no_model_gets_pick_message(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    def _boom(*args, **kwargs):
        raise AssertionError("graph must not be built")

    monkeypatch.setattr(main, "get_graph", _boom)
    response = _ask(client, "")
    assert response.json()["messages"][0]["content"] == main.NO_MODEL_MESSAGE


def test_stale_model_from_unknown_provider_is_a_chat_message(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    monkeypatch.setattr(main, "get_graph", _raising_graph(exc))
    response = _ask(client, "gpustack/qwen3.8-27b")
    assert response.status_code == 200
    assert response.json()["messages"][-1]["content"] == main.chat_error_message(exc)


def test_tool_capable_models_skips_empty_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main.settings, "default_llm_model", "")
    monkeypatch.setattr(main.settings, "available_llm_models", [])
    assert main.tool_capable_models() == []
