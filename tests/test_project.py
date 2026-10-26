"""Each endpoint is described by its own project file, data/<id>/project.json."""

import json
from pathlib import Path

import pytest

from sparql_llm.config import Settings, SettingsError, settings
from sparql_llm.project import ProjectError, load_project, reload_projects

NORSSIT = {
    "id": "norssit",
    "label": "Norssit",
    "endpoint_url": "https://ldf.fi/norssit/sparql",
    "method": "GET",
    "description": "Alumni of the Helsinki Normal Lyceum",
    "homepage_url": "http://www.ldf.fi/dataset/norssit",
    "graphs": [],
    "prefixes": {
        "schema": "http://schema.org/",
        "bioc": "http://ldf.fi/schema/bioc/",
        "norssit": "http://ldf.fi/norssit/",
    },
    "dataset_description_file": "dataset.md",
    "examples_file": "examples.md",
    "entities": {
        "label_property": "http://www.w3.org/2004/02/skos/core#prefLabel",
        "types": {
            "Person": {
                "class": "http://xmlns.com/foaf/0.1/Person",
                "birth_property": "http://schema.org/birthDate",
                "death_property": "http://schema.org/deathDate",
            },
            "Group": {"class": "http://xmlns.com/foaf/0.1/Organization"},
        },
    },
    "schema_classes": {"min_instances": 50, "exclude": ["http://ldf.fi/norssit/titles/"]},
    "curator_examples": None,
    "prefix_repairs": {},
    "ontology_from_endpoint": False,
}


def write_project(data_dir: Path, folder: str, content: dict) -> None:
    (data_dir / folder).mkdir(parents=True, exist_ok=True)
    (data_dir / folder / "project.json").write_text(json.dumps(content), encoding="utf-8")


def test_load_project_reads_fields(tmp_path: Path):
    write_project(tmp_path, "norssit", NORSSIT)
    p = load_project(tmp_path, "norssit")
    assert p.method == "GET"
    assert p.entities.types["Person"].class_iri == "http://xmlns.com/foaf/0.1/Person"
    assert p.void_path == tmp_path / "norssit" / "void.ttl"
    assert p.dataset_path == tmp_path / "norssit" / "dataset.md"


def test_missing_project_file_names_the_path(tmp_path: Path):
    with pytest.raises(ProjectError) as err:
        load_project(tmp_path, "norssit")
    assert "norssit" in str(err.value)
    assert "project.json" in str(err.value)


def test_id_mismatch_is_rejected(tmp_path: Path):
    write_project(tmp_path, "norssit", {**NORSSIT, "id": "other"})
    with pytest.raises(ProjectError, match="id"):
        load_project(tmp_path, "norssit")


def test_bad_id_slug_rejected(tmp_path: Path):
    write_project(tmp_path, "norssit", {**NORSSIT, "id": "Norssit Chat"})
    with pytest.raises(ProjectError):
        load_project(tmp_path, "norssit")


def test_settings_file_missing_raises(tmp_path: Path):
    with pytest.raises(SettingsError):
        Settings.from_file(str(tmp_path / "nope.json"))


def test_settings_endpoints_derived_from_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_project(tmp_path, "norssit", NORSSIT)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "projects", ["norssit"])
    reload_projects()
    try:
        assert settings.endpoints[0]["endpoint_url"] == "https://ldf.fi/norssit/sparql"
    finally:
        monkeypatch.undo()
        reload_projects()
