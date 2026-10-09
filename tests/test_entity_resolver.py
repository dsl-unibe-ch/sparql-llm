"""Offline tests for the entity resolver, on a fixture of real labels."""

import json
import os
import time
from pathlib import Path

import pytest

from sparql_llm.entity_resolver import EntityResolver, normalise

ROWS = [
    {"uri": "u:brenner", "label": "Brenner, Ernst", "type": "Person", "birth": "1856", "death": "1911"},
    {"uri": "u:brunner", "label": "Brunner, Ernst", "type": "Person"},
    {"uri": "u:adele1", "label": "Burckhardt, Adèle", "type": "Person", "birth": "1880"},
    {"uri": "u:ogi", "label": "Ogi, Adolf", "type": "Person", "birth": "1942"},
    {"uri": "u:derogis", "label": "Derogis, Sophie", "type": "Person"},
    {"uri": "u:forrer", "label": "Forrer, Ludwig", "type": "Person"},
    {"uri": "u:forrer2", "label": "Forrer-Dändliker, Ludwig", "type": "Person"},
    *[{"uri": f"u:weber{i}", "label": "Weber, Heinrich", "type": "Person", "birth": str(1800 + i)} for i in range(7)],
    *[{"uri": f"u:wx{i}", "label": f"Weber, Name{i}", "type": "Person"} for i in range(5)],
    *[{"uri": f"u:geneva{i}", "label": "Geneva", "type": "Place"} for i in range(4)],
    {"uri": "u:cf", "label": "Conseil fédéral", "type": "Group"},
]


@pytest.fixture
def resolver(tmp_path: Path) -> EntityResolver:
    path = tmp_path / "entity_names.json"
    path.write_text(json.dumps({"entities": ROWS}), encoding="utf-8")
    return EntityResolver(path)


def uris(resolution):
    return [c.uri for c in resolution.candidates]


def test_normalise_folds_accents_case_and_punctuation():
    assert normalise("Burckhardt, Adèle") == "burckhardt adele"
    assert normalise("  Forrer-Dändliker,  Ludwig ") == "forrer dandliker ludwig"


def test_word_order_does_not_matter(resolver):
    assert uris(resolver.resolve("Ernst Brenner")) == ["u:brenner"]


def test_accents_do_not_matter(resolver):
    assert uris(resolver.resolve("Adele Burckhardt")) == ["u:adele1"]


def test_a_typo_still_finds_the_person_first(resolver):
    assert uris(resolver.resolve("Brener Ernst"))[0] == "u:brenner"


def test_exact_matches_hide_near_misses(resolver):
    assert "u:brunner" not in uris(resolver.resolve("Ernst Brenner"))


def test_surname_alone_finds_the_person_but_not_substrings(resolver):
    assert uris(resolver.resolve("Ogi")) == ["u:ogi"]


def test_double_names_are_both_candidates(resolver):
    assert set(uris(resolver.resolve("Ludwig Forrer"))) == {"u:forrer", "u:forrer2"}


def test_homonyms_come_back_with_their_years(resolver):
    res = resolver.resolve("Heinrich Weber")
    assert res.total == 7
    assert len(res.candidates) == 7
    assert {c.years for c in res.candidates} == {f"b. {1800 + i}" for i in range(7)}


def test_many_matches_are_capped_with_the_total(resolver):
    res = resolver.resolve("Weber")
    assert len(res.candidates) == 8
    assert res.total == 12


def test_identical_place_labels_are_all_returned(resolver):
    assert len(resolver.resolve("Geneva").candidates) == 4


def test_no_match_is_an_empty_resolution(resolver):
    res = resolver.resolve("Xyzzy Quux")
    assert res.candidates == [] and res.total == 0


def test_numbers_and_dates_are_skipped(resolver):
    assert resolver.resolve("1950") is None
    assert resolver.resolve("21.08.1842") is None
    assert resolver.resolve("  ") is None


def test_missing_file_is_unavailable_not_an_error(tmp_path):
    resolver = EntityResolver(tmp_path / "absent.json")
    assert resolver.available is False
    assert resolver.resolve("Ernst Brenner") is None


def test_corrupt_file_is_unavailable(tmp_path):
    path = tmp_path / "entity_names.json"
    path.write_text("{not json", encoding="utf-8")
    assert EntityResolver(path).resolve("Ernst Brenner") is None


def test_reloads_when_file_changes(resolver, tmp_path):
    assert resolver.resolve("Ernst Brenner").candidates
    path = tmp_path / "entity_names.json"
    path.write_text(json.dumps({"entities": [{"uri": "u:new", "label": "Neu, Anna", "type": "Person"}]}), encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert uris(resolver.resolve("Anna Neu")) == ["u:new"]
    assert resolver.resolve("Ernst Brenner").candidates == []


def test_tool_text_lists_candidates_with_years(resolver):
    from sparql_llm.entity_resolver import describe_for_tool

    text = describe_for_tool(resolver, "Heinrich Weber", limit=3)
    assert text.count("<u:weber") == 3
    assert "b. 18" in text
    assert "ask the user" in text


def test_tool_text_filters_by_type(resolver):
    from sparql_llm.entity_resolver import describe_for_tool

    text = describe_for_tool(resolver, "Geneva", entity_type="person")
    assert "No entity matching 'Geneva'" in text


def test_tool_text_without_file_points_to_the_fallback(tmp_path):
    from sparql_llm.entity_resolver import describe_for_tool

    text = describe_for_tool(EntityResolver(tmp_path / "absent.json"), "Ernst Brenner")
    assert "not been built" in text and "Surname, Firstname" in text
