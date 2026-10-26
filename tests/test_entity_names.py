"""Offline tests for building each project's entity_names.json from its endpoint."""

import json

from sparql_llm.indexing.entity_names import build_entity_names, entity_query, fetch_entities


def _b(**kw):
    return {k: {"type": "literal", "value": v} for k, v in kw.items()}


def fake_fetch(project, pages_by_type, calls=None):
    """A query_sparql stand-in serving one page per class, then an empty page."""

    def fetch(query, endpoint, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        for type_name, entity_type in project.entities.types.items():
            if f"<{entity_type.class_iri}>" in query:
                rows = pages_by_type.get(type_name, [])
                return {"results": {"bindings": rows if "OFFSET 0" in query else []}}
        raise AssertionError(f"unexpected query: {query[:80]}")

    return fetch


# ── Queries follow the project's entity config and graphs ─────────────────────


def test_query_without_graph(norssit_project):
    query = entity_query(norssit_project, "Person", 0)
    assert "GRAPH" not in query
    assert "<http://www.w3.org/2004/02/skos/core#prefLabel>" in query
    assert "<http://xmlns.com/foaf/0.1/Person>" in query


def test_query_with_one_graph(swiss_project):
    query = entity_query(swiss_project, "Person", 0)
    assert "GRAPH <https://swiss-elites.lod4hss.cloud/resource/>" in query


def test_query_with_birth_death_only_for_types_that_have_them(norssit_project):
    person = entity_query(norssit_project, "Person", 0)
    group = entity_query(norssit_project, "Group", 0)
    assert "<http://schema.org/birthDate>" in person and "<http://schema.org/deathDate>" in person
    assert "OPTIONAL" not in group


def test_fetch_uses_the_project_method(use_projects):
    project = use_projects(
        {
            "id": "posty",
            "label": "Posty",
            "endpoint_url": "https://x/sparql",
            "method": "POST",
            "entities": {"label_property": "http://x/label", "types": {"Thing": {"class": "http://x/Thing"}}},
        }
    )[0]
    calls: list[dict] = []
    fetch_entities(project, fetch=fake_fetch(project, {}, calls))
    assert calls and all(c["post"] is True for c in calls)


# ── Building the file ─────────────────────────────────────────────────────────


def test_fetches_every_type_with_person_years(swiss_project):
    pages = {
        "Person": [_b(s="https://x/p1", l="Brenner, Ernst", birth="1856", death="1911"), _b(s="https://x/p2", l="Henzi, ")],
        "Place": [_b(s="https://x/pl1", l="Geneva")],
    }
    rows = fetch_entities(swiss_project, fetch=fake_fetch(swiss_project, pages))
    assert {"uri": "https://x/p1", "label": "Brenner, Ernst", "type": "Person", "birth": "1856", "death": "1911"} in rows
    assert {"uri": "https://x/p2", "label": "Henzi, ", "type": "Person"} in rows
    assert {"uri": "https://x/pl1", "label": "Geneva", "type": "Place"} in rows


def test_build_writes_the_file(swiss_project):
    target = swiss_project.entity_names_path
    pages = {"Group": [_b(s="https://x/g1", l="Conseil fédéral")]}
    report = build_entity_names(swiss_project, fetch=fake_fetch(swiss_project, pages))
    assert report == {"entities": 1, "path": str(target), "error": ""}
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["endpoint"] == swiss_project.endpoint_url
    assert data["entities"] == [{"uri": "https://x/g1", "label": "Conseil fédéral", "type": "Group"}]


def test_build_writes_utf8_labels(norssit_project):
    pages = {"Person": [_b(s="http://ldf.fi/norssit/norssi_1", l="Väkkärä, Irja")]}
    build_entity_names(norssit_project, fetch=fake_fetch(norssit_project, pages))
    raw = norssit_project.entity_names_path.read_text(encoding="utf-8")
    assert "Väkkärä, Irja" in raw
    assert json.loads(raw)["entities"][0]["label"] == "Väkkärä, Irja"


def test_build_skipped_without_entities_config(use_projects):
    project = use_projects({"id": "bare", "label": "Bare", "endpoint_url": "https://x/sparql"})[0]
    report = build_entity_names(project, fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no query")))
    assert report["entities"] == 0
    assert report["skipped"] is True
    assert not project.entity_names_path.exists()


def test_failed_build_keeps_previous_file(swiss_project):
    target = swiss_project.entity_names_path
    target.write_text('{"entities": [{"uri": "u", "label": "old", "type": "Person"}]}', encoding="utf-8")

    def broken(query, endpoint, **kwargs):
        raise TimeoutError("endpoint down")

    report = build_entity_names(swiss_project, fetch=broken)
    assert report["entities"] == 0
    assert "endpoint down" in report["error"]
    assert json.loads(target.read_text(encoding="utf-8"))["entities"][0]["label"] == "old"


def test_an_empty_result_is_an_error_not_an_empty_file(swiss_project):
    report = build_entity_names(swiss_project, fetch=fake_fetch(swiss_project, {}))
    assert report["error"]
    assert not swiss_project.entity_names_path.exists()


def test_a_much_smaller_list_does_not_replace_the_previous_one(swiss_project):
    target = swiss_project.entity_names_path
    old = [{"uri": f"u{i}", "label": f"Old {i}", "type": "Person"} for i in range(10)]
    target.write_text(json.dumps({"entities": old}), encoding="utf-8")
    pages = {"Person": [_b(s=f"https://x/p{i}", l=f"New {i}") for i in range(5)]}
    report = build_entity_names(swiss_project, fetch=fake_fetch(swiss_project, pages))
    assert report["entities"] == 0
    assert "5" in report["error"] and "10" in report["error"]
    assert len(json.loads(target.read_text(encoding="utf-8"))["entities"]) == 10


def test_a_similar_sized_list_replaces_the_previous_one(swiss_project):
    target = swiss_project.entity_names_path
    target.write_text(json.dumps({"entities": [{"uri": "u", "label": "Old", "type": "Person"}]}), encoding="utf-8")
    report = build_entity_names(swiss_project, fetch=fake_fetch(swiss_project, {"Person": [_b(s="https://x/p", l="New")]}))
    assert report["error"] == "" and report["entities"] == 1


# ── The resolver reads every project's file ───────────────────────────────────


def test_resolver_merges_every_project_file(tmp_path):
    from sparql_llm.entity_resolver import EntityResolver

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"entities": [{"uri": "https://a/1", "label": "Brenner, Ernst", "type": "Person"}]}), encoding="utf-8")
    b.write_text(json.dumps({"entities": [{"uri": "https://b/1", "label": "Waltari, Mika", "type": "Person"}]}), encoding="utf-8")
    resolver = EntityResolver([a, b, tmp_path / "absent.json"])
    assert resolver.resolve("Ernst Brenner").candidates[0].uri == "https://a/1"
    assert resolver.resolve("Mika Waltari").candidates[0].uri == "https://b/1"


