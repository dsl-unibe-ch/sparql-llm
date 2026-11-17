"""MCP tools mode offers the query it ran under the answer, like the pipeline's "Run or edit the query"."""

from typing import Any

from langchain_core.messages import AIMessage

from sparql_llm.agent.nodes.mcp_tools import executed_query, log_query_link_mismatch, show_query_that_ran
from sparql_llm.agent.state import State
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


# The answer's ```sparql block against the query behind "Run or edit the query"
# (curators' report 2026-11-17: gpt-oss ran one query and printed a trimmed copy).
RAN = """PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>
SELECT ?person ?titleLabel WHERE {
  ?event sdh-sls:P9 ?person .
  OPTIONAL { ?event sdh-sls:P10 ?title . ?title sdh-short:P9 ?titleLabel }
}
LIMIT 100"""
TITLE_OPTIONAL = "  OPTIONAL { ?event sdh-sls:P10 ?title . ?title sdh-short:P9 ?titleLabel }\n"


def answer(query: str) -> str:
    return f"Here are the persons.\n\n```sparql\n#+ endpoint: https://example.org/sparql\n{query}\n```\n"


def test_answer_query_matching_the_link_is_a_match():
    # Only the endpoint line and the indentation differ.
    assert log_query_link_mismatch(answer(RAN.replace("\n  ", "\n    ")), {"sparql_query": RAN}) is True


def test_answer_query_differing_from_the_link_is_a_mismatch():
    assert log_query_link_mismatch(answer(RAN.replace(TITLE_OPTIONAL, "")), {"sparql_query": RAN}) is False


def final_state(content: str, link: Any, **msg: Any) -> State:
    return State(messages=[AIMessage(content=content, id="answer-1", **msg)], structured_output=link)


def test_an_answer_printing_another_query_shows_the_one_that_ran():
    trimmed = RAN.replace(TITLE_OPTIONAL, "")
    content = f"Intro.\n\n```sparql\n#+ endpoint: https://example.org/sparql\n{trimmed}\n```\n\nOutro."
    link = {"sparql_query": RAN, "sparql_endpoint_url": "https://swiss.example/sparql"}

    update = show_query_that_ran(final_state(content, link))

    fixed = update["messages"][0]
    assert fixed.id == "answer-1"  # replaces the answer in the state, not appended
    assert fixed.content == f"Intro.\n\n```sparql\n#+ endpoint: https://swiss.example/sparql\n{RAN}\n```\n\nOutro."
    assert update["steps"][0].type == "fix-message"
    assert update["steps"][0].fixed_message == fixed.content


def test_only_the_last_query_of_the_answer_is_replaced():
    first = "```sparql\nSELECT ?x WHERE { ?x ?p ?o }\n```"
    content = f"{first}\n\nThen:\n\n```sparql\n{RAN.replace(TITLE_OPTIONAL, '')}\n```"
    fixed = show_query_that_ran(final_state(content, {"sparql_query": RAN}))["messages"][0].content
    assert fixed.startswith(first)
    assert fixed.endswith(f"```sparql\n{RAN}\n```")


def test_an_answer_printing_the_query_that_ran_is_left_alone():
    assert show_query_that_ran(final_state(answer(RAN), {"sparql_query": RAN})) == {}
    assert show_query_that_ran(final_state("No query here.", {"sparql_query": RAN})) == {}
    assert show_query_that_ran(final_state(answer(RAN), [])) == {}


def test_nothing_to_compare_without_a_link_or_a_printed_query():
    assert log_query_link_mismatch("No query here.", []) is None
    assert log_query_link_mismatch(answer(RAN), []) is None
    assert log_query_link_mismatch("No query here.", {"sparql_query": RAN}) is None
