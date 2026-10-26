"""The list of datasets in the data folder: seeding, adding, states and who sees what."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import NORSSIT

USER = SimpleNamespace(id=3, email="user@example.org", is_superuser=False, is_curator=False, is_active=True)
CURATOR = SimpleNamespace(id=2, email="curator@example.org", is_superuser=False, is_curator=True, is_active=True)


def _project(pid: str, url: str) -> dict:
    return {"id": pid, "label": pid.title(), "endpoint_url": url}


def test_seeded_from_install_settings(tmp_path: Path, monkeypatch):
    from sparql_llm.config import settings
    from sparql_llm.migration import migrate_configured

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "projects", ["swiss-elites"])

    migrate_configured()

    content = json.loads((tmp_path / "datasets.json").read_text(encoding="utf-8"))
    assert content == [{"id": "swiss-elites", "published": True}]


def test_add_dataset_creates_new_unpublished(swiss_project):
    from sparql_llm.datasets import add_dataset, dataset_state, list_dataset_ids

    add_dataset("norssit", "Norssit")

    assert list_dataset_ids() == ["swiss-elites", "norssit"]
    assert dataset_state("norssit") == "new"
    staged = json.loads((swiss_project.dir.parent / "norssit" / "uploaded" / "project.json").read_text(encoding="utf-8"))
    assert staged == {"id": "norssit", "label": "Norssit", "endpoint_url": "https://example.org/sparql"}


def test_add_dataset_rejects_bad_or_existing_id(swiss_project):
    from sparql_llm.datasets import DatasetError, add_dataset, list_dataset_ids

    with pytest.raises(DatasetError):
        add_dataset("Nor Sit", "Norssit")
    with pytest.raises(DatasetError):
        add_dataset("swiss-elites", "Again")
    assert list_dataset_ids() == ["swiss-elites"]


def test_publish_requires_a_successful_rebuild(swiss_project):
    from sparql_llm.datasets import DatasetError, add_dataset, dataset_state, set_published

    add_dataset("norssit", "Norssit")
    with pytest.raises(DatasetError, match="has not been rebuilt successfully yet"):
        set_published("norssit", True)

    live = swiss_project.dir.parent / "norssit"
    (live / "project.json").write_text(json.dumps(NORSSIT), encoding="utf-8")
    (live / "fingerprint.json").write_text("{}", encoding="utf-8")
    assert dataset_state("norssit") == "ready"

    set_published("norssit", True)
    assert dataset_state("norssit") == "published"


def test_users_see_published_managers_see_ready(use_projects, tmp_path: Path):
    from sparql_llm.datasets import add_dataset, datasets_for
    from sparql_llm.project import reload_projects

    use_projects(_project("swiss", "https://a.example/sparql"), _project("norssit", "https://b.example/sparql"))
    (tmp_path / "datasets.json").write_text(
        json.dumps([{"id": "swiss", "published": True}, {"id": "norssit", "published": False}]), encoding="utf-8"
    )
    (tmp_path / "norssit" / "fingerprint.json").write_text("{}", encoding="utf-8")
    add_dataset("third", "Third")
    reload_projects()

    assert [p.id for p in datasets_for(USER)] == ["swiss"]
    assert [p.id for p in datasets_for(CURATOR)] == ["swiss", "norssit"]


def test_upload_reusing_another_endpoint_is_refused(use_projects):
    from sparql_llm.project_files import validate_upload

    use_projects(_project("swiss", "https://a.example/sparql"), NORSSIT)
    content = json.dumps({**NORSSIT, "endpoint_url": "https://a.example/sparql"}).encode("utf-8")

    assert validate_upload("norssit", "project.json", content) == [
        "endpoint_url: is already used by dataset 'swiss'. Each dataset needs its own endpoint."
    ]
