"""VoID generation from a project's endpoint: valid Turtle, filtered classes, no Swiss constants."""

import pytest
import rdflib

from sparql_llm.indexing import void_gen
from sparql_llm.indexing.void_gen import VoidError, generate_void, keep_class, shorten
from sparql_llm.project import SchemaClasses
from sparql_llm.utils import get_schema_for_endpoint

NORSSIT_NS = "http://ldf.fi/norssit/"
PERSON = "http://xmlns.com/foaf/0.1/Person"
TITLE = "http://ldf.fi/norssit/titles/l%C3%A4%C3%A4k%C3%A4ri"
BROTHER = "http://ldf.fi/schema/bioc/Brother"
VOID_DATASET = "http://rdfs.org/ns/void#Dataset"


def _n(value) -> dict:
    return {"type": "literal", "value": str(value)}


def _u(value) -> dict:
    return {"type": "uri", "value": value}


CLASSES = [(PERSON, 10137), (BROTHER, 120), (TITLE, 60), (VOID_DATASET, 1), ("http://ldf.fi/norssit/Rare", 3)]
PROPS = [
    ("http://www.w3.org/1999/02/22-rdf-syntax-ns#type", 10421),
    ("http://www.w3.org/2004/02/skos/core#prefLabel", 10300),
    ("http://schema.org/birthDate", 9000),
]


def norssit_fetch(queries: list[str] | None = None):
    """Answers the generator's statistics queries with a Norssit-like sample."""

    def fetch(query, endpoint_url, **kwargs):
        if queries is not None:
            queries.append(query)
        if "GROUP BY ?c ?p" in query:
            rows = [{"c": _u(c), "p": _u(p), "n": _n(n)} for c, _ in CLASSES for p, n in PROPS[:2]]
        elif "GROUP BY ?c" in query:
            rows = [{"c": _u(c), "n": _n(n)} for c, n in CLASSES]
        elif "GROUP BY ?p" in query:
            rows = [{"p": _u(p), "n": _n(n)} for p, n in PROPS]
        elif "COUNT(DISTINCT ?s)" in query:
            rows = [{"n": _n(14265)}]
        else:
            rows = [{"n": _n(735000)}]
        return {"results": {"bindings": rows}}

    return fetch


def test_shorten_falls_back_for_slash_local():
    assert shorten("http://ldf.fi/norssit/titles/professori", {"norssit": NORSSIT_NS}) == (
        "<http://ldf.fi/norssit/titles/professori>"
    )


def test_shorten_keeps_percent_encoded_local():
    assert shorten("http://ldf.fi/norssit/titles/titles%20x", {"t": "http://ldf.fi/norssit/titles/"}) == "t:titles%20x"


def test_shorten_prefers_the_longest_namespace():
    prefixes = {"norssit": NORSSIT_NS, "t": "http://ldf.fi/norssit/titles/"}
    assert shorten("http://ldf.fi/norssit/titles/opettaja", prefixes) == "t:opettaja"


def test_service_description_classes_dropped():
    cfg = SchemaClasses()
    assert not keep_class(VOID_DATASET, 1000, cfg)
    assert not keep_class("http://www.w3.org/ns/sparql-service-description#Service", 1000, cfg)
    assert not keep_class("http://www.w3.org/ns/dcat#Dataset", 1000, cfg)
    assert keep_class(PERSON, 1000, cfg)


def test_min_instances_and_exclude():
    cfg = SchemaClasses(min_instances=50, exclude=["http://ldf.fi/norssit/titles/"])
    assert keep_class(PERSON, 10137, cfg)
    assert keep_class(BROTHER, 120, cfg)
    assert not keep_class(TITLE, 60, cfg)
    assert not keep_class("http://ldf.fi/norssit/Rare", 3, cfg)


def test_generated_file_parses(norssit_project):
    report = generate_void(norssit_project, fetch=norssit_fetch())
    graph = rdflib.Graph().parse(norssit_project.void_path, format="turtle")
    assert len(graph) > 0
    assert report["classes_total"] == 5
    assert report["classes_kept"] == 2  # Person and Brother; titles, void:Dataset and Rare dropped
    assert report["triples"] == 735000 and report["entities"] == 14265
    text = norssit_project.void_path.read_text(encoding="utf-8")
    assert "<https://ldf.fi/norssit/sparql#void>" in text
    assert "exampleResource" not in text
    assert "titles" not in text


def test_generated_file_feeds_the_schema_reader(norssit_project):
    generate_void(norssit_project, fetch=norssit_fetch())
    schema = get_schema_for_endpoint(norssit_project.endpoint_url, str(norssit_project.void_path))
    assert set(schema) == {PERSON, BROTHER}


def test_declared_prefixes_are_written(norssit_project):
    generate_void(norssit_project, fetch=norssit_fetch())
    text = norssit_project.void_path.read_text(encoding="utf-8")
    assert "@prefix schema: <http://schema.org/> ." in text


def test_unparseable_output_raises(norssit_project, monkeypatch: pytest.MonkeyPatch):
    norssit_project.void_path.write_text("# previous\n", encoding="utf-8")
    monkeypatch.setattr(void_gen, "shorten", lambda iri, prefixes: "bad name:x/y")
    with pytest.raises(VoidError, match="Norssit"):
        generate_void(norssit_project, fetch=norssit_fetch())
    assert norssit_project.void_path.read_text(encoding="utf-8") == "# previous\n"
    assert list(norssit_project.dir.glob("void.ttl.tmp*")) == []


def test_no_graph_clause_when_graphs_empty(norssit_project):
    queries: list[str] = []
    generate_void(norssit_project, fetch=norssit_fetch(queries))
    assert len(queries) == 5
    assert not any("GRAPH" in q for q in queries)


def test_graph_clause_when_a_graph_is_set(swiss_project):
    queries: list[str] = []
    generate_void(swiss_project, fetch=norssit_fetch(queries))
    assert all("GRAPH <https://swiss-elites.lod4hss.cloud/resource/>" in q for q in queries)


def test_unreadable_void_file_raises(tmp_path):
    bad = tmp_path / "void.ttl"
    bad.write_text("@prefix x: <http://x/> .\nx:a x:b norssit:titles/x .\n", encoding="utf-8")
    with pytest.raises(ValueError, match="void.ttl"):
        get_schema_for_endpoint("https://x/sparql", str(bad))
