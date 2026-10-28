"""GET /models with auth on: an instance-level OPENAI_API_KEY serves users without a saved key."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main

USER = SimpleNamespace(id=1, email="u@example.org", is_superuser=False, is_curator=False, is_active=True)


class _Session:
    async def get(self, *args, **kwargs):
        return None  # no saved UserLLMConfig


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "auth_enabled", True)
    monkeypatch.setattr(main.settings, "default_llm_model", "m-default")
    monkeypatch.setattr(main.settings, "available_llm_models", ["m-a", "", "m-b"])
    main.app.dependency_overrides[main.require_user] = lambda: USER
    main.app.dependency_overrides[main.db_session] = lambda: _Session()
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_instance_key_lists_instance_models(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    data = client.get("/models").json()
    assert data["models"] == ["m-a", "m-b"]
    assert data["default"] == "m-default"
    assert not data.get("needs_setup")


def test_no_instance_key_needs_setup(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = client.get("/models").json()
    assert data == {"models": [], "needs_setup": True}
