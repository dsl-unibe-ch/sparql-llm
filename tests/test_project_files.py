"""Curators replace a project's files from the admin page; a bad upload never replaces a good file."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main
from sparql_llm.indexing.rebuild import lock_file
from sparql_llm.project_files import file_path, list_files, restore_previous, save_upload, validate_upload

ADMIN = SimpleNamespace(id=1, email="stephen@example.org", is_superuser=False, is_curator=True, is_active=True)
USER = SimpleNamespace(id=2, email="user@example.org", is_superuser=False, is_curator=False, is_active=True)


@pytest.fixture
def client_as():
    def _client(user):
        main.app.dependency_overrides[main.current_active_user] = lambda: user
        return TestClient(main.app)

    yield _client
    main.app.dependency_overrides.clear()


def _project_json(project, **changes) -> bytes:
    content = json.loads(project.dir.joinpath("project.json").read_text(encoding="utf-8"))
    content.update(changes)
    return json.dumps(content).encode("utf-8")


def test_a_valid_project_file_passes(norssit_project):
    assert validate_upload("norssit", "project.json", _project_json(norssit_project, label="Norssit 2")) == []


def test_upload_with_other_id_rejected_and_old_file_kept(norssit_project, client_as):
    before = norssit_project.dir.joinpath("project.json").read_bytes()
    errors = validate_upload("norssit", "project.json", _project_json(norssit_project, id="other"))
    assert errors and "id" in errors[0]
    response = client_as(ADMIN).post(
        "/admin/project-files/norssit/project.json",
        files={"file": ("project.json", _project_json(norssit_project, id="other"), "application/json")},
    )
    assert response.status_code == 422
    assert any("id" in e for e in response.json()["errors"])
    assert norssit_project.dir.joinpath("project.json").read_bytes() == before


def test_upload_for_unlisted_project_rejected(norssit_project):
    errors = validate_upload("ghost", "dataset.md", b"## About\n\nText.\n")
    assert errors == ["Project 'ghost' is not served by this instance."]


def test_upload_during_rebuild_rejected(norssit_project, client_as):
    lock_file().write_text("1 now", encoding="utf-8")
    before = norssit_project.dataset_path.read_bytes()
    response = client_as(ADMIN).post(
        "/admin/project-files/norssit/dataset.md", files={"file": ("dataset.md", b"## New\n\nText.\n", "text/markdown")}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "A rebuild is running; try again when it finishes."
    assert norssit_project.dataset_path.read_bytes() == before


def test_invalid_field_message_is_plain(norssit_project):
    errors = validate_upload("norssit", "project.json", _project_json(norssit_project, method="PUT"))
    assert "method: must be GET or POST" in errors


def test_invalid_json_message_names_the_line(norssit_project):
    errors = validate_upload("norssit", "project.json", b'{\n  "id": "norssit",\n}')
    assert len(errors) == 1 and "line 3" in errors[0]


def test_examples_without_any_example_rejected(norssit_project):
    errors = validate_upload("norssit", "examples.md", b"# Examples\n\nNone yet.\n")
    assert errors and "no example" in errors[0]


def test_unknown_file_name_rejected(norssit_project, client_as):
    response = client_as(ADMIN).post(
        "/admin/project-files/norssit/..%2Fusers.db", files={"file": ("x", b"x", "text/plain")}
    )
    assert response.status_code in (404, 422)
    assert validate_upload("norssit", "users.db", b"x")


def test_save_keeps_the_upload_pending_and_undo_restores_a_promoted_file(norssit_project):
    from sparql_llm.indexing.publication import apply_files, files_for
    from sparql_llm.project import load_project

    old = norssit_project.dataset_path.read_text(encoding="utf-8")
    save_upload("norssit", "dataset.md", "## New\n\nVäkkärä.\n".encode("utf-8"), "stephen@example.org")
    assert norssit_project.dataset_path.read_text(encoding="utf-8") == old  # not live yet
    listed = {f["name"]: f for f in list_files("norssit")}
    assert listed["dataset.md"]["uploaded_by"] == "stephen@example.org"
    assert listed["dataset.md"]["pending"] is True

    candidate = load_project(norssit_project.dir.parent, "norssit", candidate=True)
    apply_files(candidate.dir, files_for(candidate))  # what a rebuild does
    assert norssit_project.dataset_path.read_text(encoding="utf-8") == "## New\n\nVäkkärä.\n"
    assert (norssit_project.dir / "dataset.md.prev").read_text(encoding="utf-8") == old

    restore_previous("norssit", "dataset.md")  # stages the previous version for the next rebuild
    assert {f["name"]: f for f in list_files("norssit")}["dataset.md"]["pending"] is True
    assert file_path("norssit", "dataset.md").read_text(encoding="utf-8") == old


def test_upload_route_saves_and_download_returns_it(norssit_project, client_as):
    client = client_as(ADMIN)
    response = client.post(
        "/admin/project-files/norssit/dataset.md", files={"file": ("dataset.md", "## Neu\n\nä\n".encode(), "text/markdown")}
    )
    assert response.status_code == 200
    download = client.get("/admin/project-files/norssit/dataset.md")
    assert download.status_code == 200
    assert download.text == "## Neu\n\nä\n"
    assert download.headers["content-type"].startswith("text/plain")


def test_non_manager_gets_403(norssit_project, client_as):
    client = client_as(USER)
    assert client.get("/admin/project-files").status_code == 403
    assert client.get("/admin/project-files/norssit/dataset.md").status_code == 403
    assert client.post(
        "/admin/project-files/norssit/dataset.md", files={"file": ("dataset.md", b"## x\n\ny\n", "text/markdown")}
    ).status_code == 403


def test_path_field_message_is_plain(norssit_project):
    errors = validate_upload("norssit", "project.json", _project_json(norssit_project, dataset_description_file="../../.env"))
    assert errors == ["dataset_description_file: must be a file name in the project folder ending in .md, e.g. dataset.md"]
