"""The retrieval index is built per project, from that project's files and settings."""

import pytest

from sparql_llm.indexing import index_resources
from sparql_llm.indexing.index_resources import build_project_docs, dataset_description_docs, general_info_docs
from sparql_llm.loaders.sparql_examples_md_loader import SparqlExamplesMdLoader
from sparql_llm.loaders.sparql_void_shapes_loader import class_labels_query


def test_label_query_accepts_rdfs_class_and_preflabel():
    query = class_labels_query(["http://xmlns.com/foaf/0.1/Person"])
    assert "owl:Class" not in query
    assert "skos:prefLabel" in query and "rdfs:label" in query
    assert "<http://xmlns.com/foaf/0.1/Person>" in query
    assert 'LANGMATCHES(LANG(?label), "en")' in query
    assert "ORDER BY" in query


def test_prefix_repairs_come_from_project(tmp_path):
    examples = tmp_path / "examples.md"
    examples.write_text(
        "## Example 1: Members\n\nQuestion: Who?\n\n```sparql\nSELECT * WHERE { ?m sdh-so:P1 ?p }\n```\n",
        encoding="utf-8",
    )
    docs = SparqlExamplesMdLoader(examples, "https://x/sparql", prefix_repairs={"sdh-so:": "sdh-short:"}).load()
    assert "sdh-short:P1" in docs[0].metadata["answer"]
    untouched = SparqlExamplesMdLoader(examples, "https://x/sparql").load()
    assert "sdh-so:P1" in untouched[0].metadata["answer"]


def test_dataset_sections_become_general_info_docs(norssit_project):
    docs = dataset_description_docs(norssit_project)
    assert [d.metadata["question"] for d in docs] == ["Prefixes", "Names and dates"]
    assert "skos:prefLabel" in docs[1].metadata["answer"]
    assert all(d.metadata["doc_type"] == "General information" for d in docs)
    assert all(d.metadata["iri"] == "http://www.ldf.fi/dataset/norssit" for d in docs)
    assert all(d.metadata["endpoint_url"] == "https://ldf.fi/norssit/sparql" for d in docs)


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(index_resources, "load_schemaorg_description", lambda endpoint: [])


def test_ontology_docs_only_when_enabled(norssit_project, offline, monkeypatch: pytest.MonkeyPatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("ontology fetched for a project that did not ask for it")

    monkeypatch.setattr(index_resources, "fetch_ontology_terms", must_not_run)
    monkeypatch.setattr(index_resources, "fetch_shacl_shapes", must_not_run)
    docs = build_project_docs(norssit_project)
    examples = [d for d in docs if d.metadata["doc_type"] == "SPARQL endpoints query examples"]
    assert len(examples) >= 6
    assert any(d.metadata["question"] == "Names and dates" for d in docs)


def test_general_info_iri_is_homepage(norssit_project):
    docs = general_info_docs()
    assert docs[0].metadata["iri"] == "http://www.ldf.fi/dataset/norssit"
    assert "Norssit (https://ldf.fi/norssit/sparql)" in docs[0].metadata["answer"]


def test_synced_examples_only_when_curator_sync_is_configured(norssit_project, offline):
    norssit_project.synced_examples_path.write_text(
        "## Example S1: Stale\n\nQuestion: A stale synced question?\n\n```sparql\nSELECT * WHERE { ?s ?p ?o }\n```\n",
        encoding="utf-8",
    )
    docs = build_project_docs(norssit_project)
    assert not any(d.metadata["question"] == "A stale synced question?" for d in docs)
