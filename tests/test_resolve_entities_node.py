"""Offline tests for the resolve_entities node and the retrieve node's question."""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import sparql_llm.agent.nodes.retrieval_entities as node_module
from sparql_llm.agent.nodes.retrieval_entities import resolve_entities
from sparql_llm.agent.state import State, StructuredQuestion
from sparql_llm.agent.utils import latest_user_question
from sparql_llm.entity_resolver import EntityResolver

ROWS = [
    {"uri": "https://e/p1", "label": "Brenner, Ernst", "type": "Person", "birth": "1856", "death": "1911"},
    {"uri": "https://e/w1", "label": "Weber, Heinrich", "type": "Person", "birth": "1810"},
    {"uri": "https://e/w2", "label": "Weber, Heinrich", "type": "Person", "birth": "1870"},
]
CONFIG = {"configurable": {}}


def run(state, config):
    return asyncio.run(resolve_entities(state, config))


@pytest.fixture(autouse=True)
def fixture_resolver(tmp_path: Path, monkeypatch):
    path = tmp_path / "entity_names.json"
    path.write_text(json.dumps({"entities": ROWS}), encoding="utf-8")
    monkeypatch.setattr(node_module, "get_resolver", lambda: EntityResolver(path))


def _state(*entities: str) -> State:
    return State(
        messages=[HumanMessage(content="Who was Ernst Brenner married to?")],
        structured_question=StructuredQuestion(extracted_entities=list(entities)),
    )


def test_resolved_uris_reach_the_model():
    out = run(_state("Ernst Brenner"), CONFIG)
    msg = out["messages"][0]
    assert msg.name == "resolve_entities"
    assert "<https://e/p1>" in msg.content
    assert "Brenner, Ernst" in msg.content and "1856–1911" in msg.content


def test_entities_message_contains_rules():
    content = (run(_state("Heinrich Weber"), CONFIG))["messages"][0].content
    assert "VALUES" in content
    assert "do not guess" in content.lower()
    assert "does not denote" in content.lower()
    assert "label filter" in content.lower()


def test_unmatched_name_tells_the_model_to_fall_back():
    content = (run(_state("Xyzzy Quux"), CONFIG))["messages"][0].content
    assert '"Xyzzy Quux": no match' in content


def test_nothing_is_added_without_entities():
    assert run(_state(), CONFIG) == {}


def test_disabled_flag_adds_nothing():
    assert run(_state("Ernst Brenner"), {"configurable": {"enable_entities_resolution": False}}) == {}


def test_missing_entity_file_adds_only_a_step(tmp_path, monkeypatch):
    monkeypatch.setattr(node_module, "get_resolver", lambda: EntityResolver(tmp_path / "absent.json"))
    out = run(_state("Ernst Brenner"), CONFIG)
    assert "messages" not in out
    assert "not built" in out["steps"][0].label


def test_retrieve_question_ignores_node_messages():
    messages = [
        HumanMessage(content="first question"),
        AIMessage(content="first answer"),
        HumanMessage(content="Who was Ernst Brenner married to?"),
        HumanMessage(content="--- ENTITIES FOUND ---", name="resolve_entities"),
    ]
    assert latest_user_question(messages) == "Who was Ernst Brenner married to?"


def test_extraction_no_longer_asks_for_dates():
    from sparql_llm.agent.prompts import EXTRACTION_PROMPT

    line = next(l for l in EXTRACTION_PROMPT.splitlines() if '"extracted_entities"' in l)
    assert "dates" not in line


def test_pipeline_prompt_prefers_resolved_uris():
    from sparql_llm.agent.prompts import RESOLUTION_PROMPT

    assert "ENTITIES FOUND" in RESOLUTION_PROMPT
    assert "Surname, Firstname" in RESOLUTION_PROMPT


def test_tools_prompt_resolves_names_first():
    from sparql_llm.agent.prompts import TOOLS_RESOLUTION_PROMPT

    assert "resolve_entity_uri" in TOOLS_RESOLUTION_PROMPT
