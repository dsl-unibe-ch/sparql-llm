"""Health reports what a publish left unfinished, by dataset, and never touches the network."""

import threading
from types import SimpleNamespace

import pytest
from qdrant_client import models
from starlette.testclient import TestClient

from sparql_llm import datasets
from sparql_llm.agent import main
from sparql_llm.config import settings
from sparql_llm.indexing import rebuild
from sparql_llm.indexing.publication import GENERATION_FILE, record_path, write_generation

from .crash_stubs import install_stubs
from .test_crash_safety import Crash, fail_once, old_version, real_qdrant  # noqa: F401

ADMIN = SimpleNamespace(id=1, email="admin@example.org", is_superuser=True, is_curator=False, is_active=True)
UNFINISHED = "Publishing did not finish. Press Rebuild to finish it."
MISSING = "The search index is missing. Press Rebuild."
MISMATCH = "The search index does not match the published files. Press Rebuild."
STALE = "A rebuild is marked as running, but nothing is running."


@pytest.fixture(autouse=True)
def _no_worker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rebuild, "_worker", None)


def test_a_healthy_dataset_has_no_problems(old_version):  # noqa: F811
    assert rebuild.publication_problems() == {}


def test_an_unfinished_publish_is_reported(old_version, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    fail_once(monkeypatch, "apply_files", Crash())
    with pytest.raises(Crash):
        rebuild.rebuild_index_with_alias("norssit")
    assert UNFINISHED in rebuild.publication_problems()["norssit"]


def test_an_unreadable_record_is_reported(old_version):  # noqa: F811
    record_path(old_version.dir).write_text("{", encoding="utf-8")
    assert rebuild.publication_problems() == {"norssit": [UNFINISHED]}


def test_a_pending_removal_is_reported_even_when_no_longer_listed(old_version):  # noqa: F811
    datasets.removal_path("gone").write_text("{}", encoding="utf-8")
    assert rebuild.publication_problems() == {
        "gone": ["Removing this dataset did not finish. It will be retried at the next start."]
    }


def test_a_missing_index_is_reported_for_a_ready_dataset(old_version):  # noqa: F811
    rebuild.drop_index("norssit")
    assert rebuild.publication_problems() == {"norssit": [MISSING]}


def test_a_new_dataset_with_no_index_is_not_a_problem(use_projects, real_qdrant, tmp_path):  # noqa: F811
    use_projects({"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"})
    datasets.set_published("a", False)
    assert rebuild.publication_problems() == {}


def test_a_legacy_first_dataset_served_by_the_old_collection_is_not_a_problem(old_version, real_qdrant):  # noqa: F811
    rebuild.drop_index("norssit")
    (old_version.dir / GENERATION_FILE).unlink()
    real_qdrant.create_collection(
        settings.docs_collection_name, vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE)
    )
    assert rebuild.publication_problems() == {}


def test_an_upgraded_dataset_without_a_generation_file_is_not_a_problem(old_version):  # noqa: F811
    (old_version.dir / GENERATION_FILE).unlink()
    assert rebuild.publication_problems() == {}


def test_a_generation_mismatch_is_reported(old_version):  # noqa: F811
    write_generation(old_version.dir, "norssit__v19990101-000000")
    assert rebuild.publication_problems() == {"norssit": [MISMATCH]}


def test_a_stale_running_job_is_reported(old_version):  # noqa: F811
    rebuild._set_job("norssit", "running", "Indexing…")
    assert rebuild.publication_problems() == {"norssit": [STALE]}


def test_a_lock_without_a_live_worker_does_not_hide_a_stale_job(old_version):  # noqa: F811
    rebuild._set_job("norssit", "swapping", "Switching…")
    rebuild._acquire_lock("norssit")
    assert rebuild.publication_problems() == {"norssit": [STALE]}


def test_a_live_rebuild_is_not_reported_as_stale(old_version, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    monkeypatch.setattr(rebuild, "_worker", worker)
    rebuild._acquire_lock("norssit")
    rebuild._set_job("norssit", "swapping", "Switching…")
    record_path(old_version.dir).write_text("{}", encoding="utf-8")  # A normal publish holds it for a moment.
    try:
        assert rebuild.publication_problems() == {}
    finally:
        release.set()


def test_a_dataset_whose_project_does_not_load_gets_no_index_checks(old_version):  # noqa: F811
    rebuild.drop_index("norssit")
    (old_version.dir / "project.json").write_text("{", encoding="utf-8")
    rebuild.reload_projects()
    assert rebuild.publication_problems() == {}


def test_index_status_returns_the_problems(old_version, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    install_stubs(monkeypatch.setattr, rebuild, "new")
    rebuild.drop_index("norssit")
    monkeypatch.setattr(main, "check_drift", lambda project_id: {"stale": False})
    main.app.dependency_overrides[main.current_active_user] = lambda: ADMIN
    try:
        body = TestClient(main.app).get("/admin/index-status?dataset=norssit").json()
    finally:
        main.app.dependency_overrides.clear()
    assert body["problems"] == [MISSING]
