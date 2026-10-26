"""No query, and no schema lookup, ever reaches an endpoint the project did not configure (spec 6.4).

The real validator runs here; only the network boundary is recorded.
"""

import asyncio
import importlib

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm import mcp_server, utils
from sparql_llm.agent.nodes import validation
from sparql_llm.agent.state import State

validator = importlib.import_module("sparql_llm.validate_sparql")

NORSSIT = "https://ldf.fi/norssit/sparql"
FOREIGN = "https://unconfigured.invalid/sparql"
SCHEMA = {NORSSIT: {"http://xmlns.com/foaf/0.1/Person": {"http://www.w3.org/2004/02/skos/core#prefLabel": []}}}
PREFIXES = {"foaf": "http://xmlns.com/foaf/0.1/", "skos": "http://www.w3.org/2004/02/skos/core#"}


@pytest.fixture
def network(norssit_project, monkeypatch: pytest.MonkeyPatch):
    """Every URL the code tries to reach: schema lookups and query executions."""
    reached: list[str] = []

    def schema(url, void_file=None):
        reached.append(url)
        return {}

    def query(q, url, **kwargs):
        reached.append(url)
        return {"results": {"bindings": [{"n": {"value": "1"}}]}}

    monkeypatch.setattr(utils, "get_schema_for_endpoint", schema)
    monkeypatch.setattr(validation, "query_sparql", query)
    monkeypatch.setattr(mcp_server, "query_sparql", query)
    monkeypatch.setattr(validation.endpoints_metadata, "_prefixes_map", PREFIXES)
    monkeypatch.setattr(validation.endpoints_metadata, "_void_dict", SCHEMA)
    monkeypatch.setattr(validation.endpoints_metadata, "_initialized", True)
    return reached


def _pipeline(query: str, endpoint: str) -> dict:
    message = AIMessage(content=f"```sparql\n#+ endpoint: {endpoint}\n{query}\n```")
    return asyncio.run(validation.validate_output(State(messages=[message]), RunnableConfig(configurable={})))


PERSONS = "PREFIX foaf: <http://xmlns.com/foaf/0.1/>\nSELECT ?p WHERE { ?p a foaf:Person }"


def test_an_unknown_outer_endpoint_is_never_contacted(network):
    _pipeline(PERSONS, FOREIGN)
    assert FOREIGN not in network
    assert network == [NORSSIT]  # substituted: the one configured endpoint


@pytest.mark.parametrize(
    "service",
    [
        f"SERVICE <{FOREIGN}> {{ ?p ?x ?y }}",
        "SERVICE ?where { ?p ?x ?y }",
        f"OPTIONAL {{ SERVICE SILENT <{FOREIGN}> {{ ?p ?x ?y }} }}",
    ],
)
def test_service_clauses_are_refused_in_pipeline_mode(network, service):
    result = _pipeline(f"PREFIX foaf: <http://xmlns.com/foaf/0.1/>\nSELECT ?p WHERE {{ ?p a foaf:Person . {service} }}", NORSSIT)
    assert network == []  # neither a schema lookup nor an execution
    assert result["passed_validation"] is False
    assert "SERVICE" in result["messages"][0].content


def test_service_clauses_are_refused_in_tools_mode(network):
    tools = {t.name: t for t in mcp_server.get_mcp_app()._tool_manager.list_tools()}
    query = f"SELECT ?p WHERE {{ SERVICE <{FOREIGN}> {{ ?p ?x ?y }} }}"
    text = tools["execute_sparql_query"].fn(query, NORSSIT)
    assert network == []
    assert "SERVICE" in text


def test_the_word_service_in_a_literal_is_fine(network):
    query = (
        "PREFIX foaf: <http://xmlns.com/foaf/0.1/>\nPREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        'SELECT ?p WHERE { ?p a foaf:Person ; skos:prefLabel ?l . FILTER(CONTAINS(?l, "civil service")) }  # no SERVICE here'
    )
    assert validator.validate_sparql(query, NORSSIT, PREFIXES, SCHEMA)["errors"] == []


def test_a_hash_inside_an_iri_does_not_hide_a_service_clause():
    query = f"SELECT ?l WHERE {{ ?p <http://www.w3.org/2004/02/skos/core#prefLabel> ?l . SERVICE <{FOREIGN}> {{ ?p ?x ?y }} }}"
    assert validator.uses_service(query)
