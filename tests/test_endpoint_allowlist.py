"""Queries run only on the selected dataset's endpoint, in pipeline and tools mode (F2)."""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm.agent.nodes import validation
from sparql_llm.agent.state import State
from sparql_llm.project import allowed_endpoint

NORSSIT = "https://ldf.fi/norssit/sparql"
OTHER = "https://sparql.example.org/sparql"


def test_configured_endpoint_is_kept(norssit_project):
    assert allowed_endpoint(NORSSIT, norssit_project) == (NORSSIT, "")


def test_unknown_endpoint_is_replaced_by_the_datasets_own(norssit_project):
    url, note = allowed_endpoint("https://swiss-elites.example/sparql", norssit_project)
    assert url == NORSSIT
    assert note == f"Ran on {NORSSIT}, the endpoint of Norssit, instead of https://swiss-elites.example/sparql."


def test_a_missing_endpoint_is_filled(norssit_project):
    url, note = allowed_endpoint(None, norssit_project)
    assert url == NORSSIT
    assert NORSSIT in note


def test_another_configured_datasets_endpoint_is_replaced(use_projects):
    norssit, _ = use_projects(
        {"id": "norssit", "label": "Norssit", "endpoint_url": NORSSIT},
        {"id": "other", "label": "Other", "endpoint_url": OTHER},
    )
    url, note = allowed_endpoint(OTHER, norssit)
    assert url == NORSSIT
    assert f"instead of {OTHER}" in note


def test_validation_never_queries_unconfigured_url(norssit_project, monkeypatch: pytest.MonkeyPatch):
    queried: list[str] = []

    def fake_query(query, endpoint_url, **kwargs):
        queried.append(endpoint_url)
        return {"results": {"bindings": [{"n": {"value": "10137"}}]}}

    monkeypatch.setattr(validation, "query_sparql", fake_query)
    monkeypatch.setattr(validation, "validate_sparql_in_msg", lambda msg, prefixes, schema: [
        {"original_query": "SELECT * WHERE { ?s ?p ?o }", "fixed_query": None, "errors": [],
         "endpoint_url": "https://evil.example/sparql"}
    ])
    message = AIMessage(content="```sparql\n#+ endpoint: https://evil.example/sparql\nSELECT * WHERE { ?s ?p ?o }\n```")
    result = asyncio.run(validation.validate_output(State(messages=[message]), RunnableConfig(configurable={})))
    assert queried == [NORSSIT]
    assert any("instead of https://evil.example/sparql" in step.details for step in result["steps"])


def test_tools_mode_never_runs_on_another_endpoint(use_projects):
    from sparql_llm.mcp_server import resolve_endpoint_url

    norssit, other = use_projects(
        {"id": "norssit", "label": "Norssit", "endpoint_url": NORSSIT},
        {"id": "other", "label": "Other", "endpoint_url": OTHER},
    )
    assert resolve_endpoint_url("https://evil.example/sparql", norssit)[0] == NORSSIT
    assert resolve_endpoint_url(NORSSIT, other)[0] == OTHER
