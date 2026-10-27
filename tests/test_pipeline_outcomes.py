"""The pipeline tells a repair from an answer: results reach the model, and only repairs use the budget."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm.agent.graph import route_model_output
from sparql_llm.agent.nodes import validation
from sparql_llm.agent.state import State

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}
ANSWER = f"```sparql\n#+ endpoint: {A['endpoint_url']}\nSELECT ?s WHERE {{ ?s ?p ?o }}\n```"
ROWS = {"results": {"bindings": [{"s": {"value": "x"}}]}}
EMPTY = {"results": {"bindings": []}}


@pytest.fixture
def endpoint(use_projects, monkeypatch: pytest.MonkeyPatch):
    """Answers each query with the next prepared result, and records how many ran."""
    use_projects(A)
    monkeypatch.setattr(validation, "endpoints_metadata", SimpleNamespace(prefixes_for=lambda p: {}, schema_for=lambda p: {}))
    ran = SimpleNamespace(count=0, result=ROWS)

    def query(q, url, **kw):
        ran.count += 1
        return ran.result

    monkeypatch.setattr(validation, "query_sparql", query)
    return ran


def _config(max_tries: int = 3) -> RunnableConfig:
    return RunnableConfig(configurable={"max_try_fix_sparql": max_tries})


def _step(state: State, config: RunnableConfig) -> tuple[State, str]:
    """Run the validation node on ``state``, apply its update, and say where the graph goes next."""
    update = asyncio.run(validation.validate_output(state, config))
    fields = {k: v for k, v in update.items() if k not in ("messages", "steps", "structured_output")}
    new = State(messages=[*state.messages, *update.get("messages", [])], **{**vars_of(state), **fields})
    return new, route_model_output(new, config)


def vars_of(state: State) -> dict:
    return {"try_count": state.try_count, "passed_validation": state.passed_validation, "results_delivered": state.results_delivered}


def test_results_after_a_repair_reach_the_model_on_a_budget_of_one(endpoint):
    # One repair already used, budget one: the working query's rows must still be summarised.
    state = State(messages=[HumanMessage("q"), AIMessage(ANSWER)], try_count=1)
    state, route = _step(state, _config(max_tries=1))
    assert endpoint.count == 1
    assert route == "call_model"
    assert state.try_count == 1


def test_the_summary_after_results_is_the_answer_and_runs_nothing(endpoint):
    state = State(messages=[HumanMessage("q"), AIMessage(ANSWER)])
    state, route = _step(state, _config())
    assert route == "call_model"
    # The model's summary repeats the query: it is the answer, not a new query to run.
    state = State(messages=[*state.messages, AIMessage(f"Here is what I found.\n{ANSWER}")], **vars_of(state))
    state, route = _step(state, _config())
    assert endpoint.count == 1
    assert route == "__end__"


def test_an_empty_result_is_not_called_a_mistake(endpoint):
    endpoint.result = EMPTY
    state = State(messages=[HumanMessage("q"), AIMessage(ANSWER)])
    state, route = _step(state, _config())
    told = str(state.messages[-1].content)
    assert route == "call_model"
    assert "Please fix the query" not in told
    assert "can be the real answer" in told


def test_an_empty_result_on_the_last_try_is_reported_not_abandoned(endpoint):
    endpoint.result = EMPTY
    state = State(messages=[HumanMessage("q"), AIMessage(ANSWER)], try_count=1)
    state, route = _step(state, _config(max_tries=1))
    assert route == "call_model"
    assert "no results" in str(state.messages[-1].content).lower()
