"""Admins add, publish and unpublish datasets; every admin card works on the selected dataset."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main

ADMIN = SimpleNamespace(id=1, email="admin@example.org", is_superuser=True, is_curator=False, is_active=True)
CURATOR = SimpleNamespace(id=2, email="curator@example.org", is_superuser=False, is_curator=True, is_active=True)
A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}
B = {"id": "b", "label": "Beta", "endpoint_url": "https://b.example/sparql"}


class _Users:
    """Stands in for the users database on the admin page."""

    async def execute(self, query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [ADMIN, CURATOR]))


def _client_as(user) -> TestClient:
    main.app.dependency_overrides[main.current_active_user] = lambda: user
    main.app.dependency_overrides[main.get_async_session] = lambda: _Users()
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main.app.dependency_overrides.clear()


@pytest.fixture
def two(use_projects, tmp_path: Path):
    return {p.id: p for p in use_projects(A, B)}


def _make_ready(data_dir: Path, project: dict) -> None:
    """What a successful rebuild leaves behind: live project.json and fingerprint.json."""
    (data_dir / project["id"] / "project.json").write_text(json.dumps(project), encoding="utf-8")
    (data_dir / project["id"] / "fingerprint.json").write_text("{}", encoding="utf-8")


def test_admin_adds_a_dataset_and_it_starts_new(two):
    from sparql_llm.datasets import dataset_state, list_dataset_ids

    client = _client_as(ADMIN)
    response = client.post("/admin/datasets", json={"id": "norssit", "label": "Norssit"})

    assert response.status_code == 200
    assert list_dataset_ids() == ["a", "b", "norssit"]
    assert dataset_state("norssit") == "new"
    bad = client.post("/admin/datasets", json={"id": "Nor Sit", "label": "Norssit"})
    assert bad.status_code == 422 and "is not a valid id" in bad.json()["error"]
    again = client.post("/admin/datasets", json={"id": "a", "label": "Again"})
    assert again.status_code == 422 and "already exists" in again.json()["error"]


def test_curator_cannot_add_or_publish(two):
    from sparql_llm.datasets import list_dataset_ids

    client = _client_as(CURATOR)

    assert client.post("/admin/datasets", json={"id": "c", "label": "Gamma"}).status_code == 403
    assert client.post("/admin/datasets/b/unpublish").status_code == 403
    assert client.post("/admin/datasets/b/publish").status_code == 403
    assert list_dataset_ids() == ["a", "b"]


def test_publish_only_after_a_successful_rebuild(two, tmp_path: Path):
    from sparql_llm.datasets import dataset_state

    client = _client_as(ADMIN)
    client.post("/admin/datasets", json={"id": "c", "label": "Gamma"})

    refused = client.post("/admin/datasets/c/publish")
    assert refused.status_code == 409
    assert "has not been rebuilt successfully yet" in refused.json()["error"]

    _make_ready(tmp_path, {"id": "c", "label": "Gamma", "endpoint_url": "https://c.example/sparql"})
    assert client.post("/admin/datasets/c/publish").status_code == 200
    assert dataset_state("c") == "published"
    assert client.post("/admin/datasets/c/unpublish").status_code == 200
    assert dataset_state("c") == "ready"
    assert client.post("/admin/datasets/ghost/publish").status_code == 404


def test_admin_cards_follow_the_selected_dataset(two, tmp_path: Path):
    client = _client_as(ADMIN)
    client.post("/admin/datasets", json={"id": "c", "label": "Gamma"})
    _make_ready(tmp_path, B)
    client.post("/admin/datasets/b/unpublish")

    page = client.get("/admin?dataset=b").text
    files = client.get("/admin/project-files?dataset=b").json()

    assert 'data-selected-dataset="b"' in page
    assert "Alpha" in page and "published" in page
    assert "ready, not published" in page
    assert "Gamma" in page and ">new<" in page
    assert list(files["projects"]) == ["b"]
