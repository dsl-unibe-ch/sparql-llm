"""Every chat request is answered from one dataset, the one the user selected and may use."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main
from tests.conftest import NORSSIT

USER = SimpleNamespace(id=1, email="user@example.org", is_superuser=False, is_curator=False, is_active=True)
CURATOR = SimpleNamespace(id=2, email="curator@example.org", is_superuser=False, is_curator=True, is_active=True)
SWISS = {"id": "swiss", "label": "Swiss", "endpoint_url": "https://a.example/sparql"}


class _NoSession:
    async def get(self, *args, **kwargs):
        return None


@pytest.fixture
def two_datasets(use_projects, tmp_path: Path):
    """``swiss`` published, ``norssit`` ready (rebuilt) but not published."""
    from sparql_llm.project import reload_projects

    use_projects(SWISS, NORSSIT)
    (tmp_path / "datasets.json").write_text(
        json.dumps([{"id": "swiss", "published": True}, {"id": "norssit", "published": False}]), encoding="utf-8"
    )
    (tmp_path / "norssit" / "fingerprint.json").write_text("{}", encoding="utf-8")
    reload_projects()


@pytest.fixture
def graph_calls(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    class Graph:
        async def ainvoke(self, inputs, config=None):
            calls.append(config["configurable"])
            return {"messages": []}

    monkeypatch.setattr(main, "get_graph", lambda use_tools=False: Graph())
    return calls


def _client_as(user):
    main.app.dependency_overrides[main.current_active_user] = lambda: user
    main.app.dependency_overrides[main.get_async_session] = lambda: _NoSession()
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main.app.dependency_overrides.clear()


def _ask(client: TestClient, **extra):
    body = {"messages": [{"role": "user", "content": "How many persons?"}], "stream": False, **extra}
    return client.post("/chat", json=body)


def test_user_cannot_ask_an_unpublished_dataset(two_datasets, graph_calls):
    response = _ask(_client_as(USER), project="norssit")

    assert response.status_code == 200
    assert response.json()["messages"][-1]["content"] == "The dataset 'norssit' is not available."
    assert graph_calls == []


def test_curator_can_ask_a_ready_dataset(two_datasets, graph_calls):
    response = _ask(_client_as(CURATOR), project="norssit")

    assert response.status_code == 200
    assert graph_calls[-1]["project"] == "norssit"


def test_unknown_dataset_refused(two_datasets, graph_calls):
    response = _ask(_client_as(CURATOR), project="ghost")

    assert response.json()["messages"][-1]["content"] == "The dataset 'ghost' is not available."
    assert graph_calls == []


def test_missing_project_means_first_available(two_datasets, graph_calls):
    _ask(_client_as(USER))

    assert graph_calls[-1]["project"] == "swiss"


def test_project_of_reads_the_configuration(two_datasets):
    from sparql_llm.agent.request_project import project_of
    from sparql_llm.config import Configuration

    assert project_of(Configuration(project="norssit")).id == "norssit"
    assert project_of(Configuration()).id == "swiss"
