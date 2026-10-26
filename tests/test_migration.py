"""The one-off move of the flat data/*.json files into data/<project id>/."""

from pathlib import Path

from sparql_llm.migration import migrate_legacy_data
from sparql_llm.project import load_project

LEGACY = {
    "endpoints_metadata.json": "prefixes.json",
    "entity_names.json": "entity_names.json",
    "index_fingerprint.json": "fingerprint.json",
    "elites-suisses-examples-synced.md": "examples-synced.md",
}


def test_migration_moves_legacy_files_once(tmp_path: Path):
    for old in LEGACY:
        (tmp_path / old).write_text(f"content of {old}", encoding="utf-8")

    first = migrate_legacy_data(tmp_path, "swiss-elites")
    second = migrate_legacy_data(tmp_path, "swiss-elites")

    assert len(first) == 4
    assert second == []
    for old, new in LEGACY.items():
        assert not (tmp_path / old).exists()
        assert (tmp_path / "swiss-elites" / new).read_text(encoding="utf-8") == f"content of {old}"


def test_migration_never_overwrites(tmp_path: Path):
    (tmp_path / "entity_names.json").write_text("legacy", encoding="utf-8")
    (tmp_path / "swiss-elites").mkdir()
    (tmp_path / "swiss-elites" / "entity_names.json").write_text("current", encoding="utf-8")

    assert migrate_legacy_data(tmp_path, "swiss-elites") == []
    assert (tmp_path / "entity_names.json").read_text(encoding="utf-8") == "legacy"
    assert (tmp_path / "swiss-elites" / "entity_names.json").read_text(encoding="utf-8") == "current"


def test_swiss_project_file_loads():
    project = load_project(Path("data"), "swiss-elites")
    assert len(project.entities.types) == 5
    assert project.prefixes["swel"] == "https://elites-suisses.lod4hss.org/resource/"
