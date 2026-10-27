"""A query the model writes is validated and run, however it labels the fence or names the endpoint."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm.agent.nodes import validation
from sparql_llm.agent.state import State

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}
QUERY = "SELECT ?s WHERE { ?s ?p ?o }"


@pytest.fixture
def executed(use_projects, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    use_projects(A)
    urls: list[str] = []
    monkeypatch.setattr(validation, "endpoints_metadata", SimpleNamespace(prefixes_for=lambda p: {}, schema_for=lambda p: {}))
    monkeypatch.setattr(
        validation, "query_sparql", lambda q, url, **kw: urls.append(url) or {"results": {"bindings": [{"s": {"value": "x"}}]}}
    )
    return urls


def _validate(content: str) -> dict:
    return asyncio.run(validation.validate_output(State(messages=[AIMessage(content=content)]), RunnableConfig(configurable={})))


@pytest.mark.parametrize("label", ["SPARQL", "Sparql"])
def test_an_uppercase_fence_is_still_run(executed: list[str], label: str):
    out = _validate(f"```{label}\n#+ endpoint: {A['endpoint_url']}\n{QUERY}\n```")
    assert executed == [A["endpoint_url"]]
    assert out["structured_output"]["sparql_query"].endswith(QUERY)


def test_a_query_without_an_endpoint_comment_runs_on_the_selected_endpoint(executed: list[str]):
    out = _validate(f"```sparql\n{QUERY}\n```")
    assert executed == [A["endpoint_url"]]
    assert out["structured_output"]["sparql_endpoint_url"] == A["endpoint_url"]
