"""Uploaded files and rebuild output stay out of service until a rebuild succeeds."""

import json

import pytest

from sparql_llm.agent.prompts import resolution_prompt
from sparql_llm.config import settings
from sparql_llm.indexing import rebuild
from sparql_llm.indexing.drift import Fingerprint
from sparql_llm.indexing.preflight import CheckResult
from sparql_llm.migration import seed_projects
from sparql_llm.project import get_projects, reload_projects
from sparql_llm.project_files import list_files, restore_previous, save_upload

from .conftest import NORSSIT


def _upload_project(**changes) -> None:
    save_upload("norssit", "project.json", json.dumps({**NORSSIT, **changes}).encode("utf-8"), "s@x")


def test_an_upload_is_not_served_after_a_restart(norssit_project):
    _upload_project(endpoint_url="https://wrong.example/sparql")
    reload_projects()  # what a restart does
    assert get_projects()[0].endpoint_url == "https://ldf.fi/norssit/sparql"
    assert {f["name"]: f for f in list_files("norssit")}["project.json"]["pending"] is True


def test_an_uploaded_dataset_md_does_not_reach_the_prompt(norssit_project):
    save_upload("norssit", "dataset.md", b"## Uploaded\n\nNOT YET CHECKED\n", "s@x")
    assert "NOT YET CHECKED" not in resolution_prompt(norssit_project)


def test_undo_discards_a_pending_upload(norssit_project):
    live = norssit_project.dataset_path.read_text(encoding="utf-8")
    save_upload("norssit", "dataset.md", b"## Uploaded\n\nx\n", "s@x")
    restore_previous("norssit", "dataset.md")
    assert not {f["name"]: f for f in list_files("norssit")}["dataset.md"]["pending"]
    assert norssit_project.dataset_path.read_text(encoding="utf-8") == live


@pytest.fixture
def writing_pipeline(norssit_project, monkeypatch: pytest.MonkeyPatch):
    """The rebuild steps, faked so they write their files where the real ones would."""
    norssit_project.void_path.write_text("# live void\n", encoding="utf-8")
    norssit_project.entity_names_path.write_text('{"entities": []}', encoding="utf-8")

    class FakeMetadata:
        def __init__(self, projects=None, auto_init=False):
            self.projects = projects
            self.prefixes_map = {}

        def refresh(self):
            for p in self.projects:
                p.prefixes_path.write_text('{"prefixes_map": {}, "classes_schema": {}}', encoding="utf-8")

    def void(project):
        project.void_path.write_text("# new void\n", encoding="utf-8")
        return {"triples": 1, "entities": 1, "classes_total": 1, "classes_kept": 1, "predicates": 1}

    def names(project):
        project.entity_names_path.write_text('{"entities": [{"label": "new"}]}', encoding="utf-8")
        return {"entities": 1, "error": ""}

    monkeypatch.setattr(rebuild, "run_preflight", lambda p: [CheckResult("endpoint", "ok", "fine")])
    monkeypatch.setattr(rebuild, "sync_curator_examples", lambda p: {"skipped": True, "accepted": 0, "rejected": [], "error": ""})
    monkeypatch.setattr(rebuild, "generate_void", void)
    monkeypatch.setattr(rebuild, "EndpointsMetadataManager", FakeMetadata)
    monkeypatch.setattr(rebuild, "build_entity_names", names)
    monkeypatch.setattr(rebuild, "take_fingerprint", lambda p: Fingerprint(triples=1))
    monkeypatch.setattr(rebuild, "_collections", lambda: [])
    monkeypatch.setattr(rebuild, "_point_alias", lambda alias, target: None)
    return norssit_project


def test_a_late_failure_leaves_every_live_file_untouched(writing_pipeline, monkeypatch: pytest.MonkeyPatch):
    project = writing_pipeline
    _upload_project(label="Norssit v2")
    monkeypatch.setattr(rebuild, "init_vectordb", lambda collection_name=None, projects=None, prefixes_map=None: 0)
    rebuild.run_rebuild_job("norssit")
    assert rebuild.read_job("norssit")["state"] == "failed"
    assert project.void_path.read_text(encoding="utf-8") == "# live void\n"
    assert project.entity_names_path.read_text(encoding="utf-8") == '{"entities": []}'
    assert not (project.dir / "next").exists()
    assert get_projects()[0].label == "Norssit"
    assert {f["name"]: f for f in list_files("norssit")}["project.json"]["pending"] is True


def test_a_successful_rebuild_promotes_uploads_and_generated_files(writing_pipeline, monkeypatch: pytest.MonkeyPatch):
    project = writing_pipeline
    _upload_project(label="Norssit v2")
    seen = []
    monkeypatch.setattr(
        rebuild,
        "init_vectordb",
        lambda collection_name=None, projects=None, prefixes_map=None: seen.extend(p.label for p in projects) or 5,
    )
    rebuild.run_rebuild_job("norssit")
    assert rebuild.read_job("norssit")["state"] == "done", rebuild.read_job("norssit")["message"]
    assert seen == ["Norssit v2"]
    assert project.void_path.read_text(encoding="utf-8") == "# new void\n"
    assert json.loads(project.entity_names_path.read_text(encoding="utf-8"))["entities"] == [{"label": "new"}]
    assert get_projects()[0].label == "Norssit v2"
    assert not (project.dir / "next").exists() and not (project.dir / "uploaded").exists()
    assert {f["name"]: f for f in list_files("norssit")}["project.json"]["has_previous"] is True


def test_seed_is_copied_only_when_the_live_project_is_missing(tmp_path, monkeypatch: pytest.MonkeyPatch):
    seeds = tmp_path / "seeds"
    (seeds / "swiss-elites").mkdir(parents=True)
    for name in ("project.json", "dataset.md", "examples.md"):
        (seeds / "swiss-elites" / name).write_text(f"seed {name}", encoding="utf-8")
    data = tmp_path / "data"
    assert seed_projects(data, ["swiss-elites"], seeds) == ["swiss-elites"]
    assert (data / "swiss-elites" / "dataset.md").read_text(encoding="utf-8") == "seed dataset.md"
    (data / "swiss-elites" / "dataset.md").write_text("curated on the server", encoding="utf-8")
    assert seed_projects(data, ["swiss-elites"], seeds) == []
    assert (data / "swiss-elites" / "dataset.md").read_text(encoding="utf-8") == "curated on the server"


def test_the_swiss_seed_is_shipped():
    from sparql_llm.migration import SEEDS_DIR

    assert (SEEDS_DIR / "swiss-elites" / "project.json").is_file()
    assert settings is not None
