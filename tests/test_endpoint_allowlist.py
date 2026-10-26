"""Queries run only on configured endpoints, in pipeline and tools mode (F2)."""

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
    assert allowed_endpoint(NORSSIT) == (NORSSIT, "")


def test_single_project_substitutes_unknown_endpoint(norssit_project):
    url, note = allowed_endpoint("https://swiss-elites.example/sparql")
    assert url == NORSSIT
    assert note == f"Ran on {NORSSIT}, the configured endpoint, instead of https://swiss-elites.example/sparql."


def test_single_project_fills_a_missing_endpoint(norssit_project):
    url, note = allowed_endpoint(None)
    assert url == NORSSIT
    assert NORSSIT in note


def test_several_projects_reject_unknown_endpoint(use_projects):
    use_projects(
        {"id": "norssit", "label": "Norssit", "endpoint_url": NORSSIT},
        {"id": "other", "label": "Other", "endpoint_url": OTHER},
    )
    url, note = allowed_endpoint("https://evil.example/sparql")
    assert url is None
    assert note == f"Unknown endpoint https://evil.example/sparql. Use one of: {NORSSIT}, {OTHER}."


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


def test_tools_mode_rejects_unknown_endpoint_with_several_projects(use_projects):
    from sparql_llm.mcp_server import resolve_endpoint_url

    use_projects(
        {"id": "norssit", "label": "Norssit", "endpoint_url": NORSSIT},
        {"id": "other", "label": "Other", "endpoint_url": OTHER},
    )
    url, error = resolve_endpoint_url("https://evil.example/sparql")
    assert url is None
    assert "Use one of" in error
