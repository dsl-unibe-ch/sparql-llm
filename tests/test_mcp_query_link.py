"""MCP tools mode offers the query it ran under the answer, like the pipeline's "Run or edit the query"."""

from sparql_llm.agent.nodes.mcp_tools import executed_query
from sparql_llm.mcp_server import EMPTY_RESULT_PROMPT, FIXED_QUERY_HEADING, QUERY_RESULTS_HEADING

QUERY = "SELECT (COUNT(?p) AS ?n) WHERE { ?p a <http://www.cidoc-crm.org/cidoc-crm/E21> }"


def call(query: str = QUERY, endpoint_url: str = "", name: str = "execute_sparql_query") -> dict:
    return {"name": name, "args": {"sparql_query": query, "endpoint_url": endpoint_url}, "id": "1"}


def test_query_with_rows_is_linked_on_the_dataset_endpoint(swiss_project):
    content = f'{QUERY_RESULTS_HEADING} {swiss_project.endpoint_url}:\n```\n{{"n": 58729}}\n```'
    link = executed_query(call(endpoint_url="https://elsewhere.example/sparql"), content, swiss_project)
    assert link == {"sparql_query": QUERY, "sparql_endpoint_url": swiss_project.endpoint_url}


def test_query_with_empty_result_is_linked(swiss_project):
    content = f"{EMPTY_RESULT_PROMPT}\n```sparql\n{QUERY}\n```"
    assert executed_query(call(), content, swiss_project)["sparql_query"] == QUERY


def test_fixed_query_is_the_one_linked(swiss_project):
    fixed = "PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>\n" + QUERY
    content = f"{FIXED_QUERY_HEADING}\n```sparql\n{fixed}\n```\n{QUERY_RESULTS_HEADING} x:\n```\n{{}}\n```"
    assert executed_query(call(), content, swiss_project)["sparql_query"] == fixed


def test_refused_or_failed_query_is_not_linked(swiss_project):
    refused = "The query generated in the original response is not valid according to the endpoints schema."
    failed = "SPARQL query returned error: timeout."
    assert executed_query(call(), refused, swiss_project) is None
    assert executed_query(call(), failed, swiss_project) is None


def test_other_tools_are_not_linked(swiss_project):
    content = f"{QUERY_RESULTS_HEADING} x:\n```\n{{}}\n```"
    assert executed_query(call(name="validate_sparql_query"), content, swiss_project) is None
