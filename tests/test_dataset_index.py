"""Each dataset has its own search index, and retrieval reads only the selected dataset's."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm.agent.nodes import retrieval_docs
from sparql_llm.agent.state import State, StructuredQuestion
from sparql_llm.config import settings
from sparql_llm.indexing import rebuild

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}
B = {"id": "b", "label": "Beta", "endpoint_url": "https://b.example/sparql"}


def _aliases(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(rebuild, "_aliases", lambda: set(names))


def test_alias_names_cannot_collide(use_projects):
    a, a_v2 = use_projects(A, {**A, "id": "a-v2", "endpoint_url": "https://a2.example/sparql"})

    assert rebuild.alias_for(a) == "a__docs"
    assert rebuild.alias_for(a_v2) == "a-v2__docs"
    a_version = rebuild.versioned_collection(a, "20261026-120000")
    a_v2_version = rebuild.versioned_collection(a_v2, "20261026-120000")
    assert a_version == "a__v20261026-120000"
    # One dataset's cleanup never matches the other's collections, nor the alias names.
    assert not a_v2_version.startswith(rebuild.version_prefix(a))
    assert not a_version.startswith(rebuild.version_prefix(a_v2))
    assert not rebuild.alias_for(a).startswith(rebuild.version_prefix(a))


def test_retrieval_queries_only_the_selected_datasets_collection(use_projects, monkeypatch: pytest.MonkeyPatch):
    use_projects(A, B)
    _aliases(monkeypatch, "a__docs", "b__docs")
    queried: list[str] = []

    def query_points(collection_name, **kwargs):
        queried.append(collection_name)
        return SimpleNamespace(points=[])

    monkeypatch.setattr(retrieval_docs, "qdrant_client", SimpleNamespace(query_points=query_points))
    monkeypatch.setattr(retrieval_docs, "embedding_model", SimpleNamespace(embed_query=lambda texts: [[0.0] for _ in texts]))
    state = State(messages=[HumanMessage(content="Who is Weber?")], structured_question=StructuredQuestion(intent="general_information"))

    asyncio.run(retrieval_docs.retrieve(state, RunnableConfig(configurable={"project": "b"})))

    assert queried and set(queried) == {"b__docs"}


def test_legacy_alias_serves_the_first_dataset_until_its_rebuild(use_projects, monkeypatch: pytest.MonkeyPatch):
    swiss, norssit = use_projects(
        {"id": "swiss-elites", "label": "Swiss", "endpoint_url": "https://s.example/sparql"},
        {"id": "norssit", "label": "Norssit", "endpoint_url": "https://n.example/sparql"},
    )
    _aliases(monkeypatch, settings.docs_collection_name)
    assert rebuild.collection_for(swiss) == settings.docs_collection_name
    assert rebuild.collection_for(norssit) == "norssit__docs"

    _aliases(monkeypatch, settings.docs_collection_name, "swiss-elites__docs")
    assert rebuild.collection_for(swiss) == "swiss-elites__docs"
