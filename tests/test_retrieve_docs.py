"""Offline tests for which documents the retrieve node hands the model."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from qdrant_client.models import ScoredPoint

import sparql_llm.agent.nodes.retrieval_docs as node_module
from sparql_llm.agent.nodes.retrieval_docs import retrieve
from sparql_llm.agent.state import State, StructuredQuestion

EXAMPLE = {"doc_type": "SPARQL endpoints query examples", "question": "How many marriages are recorded?", "answer": "SELECT (COUNT(?r) AS ?n) ..."}
SCHEMA = {"doc_type": "SPARQL endpoints classes schema", "question": "Social Relationship", "answer": "shape:C3 {}", "iri": "https://x/C3"}
GENERAL = {"doc_type": "General information", "question": "Which resources are supported?", "answer": "Swiss Elites ..."}


class FakeQdrant:
    """Serves one document per doc_type, honouring the must / must_not doc_type filters."""

    def query_points(self, query, collection_name, limit, query_filter=None):
        docs = [EXAMPLE, SCHEMA, GENERAL]
        if query_filter is not None:
            for cond in query_filter.must or []:
                docs = [d for d in docs if d["doc_type"] == cond.match.value]
            for cond in query_filter.must_not or []:
                docs = [d for d in docs if d["doc_type"] != cond.match.value]
        return SimpleNamespace(points=[ScoredPoint(id=i, version=0, score=0.9, payload=d) for i, d in enumerate(docs)])


class FakeEmbedding:
    def embed_query(self, texts, **kwargs):
        return [[0.0] for _ in texts]


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(node_module, "qdrant_client", FakeQdrant())
    monkeypatch.setattr(node_module, "embedding_model", FakeEmbedding())


def doc_types(intent: str) -> list[str]:
    state = State(
        messages=[HumanMessage(content="Combien de mariages sont enregistrés ?")],
        structured_question=StructuredQuestion(intent=intent),
    )
    out = asyncio.run(retrieve(state, {"configurable": {}}))
    return [s.label for s in out["steps"][0].substeps]


def test_counting_question_labelled_general_information_still_gets_examples():
    # The extraction model labels "how many …" as general_information; the query it then
    # writes needs the examples as much as any other query does.
    types = doc_types("general_information")
    assert "SPARQL endpoints query examples" in types
    assert "General information" in types
    assert "SPARQL endpoints classes schema" in types


def test_access_resources_retrieval_is_unchanged():
    assert set(doc_types("access_resources")) == {
        "SPARQL endpoints query examples",
        "SPARQL endpoints classes schema",
        "General information",
    }