def test_a_list_from_another_endpoint_is_replaced_even_if_smaller(norssit_project):
    # After a wrong endpoint is corrected, the big wrong list must not survive the shrink guard.
    old = [{"uri": f"https://swiss/p{i}", "label": f"Old {i}", "type": "Person"} for i in range(10)]
    norssit_project.entity_names_path.write_text(
        json.dumps({"endpoint": "https://swiss.example/sparql", "entities": old}), encoding="utf-8"
    )
    pages = {"Person": [_b(s="http://ldf.fi/norssit/norssi_1", l="Waltari, Mika")]}
    report = build_entity_names(norssit_project, fetch=fake_fetch(norssit_project, pages))
    assert report["error"] == "" and report["entities"] == 1


def test_resolver_ignores_projects_without_entities(use_projects):
    from sparql_llm.entity_resolver import get_resolver

    project = use_projects({"id": "bare", "label": "Bare", "endpoint_url": "https://x/sparql"})[0]
    project.entity_names_path.parent.mkdir(parents=True, exist_ok=True)
    project.entity_names_path.write_text(
        json.dumps({"entities": [{"uri": "https://x/1", "label": "Stale, Name", "type": "Person"}]}), encoding="utf-8"
    )
    assert get_resolver().resolve("Name Stale") is None
