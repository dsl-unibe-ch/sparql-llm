"""MCP is served per dataset under /mcp/<id>; a tool can never reach another dataset."""

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.testclient import TestClient

from sparql_llm import mcp_server
from sparql_llm.indexing import rebuild

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}
B = {"id": "b", "label": "Beta", "endpoint_url": "https://b.example/sparql", "topics": "beta things"}


@pytest.fixture
def two(use_projects, tmp_path: Path):
    """a published, b ready (rebuilt) but not published."""
    from sparql_llm.project import reload_projects

    projects = {p.id: p for p in use_projects(A, B)}
    (tmp_path / "datasets.json").write_text(
        json.dumps([{"id": "a", "published": True}, {"id": "b", "published": False}]), encoding="utf-8"
    )
    (tmp_path / "b" / "fingerprint.json").write_text("{}", encoding="utf-8")
    reload_projects()
    return projects


def _tools(project_id: str):
    return {t.name: t for t in mcp_server.get_mcp_app(project_id)._tool_manager.list_tools()}


def test_mcp_tools_are_bound_to_their_dataset(two, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rebuild, "_aliases", lambda: {"a__docs", "b__docs"})
    searched: list[str] = []
    executed: list[str] = []

    def query_points(collection_name, **kwargs):
        searched.append(collection_name)
        return SimpleNamespace(points=[])

    monkeypatch.setattr(mcp_server, "qdrant_client", SimpleNamespace(query_points=query_points))
    monkeypatch.setattr(mcp_server, "embedding_model", SimpleNamespace(embed_query=lambda texts: [[0.0] for _ in texts]))
    monkeypatch.setattr(mcp_server, "validate_sparql", lambda *a: {"fixed_query": None, "errors": []})
    monkeypatch.setattr(
        mcp_server, "query_sparql", lambda q, url, **kw: executed.append(url) or {"results": {"bindings": []}}
    )
    tools = _tools("b")

    asyncio.run(tools["search_sparql_docs"].fn("Who is Weber?", [], []))
    asyncio.run(tools["execute_sparql_query"].fn("SELECT * WHERE { ?s ?p ?o }", A["endpoint_url"]))

    assert searched and set(searched) == {"b__docs"}
    assert executed == [B["endpoint_url"]]
    assert "beta things" in tools["search_sparql_docs"].description


class _FakeApps:
    """Stands in for the per-dataset FastMCP apps: each answers with its dataset id."""

    def __init__(self):
        self.started: list[str] = []

    def __call__(self, project_id: str):
        async def app(scope, receive, send):
            route = scope["path"][len(scope.get("root_path", "")) :] or "/"
            await PlainTextResponse(f"{project_id} {route}")(scope, receive, send)

        started = self.started

        class Running:
            async def __aenter__(self):
                started.append(project_id)

            async def __aexit__(self, *exc):
                return False

        return app, Running()


def _client(dispatcher: "mcp_server.McpDispatcher") -> TestClient:
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with dispatcher.running():
            yield

    # localhost, as the tools agent calls it: FastMCP refuses unknown Host headers.
    return TestClient(Starlette(routes=[Mount("/mcp", app=dispatcher)], lifespan=lifespan), base_url="http://localhost:8000")


@pytest.fixture
def dispatcher_client(two):
    apps = _FakeApps()
    with _client(mcp_server.McpDispatcher(app_factory=apps)) as client:
        yield client, apps


def test_bare_mcp_is_the_first_dataset(dispatcher_client):
    client, apps = dispatcher_client

    assert client.post("/mcp/").text == "a /"
    assert client.post("/mcp/a/").text == "a /"
    assert client.post("/mcp/ghost/").status_code == 404
    assert apps.started == ["a"]


def test_unpublished_dataset_refuses_external_mcp(dispatcher_client):
    client, _ = dispatcher_client

    refused = client.post("/mcp/b/")
    assert refused.status_code == 403
    assert refused.json()["error"] == "This dataset is not published."
    assert client.post("/mcp/b/", headers={"X-Internal-Token": mcp_server.INTERNAL_TOKEN}).text == "b /"
    assert client.post("/mcp/b/", headers={"X-Internal-Token": "guess"}).status_code == 403


def test_tools_agent_uses_the_selected_datasets_address(two, monkeypatch: pytest.MonkeyPatch):
    from sparql_llm.agent.nodes import call_model as node
    from sparql_llm.agent.state import State

    seen: list[dict] = []

    class Recorder:
        def __init__(self, connections):
            seen.append(connections)

        async def get_tools(self):
            raise ConnectionError("stop here")

    monkeypatch.setattr(node, "MultiServerMCPClient", Recorder)
    config = RunnableConfig(configurable={"project": "b", "use_tools": True})
    with pytest.raises(RuntimeError):
        asyncio.run(node.call_model(State(messages=[HumanMessage(content="Who?")]), config))

    connection = next(iter(seen[0].values()))
    assert connection["url"].endswith("/mcp/b")
    assert connection["headers"]["X-Internal-Token"] == mcp_server.INTERNAL_TOKEN


def test_real_mcp_apps_answer_through_the_dispatcher(two):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with _client(mcp_server.McpDispatcher()) as client:
        bare = client.post("/mcp/", json=rpc, headers=headers)
        beta = client.post("/mcp/b/", json=rpc, headers={**headers, "X-Internal-Token": mcp_server.INTERNAL_TOKEN})

    for response in (bare, beta):
        assert response.status_code == 200, response.text
        assert "search_sparql_docs" in {t["name"] for t in response.json()["result"]["tools"]}


def test_tool_calls_run_on_the_selected_datasets_address(two, monkeypatch: pytest.MonkeyPatch):
    """Found in the acceptance run: tools were listed on /mcp/<id> but executed on bare /mcp (the first dataset)."""
    from langchain_core.messages import AIMessage

    from sparql_llm.agent.nodes import mcp_tools as node
    from sparql_llm.agent.state import State

    seen: list[dict] = []

    class Recorder:
        def __init__(self, connections):
            seen.append(connections)

        def session(self, name):
            raise ConnectionError("stop here")

    monkeypatch.setattr(node, "MultiServerMCPClient", Recorder)
    call = AIMessage(content="", tool_calls=[{"name": "search_sparql_docs", "args": {}, "id": "t1"}])
    config = RunnableConfig(configurable={"project": "b", "use_tools": True})
    with pytest.raises(ConnectionError):
        asyncio.run(node.mcp_tools_node(State(messages=[call]), config))

    connection = next(iter(seen[0].values()))
    assert connection["url"].endswith("/mcp/b")
    assert connection["headers"]["X-Internal-Token"] == mcp_server.INTERNAL_TOKEN


def test_endpoint_list_tool_shows_only_its_own_dataset(two):
    """Final review: the tool listed every served dataset, unpublished ones too, on every address."""
    for project_id, own, other in (("a", A, B), ("b", B, A)):
        listing = _tools(project_id)["list_available_endpoints"].fn()
        assert own["endpoint_url"] in listing and own["label"] in listing
        assert other["endpoint_url"] not in listing and other["label"] not in listing
