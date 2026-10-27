"""A request on dataset A never uses anything of dataset B: prompt, names, prefixes, endpoint or method.

Both datasets declare ``schema:`` and ``voc:`` (to different namespaces) and both hold a "Weber, Anna".
The validator test uses ``voc:``: rdflib binds ``schema:`` itself, so it is never reported missing.
"""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm.agent.nodes import validation
from sparql_llm.agent.state import State

A_URL = "https://a.example/sparql"
B_URL = "https://b.example/sparql"
A = {
    "id": "a",
    "label": "Alpha",
    "endpoint_url": A_URL,
    "method": "POST",
    "prefixes": {"schema": "http://schema.org/", "voc": "http://a.example/voc/"},
    "entities": {"label_property": "http://schema.org/name", "types": {"Person": {"class": "http://schema.org/Person"}}},
    "topics": "alpha topics",
}
B = {
    "id": "b",
    "label": "Beta",
    "endpoint_url": B_URL,
    "method": "GET",
    "prefixes": {"schema": "https://schema.org/", "voc": "http://b.example/voc/"},
    "entities": {"label_property": "https://schema.org/name", "types": {"Person": {"class": "https://schema.org/Person"}}},
    "topics": "beta topics",
}


@pytest.fixture
def two(use_projects, tmp_path: Path):
    projects = {p.id: p for p in use_projects(A, B)}
    for pid, spec in (("a", A), ("b", B)):
        folder = tmp_path / pid
        (folder / "dataset.md").write_text(f"## Rules of {pid}\n\nOnly in dataset {pid}.\n", encoding="utf-8")
        (folder / "entity_names.json").write_text(
            json.dumps({"entities": [{"uri": f"https://{pid}.example/person/1", "label": "Weber, Anna", "type": "Person"}]}),
            encoding="utf-8",
        )
        (folder / "prefixes.json").write_text(
            json.dumps({"prefixes_map": spec["prefixes"], "classes_schema": {spec["endpoint_url"]: {}}}), encoding="utf-8"
        )
    from sparql_llm.indexing.index_resources import endpoints_metadata

    endpoints_metadata.reload()
    return projects


def _config(project: str, **extra) -> RunnableConfig:
    return RunnableConfig(configurable={"project": project, **extra})


def test_prompt_mentions_only_the_selected_dataset(two):
    from sparql_llm.agent.prompts import extraction_prompt, resolution_prompt, tools_resolution_prompt

    for prompt in (resolution_prompt(two["a"]), tools_resolution_prompt(two["a"])):
        assert "Only in dataset a." in prompt and A_URL in prompt and "alpha topics" in prompt
        assert "Only in dataset b." not in prompt and B_URL not in prompt and "beta" not in prompt.lower()
    extraction = extraction_prompt(two["a"])
    assert "alpha topics" in extraction and "https://schema.org" not in extraction and "Beta" not in extraction


def test_resolver_returns_only_the_selected_datasets_entities(two):
    from sparql_llm.entity_resolver import describe_for_tool, get_resolver

    resolution = get_resolver(two["a"]).resolve("Anna Weber")
    assert [c.uri for c in resolution.candidates] == ["https://a.example/person/1"]
    assert "https://b.example/person/1" not in describe_for_tool(get_resolver(two["a"]), "Anna Weber", two["a"])
    assert [c.uri for c in get_resolver(two["b"]).resolve("Anna Weber").candidates] == ["https://b.example/person/1"]


def test_validator_uses_the_selected_datasets_prefixes(two):
    message = AIMessage(content=f"```sparql\n#+ endpoint: {B_URL}\nSELECT ?s WHERE {{ ?s a voc:Person }}\n```")
    result = asyncio.run(
        validation.validate_output(State(messages=[message]), _config("b", enable_sparql_execution=False))
    )
    query = result["structured_output"]["sparql_query"]
    assert "PREFIX voc: <http://b.example/voc/>" in query
    assert "a.example" not in query


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, bool]] = []

    def fake_query(query, endpoint_url, **kwargs):
        calls.append((endpoint_url, kwargs.get("post", False)))
        return {"results": {"bindings": [{"n": {"value": "1"}}]}}

    monkeypatch.setattr(validation, "query_sparql", fake_query)
    return calls


def test_endpoint_line_naming_the_other_dataset_is_not_followed(two, executed):
    message = AIMessage(content=f"```sparql\n#+ endpoint: {B_URL}\nSELECT * WHERE {{ ?s ?p ?o }}\n```")
    result = asyncio.run(validation.validate_output(State(messages=[message]), _config("a")))

    assert [url for url, _ in executed] == [A_URL]
    assert result["structured_output"]["sparql_endpoint_url"] == A_URL
    assert any(f"the endpoint of Alpha, instead of {B_URL}" in step.details for step in result["steps"])


def test_execution_uses_the_selected_datasets_method(two, executed):
    message = AIMessage(content=f"```sparql\n#+ endpoint: {A_URL}\nSELECT * WHERE {{ ?s ?p ?o }}\n```")
    asyncio.run(validation.validate_output(State(messages=[message]), _config("a")))
    message = AIMessage(content=f"```sparql\n#+ endpoint: {B_URL}\nSELECT * WHERE {{ ?s ?p ?o }}\n```")
    asyncio.run(validation.validate_output(State(messages=[message]), _config("b")))

    assert executed == [(A_URL, True), (B_URL, False)]


def test_missing_schema_prefix_takes_the_selected_datasets_namespace(two):
    """GPT review I2: rdflib binds schema: to https itself, so an http dataset never got its own namespace."""
    message = AIMessage(content=f"```sparql\n#+ endpoint: {A_URL}\nSELECT ?s WHERE {{ ?s a schema:Person }}\n```")
    on_a = asyncio.run(validation.validate_output(State(messages=[message]), _config("a", enable_sparql_execution=False)))
    message = AIMessage(content=f"```sparql\n#+ endpoint: {B_URL}\nSELECT ?s WHERE {{ ?s a schema:Person }}\n```")
    on_b = asyncio.run(validation.validate_output(State(messages=[message]), _config("b", enable_sparql_execution=False)))

    assert "PREFIX schema: <http://schema.org/>" in on_a["structured_output"]["sparql_query"]
    assert "PREFIX schema: <https://schema.org/>" in on_b["structured_output"]["sparql_query"]


def test_an_endpoint_comment_naming_another_dataset_does_not_skip_the_schema_check(two, executed, tmp_path: Path):
    """GPT review I3: the schema check looked up the commented endpoint, so naming B skipped A's schema."""
    from sparql_llm.indexing.index_resources import endpoints_metadata

    schema = {A_URL: {"http://a.example/voc/Person": {"http://a.example/voc/name": []}}}
    (tmp_path / "a" / "prefixes.json").write_text(
        json.dumps({"prefixes_map": A["prefixes"], "classes_schema": schema}), encoding="utf-8"
    )
    endpoints_metadata.reload()
    query = "SELECT ?s WHERE {{ ?s a voc:Nothing ; voc:nope ?x }}"
    message = AIMessage(content=f"```sparql\n#+ endpoint: {B_URL}\n{query}\n```".replace("{{", "{").replace("}}", "}"))

    result = asyncio.run(validation.validate_output(State(messages=[message]), _config("a")))

    assert result["passed_validation"] is False
    assert executed == []
