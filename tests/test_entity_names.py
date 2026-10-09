"""Offline tests for building data/entity_names.json from the endpoint."""

import json
from pathlib import Path

from sparql_llm.indexing.entity_names import ENTITY_TYPES, build_entity_names, fetch_entities

EP = "https://example.org/sparql"


def _b(**kw):
    return {k: {"type": "literal", "value": v} for k, v in kw.items()}


def fake_fetch(pages_by_type):
    """A query_sparql stand-in serving one page per class, then an empty page."""

    def fetch(query, endpoint, **kwargs):
        for type_name, iri in ENTITY_TYPES.items():
            if f"<{iri}>" in query:
                rows = pages_by_type.get(type_name, [])
                return {"results": {"bindings": rows if "OFFSET 0" in query else []}}
        raise AssertionError(f"unexpected query: {query[:80]}")

    return fetch


def test_fetches_every_type_with_person_years():
    pages = {
        "Person": [_b(s="https://x/p1", l="Brenner, Ernst", birth="1856", death="1911"), _b(s="https://x/p2", l="Henzi, ")],
        "Place": [_b(s="https://x/pl1", l="Geneva")],
    }
    rows = fetch_entities(EP, fetch=fake_fetch(pages))
    assert {"uri": "https://x/p1", "label": "Brenner, Ernst", "type": "Person", "birth": "1856", "death": "1911"} in rows
    assert {"uri": "https://x/p2", "label": "Henzi, ", "type": "Person"} in rows
    assert {"uri": "https://x/pl1", "label": "Geneva", "type": "Place"} in rows


def test_build_writes_the_file(tmp_path: Path):
    target = tmp_path / "entity_names.json"
    report = build_entity_names(target, EP, fetch=fake_fetch({"Group": [_b(s="https://x/g1", l="Conseil fédéral")]}))
    assert report == {"entities": 1, "path": str(target), "error": ""}
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["endpoint"] == EP
    assert data["entities"] == [{"uri": "https://x/g1", "label": "Conseil fédéral", "type": "Group"}]


def test_failed_build_keeps_previous_file(tmp_path: Path):
    target = tmp_path / "entity_names.json"
    target.write_text('{"entities": [{"uri": "u", "label": "old", "type": "Person"}]}', encoding="utf-8")

    def broken(query, endpoint, **kwargs):
        raise TimeoutError("endpoint down")

    report = build_entity_names(target, EP, fetch=broken)
    assert report["entities"] == 0
    assert "endpoint down" in report["error"]
    assert json.loads(target.read_text(encoding="utf-8"))["entities"][0]["label"] == "old"


def test_an_empty_result_is_an_error_not_an_empty_file(tmp_path: Path):
    target = tmp_path / "entity_names.json"
    report = build_entity_names(target, EP, fetch=fake_fetch({}))
    assert report["error"]
    assert not target.exists()


def test_a_much_smaller_list_does_not_replace_the_previous_one(tmp_path: Path):
    target = tmp_path / "entity_names.json"
    old = [{"uri": f"u{i}", "label": f"Old {i}", "type": "Person"} for i in range(10)]
    target.write_text(json.dumps({"entities": old}), encoding="utf-8")
    pages = {"Person": [_b(s=f"https://x/p{i}", l=f"New {i}") for i in range(5)]}
    report = build_entity_names(target, EP, fetch=fake_fetch(pages))
    assert report["entities"] == 0
    assert "5" in report["error"] and "10" in report["error"]
    assert len(json.loads(target.read_text(encoding="utf-8"))["entities"]) == 10


def test_a_similar_sized_list_replaces_the_previous_one(tmp_path: Path):
    target = tmp_path / "entity_names.json"
    target.write_text(json.dumps({"entities": [{"uri": "u", "label": "Old", "type": "Person"}]}), encoding="utf-8")
    report = build_entity_names(target, EP, fetch=fake_fetch({"Person": [_b(s="https://x/p", l="New")]}))
    assert report["error"] == "" and report["entities"] == 1
