"""A crash at any moment of a publish leaves one coherent version and a truthful job after restart.

These run against a real local Qdrant, because what matters is what is really left on disk.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from qdrant_client import QdrantClient, models

from sparql_llm.config import settings
from sparql_llm.indexing import rebuild
from sparql_llm.indexing.publication import GENERATION_FILE, read_generation, record_path
from sparql_llm.project_files import save_upload
from sparql_llm.utils import EndpointsMetadataManager

from .crash_stubs import install_stubs

ALIAS = "norssit__docs"
AFTER_COMMIT = ["_publish_alias", "apply_files", "write_generation", "reload_projects", "snapshot_meta", "_set_job_done", "clear_record"]


class Crash(BaseException):
    """Stands for a kill: nothing in the code under test may catch it."""


class _Clock(datetime):
    """Two rebuilds in one second would otherwise build into the same collection name."""

    ticks = 0

    @classmethod
    def now(cls, tz=None):
        cls.ticks += 1
        return datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=cls.ticks)


@pytest.fixture
def real_qdrant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = str(tmp_path / "vectordb")
    client = QdrantClient(path=path)
    monkeypatch.setattr(rebuild, "qdrant_client", client)
    monkeypatch.setattr(settings, "vectordb_url", path)
    monkeypatch.setattr(rebuild, "datetime", _Clock)
    yield client
    client.close()


@pytest.fixture
def old_version(norssit_project, real_qdrant, monkeypatch: pytest.MonkeyPatch):
    """Version "old" is live, and an upload of examples.md waits for the rebuild under test."""
    install_stubs(monkeypatch.setattr, rebuild, "old")
    rebuild.rebuild_index_with_alias("norssit")
    assert rebuild.read_job("norssit")["state"] == "done"
    (norssit_project.dir / "examples.md").write_text("old examples", encoding="utf-8")
    save_upload("norssit", "examples.md", b"new examples", "s@x")
    install_stubs(monkeypatch.setattr, rebuild, "new")
    return norssit_project


def assert_serving(version: str, job_state: str, dataset: str = "norssit") -> None:
    folder = Path(settings.data_dir) / dataset
    target = rebuild._alias_target(f"{dataset}__docs")
    points, _ = rebuild.qdrant_client.scroll(f"{dataset}__docs", limit=10, with_payload=True)
    assert {p.payload["version"] for p in points} == {version}
    assert (folder / "void.ttl").read_text(encoding="utf-8") == version
    assert [c for c in rebuild._collections() if c.startswith(f"{dataset}__v")] == [target]
    assert not record_path(folder).exists()
    assert not (folder / "next").exists()
    assert rebuild.read_job(dataset)["state"] == job_state
    assert read_generation(folder)["collection"] == target


def fail_once(monkeypatch: pytest.MonkeyPatch, step: str, error: BaseException, dataset: str = "norssit") -> None:
    """``step`` of ``rebuild`` raises ``error`` the first time it runs after the commit point, then behaves."""
    name = "_set_job" if step == "_set_job_done" else step
    real = getattr(rebuild, name)
    armed = [True]

    def wrapper(*args, **kwargs):
        due = armed[0] and rebuild.publication_pending(dataset) and (step != "_set_job_done" or args[1] == "done")
        if due:
            armed[0] = False
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(rebuild, name, wrapper)


def _crash(dataset: str = "norssit") -> None:
    with pytest.raises(Crash):
        rebuild.rebuild_index_with_alias(dataset)


def _waiting_upload(project) -> Path:
    return project.dir / "uploaded" / "examples.md"


def test_a_crash_before_the_record_leaves_the_old_version_serving(old_version, monkeypatch: pytest.MonkeyPatch):
    def crash(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(rebuild, "write_record", crash)
    _crash()
    rebuild.recover_after_restart()

    assert_serving("old", "failed")
    assert "interrupted" in rebuild.read_job("norssit")["message"]
    assert _waiting_upload(old_version).read_text(encoding="utf-8") == "new examples"


@pytest.mark.parametrize("step", AFTER_COMMIT)
def test_a_crash_after_the_commit_point_rolls_forward(step, old_version, monkeypatch: pytest.MonkeyPatch):
    fail_once(monkeypatch, step, Crash())
    _crash()
    rebuild.recover_after_restart()

    assert_serving("new", "done")
    assert "Publishing was finished after a restart." in rebuild.read_job("norssit")["message"]
    assert (old_version.dir / "examples.md").read_text(encoding="utf-8") == "new examples"
    assert (old_version.dir / "examples.md.prev").read_text(encoding="utf-8") == "old examples"


@pytest.mark.parametrize("step", ["_publish_alias", "apply_files"])
def test_a_disk_error_after_the_commit_point_says_so_and_rebuild_finishes_it(step, old_version, monkeypatch: pytest.MonkeyPatch):
    old_collection = rebuild._alias_target(ALIAS)
    voids = []
    real_void = rebuild.generate_void
    monkeypatch.setattr(rebuild, "generate_void", lambda project: voids.append(1) or real_void(project))
    fail_once(monkeypatch, step, OSError("No space left on device"))

    rebuild.run_rebuild_job("norssit")

    job = rebuild.read_job("norssit")
    assert job["state"] == "failed"
    assert "Publishing is unfinished" in job["message"]
    assert "previous index is still serving" not in job["message"]
    assert record_path(old_version.dir).exists()
    assert old_collection in rebuild._collections()

    rebuild.run_rebuild_job("norssit")

    assert_serving("new", "done")
    assert len(voids) == 1


def test_a_failed_alias_move_does_not_claim_the_new_index_is_live(old_version, monkeypatch: pytest.MonkeyPatch):
    fail_once(monkeypatch, "_publish_alias", OSError("No space left on device"))
    rebuild.run_rebuild_job("norssit")
    assert "is live" not in rebuild.read_job("norssit")["message"]


def test_a_commit_that_fails_before_the_record_drops_the_new_collection(old_version, monkeypatch: pytest.MonkeyPatch):
    def full_disk(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(rebuild, "write_record", full_disk)
    rebuild.run_rebuild_job("norssit")
    assert_serving("old", "failed")
    assert "previous index is still serving" in rebuild.read_job("norssit")["message"]


def test_recovery_twice_changes_nothing(old_version, monkeypatch: pytest.MonkeyPatch):
    fail_once(monkeypatch, "apply_files", Crash())
    _crash()
    rebuild.recover_after_restart()
    job = rebuild.read_job("norssit")
    rebuild.recover_after_restart()
    assert_serving("new", "done")
    assert rebuild.read_job("norssit") == job


def test_recovery_abandons_when_the_target_collection_is_gone(old_version, real_qdrant, monkeypatch: pytest.MonkeyPatch):
    fail_once(monkeypatch, "_publish_alias", Crash())
    _crash()
    target = json.loads(record_path(old_version.dir).read_text(encoding="utf-8"))["collection"]
    real_qdrant.delete_collection(target)

    rebuild.recover_after_restart()

    assert_serving("old", "failed")
    job = rebuild.read_job("norssit")
    assert "still serving" in job["message"]
    assert job["error"] == "abandoned"
    assert _waiting_upload(old_version).read_text(encoding="utf-8") == "new examples"


def test_one_stuck_dataset_does_not_stop_the_others(use_projects, real_qdrant, monkeypatch: pytest.MonkeyPatch):
    use_projects(
        {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"},
        {"id": "b", "label": "Beta", "endpoint_url": "https://b.example/sparql"},
    )
    install_stubs(monkeypatch.setattr, rebuild, "new")
    real_apply = rebuild.apply_files

    def crash(project_dir, files):
        raise Crash

    monkeypatch.setattr(rebuild, "apply_files", crash)
    _crash("a")
    _crash("b")

    def stuck_on_a(project_dir, files):
        if project_dir.name == "a":
            raise OSError("No space left on device")
        real_apply(project_dir, files)

    monkeypatch.setattr(rebuild, "apply_files", stuck_on_a)
    rebuild.recover_after_restart()

    assert_serving("new", "done", dataset="b")
    assert rebuild.read_job("a")["state"] == "failed"
    assert "Publishing is unfinished" in rebuild.read_job("a")["message"]
    assert rebuild.publication_pending("a")


def test_an_unreadable_record_does_not_stop_recovery(old_version):
    record_path(old_version.dir).write_text("{", encoding="utf-8")

    rebuild.recover_after_restart()

    job = rebuild.read_job("norssit")
    assert job["state"] == "failed"
    assert "norssit/publish.json" in job["message"]
    assert record_path(old_version.dir).read_text(encoding="utf-8") == "{"
    assert rebuild.publication_pending("norssit")


def test_rebuild_on_an_unreadable_record_fails_with_a_plain_message(old_version):
    record_path(old_version.dir).write_text("{", encoding="utf-8")
    rebuild.run_rebuild_job("norssit")
    job = rebuild.read_job("norssit")
    assert job["state"] == "failed"
    assert "could not be read" in job["message"]
    assert "traceback" not in job


def test_recovery_removes_a_half_built_collection_and_next(old_version, real_qdrant):
    real_qdrant.create_collection(
        "norssit__v20991231-235959", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE)
    )
    (old_version.dir / "next").mkdir()
    rebuild._set_job("norssit", "running", "Building…")

    rebuild.recover_after_restart()

    assert_serving("old", "failed")
    assert "Nothing changed" in rebuild.read_job("norssit")["message"]


def test_recovery_relinks_a_lost_alias_from_the_generation(old_version, real_qdrant):
    published = rebuild._alias_target(ALIAS)
    real_qdrant.update_collection_aliases(
        change_aliases_operations=[models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=ALIAS))]
    )
    real_qdrant.create_collection(
        "norssit__v20991231-235959", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE)
    )

    rebuild.recover_after_restart()

    assert rebuild._alias_target(ALIAS) == published
    assert_serving("old", "done")


def test_recovery_removes_the_collections_of_a_dataset_never_published(norssit_project, real_qdrant):
    real_qdrant.create_collection(
        "norssit__v20991231-235959", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE)
    )
    rebuild.recover_after_restart()
    assert rebuild._collections() == []


def test_recovery_touches_nothing_when_a_published_dataset_lost_its_alias_and_generation(old_version, real_qdrant):
    collection = rebuild._alias_target(ALIAS)
    real_qdrant.update_collection_aliases(
        change_aliases_operations=[models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=ALIAS))]
    )
    (old_version.dir / GENERATION_FILE).unlink()

    rebuild.recover_after_restart()

    assert rebuild._collections() == [collection]
    assert rebuild._alias_target(ALIAS) is None


def test_recovery_makes_no_network_call(old_version, monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise AssertionError("network call")

    fail_once(monkeypatch, "apply_files", Crash())
    _crash()
    metadata = EndpointsMetadataManager(auto_init=True)
    monkeypatch.setattr(rebuild, "endpoints_metadata", metadata)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)

    rebuild.recover_after_restart()
    metadata.prefixes_map  # the first use after the reload

    assert_serving("new", "done")


def _kill_at(step: str, data_dir: Path) -> None:
    child = Path(__file__).parent / "crash_child.py"
    done = subprocess.run([sys.executable, str(child), str(data_dir), step], env={**os.environ}, capture_output=True, text=True)
    assert done.returncode == 137, done.stderr


def _after_the_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> QdrantClient:
    path = str(tmp_path / "vectordb")
    client = QdrantClient(path=path)
    monkeypatch.setattr(rebuild, "qdrant_client", client)
    monkeypatch.setattr(settings, "vectordb_url", path)
    rebuild.recover_after_restart()
    return client


def test_a_killed_process_is_recovered(norssit_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _kill_at("write_generation", tmp_path)
    client = _after_the_kill(tmp_path, monkeypatch)
    try:
        assert_serving("new", "done")
    finally:
        client.close()


def test_a_process_killed_before_the_commit_point_changes_nothing(norssit_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _kill_at("write_record", tmp_path)
    assert (tmp_path / "norssit" / "next").is_dir()
    client = _after_the_kill(tmp_path, monkeypatch)
    try:
        assert rebuild.read_job("norssit")["error"] == "interrupted"
        assert not (tmp_path / "norssit" / "next").exists()
        assert rebuild._collections() == []
    finally:
        client.close()
