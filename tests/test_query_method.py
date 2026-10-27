"""Every query to a project's endpoint uses the project's method (GET or POST)."""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm import mcp_server
from sparql_llm.agent.nodes import validation
from sparql_llm.agent.state import State
from sparql_llm.indexing import sync_examples
from sparql_llm.indexing.sync_examples import Example
from sparql_llm.loaders import ontology_profiles_loader
from sparql_llm.project import post_for

URL = "https://post-only.example/sparql"


@pytest.fixture
def post_only(use_projects):
    return use_projects({"id": "posty", "label": "Posty", "endpoint_url": URL, "method": "POST"})[0]


def _recorder(calls):
    def fake(query, endpoint_url, **kwargs):
        calls.append(kwargs.get("post", False))
        return {"results": {"bindings": [{"n": {"value": "1"}}]}}

    return fake


def test_post_for_follows_the_project(post_only):
    assert post_for(post_only) is True
    assert post_for(post_only.model_copy(update={"method": "GET"})) is False


def test_pipeline_execution_uses_the_project_method(post_only, monkeypatch: pytest.MonkeyPatch):
    calls: list[bool] = []
    monkeypatch.setattr(validation, "query_sparql", _recorder(calls))
    monkeypatch.setattr(validation, "validate_sparql_in_msg", lambda *a, **kw: [
        {"original_query": "SELECT * WHERE { ?s ?p ?o }", "fixed_query": None, "errors": [], "endpoint_url": URL}
    ])
    message = AIMessage(content="```sparql\nSELECT * WHERE { ?s ?p ?o }\n```")
    asyncio.run(validation.validate_output(State(messages=[message]), RunnableConfig(configurable={})))
    assert calls == [True]


def test_tools_execution_uses_the_project_method(use_projects, monkeypatch: pytest.MonkeyPatch):
    use_projects({"id": "getty", "label": "Getty", "endpoint_url": URL, "method": "GET"})
    calls: list[bool] = []
    monkeypatch.setattr(mcp_server, "query_sparql", _recorder(calls))
    monkeypatch.setattr(mcp_server, "validate_sparql", lambda *a: {"fixed_query": None, "errors": []})
    tools = {t.name: t for t in mcp_server.get_mcp_app()._tool_manager.list_tools()}
    tools["execute_sparql_query"].fn("SELECT * WHERE { ?s ?p ?o }", URL)
    assert calls == [False]


def test_curator_examples_are_validated_with_the_project_method(post_only, monkeypatch: pytest.MonkeyPatch):
    calls: list[bool] = []
    monkeypatch.setattr(sync_examples, "query_sparql", _recorder(calls))
    sync_examples.validate([Example(title="t", questions=["q"], query="SELECT 1 {}")], URL, post=True)
    assert calls == [True]


def test_ontology_terms_use_the_project_method(monkeypatch: pytest.MonkeyPatch):
    calls: list[bool] = []
    monkeypatch.setattr(ontology_profiles_loader, "query_sparql", _recorder(calls))
    from sparql_llm.utils import get_prefix_converter

    ontology_profiles_loader.fetch_ontology_terms(URL, get_prefix_converter({}), post=False)
    ontology_profiles_loader.fetch_shacl_shapes(URL, get_prefix_converter({}), post=False)
    assert calls == [False, False]
