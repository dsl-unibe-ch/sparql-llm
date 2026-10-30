"""A normal stop ends a running rebuild quickly, up to the commit point and never after it."""

import threading
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from starlette.testclient import TestClient

from sparql_llm.agent import main
from sparql_llm.indexing import index_resources, rebuild

from .test_crash_safety import assert_serving, old_version, real_qdrant  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_stop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rebuild, "_worker", None)
    rebuild._stop.clear()
    yield
    rebuild._stop.clear()


def test_stop_cancels_a_rebuild_while_it_indexes(old_version, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    real_index = rebuild.init_vectordb
    indexing = threading.Event()

    def blocking_index(collection_name=None, projects=None, checkpoint=None):
        real_index(collection_name, projects)
        indexing.set()
        rebuild._stop.wait(5)
        checkpoint()
        raise AssertionError("the checkpoint should have stopped this rebuild")

    monkeypatch.setattr(rebuild, "init_vectordb", blocking_index)
    assert rebuild.start_rebuild("norssit")["started"]
    assert indexing.wait(5)

    assert rebuild.stop_rebuilds() is True

    job = rebuild.read_job("norssit")
    assert job["error"] == "cancelled"
    assert "Nothing changed" in job["message"]
    assert not rebuild.lock_file().exists()
    assert_serving("old", "failed")


def test_stop_does_not_cancel_after_the_commit_point(old_version, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    real_apply = rebuild.apply_files

    def stop_arrives_now(project_dir, files):
        rebuild._stop.set()
        real_apply(project_dir, files)

    monkeypatch.setattr(rebuild, "apply_files", stop_arrives_now)
    assert rebuild.start_rebuild("norssit")["started"]
    rebuild._worker.join(5)

    assert_serving("new", "done")


def test_stop_returns_false_for_a_thread_that_does_not_answer(old_version, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    blocked, release = threading.Event(), threading.Event()
    real_preflight = rebuild.run_preflight

    def stuck(project):
        blocked.set()
        release.wait(10)
        return real_preflight(project)

    monkeypatch.setattr(rebuild, "run_preflight", stuck)
    assert rebuild.start_rebuild("norssit")["started"]
    assert blocked.wait(5)
    try:
        assert rebuild.stop_rebuilds(timeout=0.1) is False
    finally:
        release.set()
        rebuild._worker.join(5)


def test_stop_with_no_rebuild_returns_true():
    assert rebuild.stop_rebuilds() is True


def test_recovery_clears_the_stop(norssit_project):
    rebuild._stop.set()
    rebuild.recover_after_restart()
    assert not rebuild._stop.is_set()


def test_init_vectordb_calls_the_checkpoint_every_64_documents(norssit_project, monkeypatch: pytest.MonkeyPatch):
    batches: list[list[int]] = []
    checkpoints = []

    class FakeClient:
        def collection_exists(self, name):
            return False

        def create_collection(self, **kwargs):
            pass

        def upsert(self, collection_name, points):
            batches.append(points.ids)

    monkeypatch.setattr(index_resources, "qdrant_client", FakeClient())
    monkeypatch.setattr(
        index_resources, "embedding_model", SimpleNamespace(embedding_size=4, embed=lambda texts: [[0.0] * 4 for _ in texts])
    )
    monkeypatch.setattr(
        index_resources, "build_project_docs", lambda project, prefixes: [Document(page_content=str(i)) for i in range(130)]
    )
    monkeypatch.setattr(index_resources, "general_info_docs", lambda projects: [])

    def checkpoint():
        checkpoints.append(len(batches))

    assert index_resources.init_vectordb("target", checkpoint=checkpoint) == 130

    assert checkpoints == [0, 1, 2]
    assert batches == [list(range(1, 65)), list(range(65, 129)), [129, 130]]


def test_lifespan_exit_calls_stop_rebuilds(norssit_project, monkeypatch: pytest.MonkeyPatch):
    stopped = []
    monkeypatch.setattr(main, "stop_rebuilds", lambda: stopped.append(True))
    with TestClient(main.app):
        assert stopped == []
    assert stopped == [True]
