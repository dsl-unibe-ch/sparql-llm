"""A publish commits through one durable record, and its file moves can be replayed after any failure."""

import json
import os
import stat
from pathlib import Path

import pytest

from sparql_llm.indexing import publication
from sparql_llm.project import BUILD_DIR, UPLOADED_DIR, load_project

from .conftest import NORSSIT


def _tree(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): p.read_text(encoding="utf-8") for p in folder.rglob("*") if p.is_file()}


def _stage(project) -> dict[str, list[str]]:
    """A finished build: four generated files and one upload over the live examples.md."""
    (project.dir / "void.ttl").write_text("old void", encoding="utf-8")
    (project.dir / "examples.md").write_text("old examples", encoding="utf-8")
    build = project.dir / BUILD_DIR
    build.mkdir()
    for name in ("void.ttl", "prefixes.json", "entity_names.json", "fingerprint.json"):
        (build / name).write_text(f"new {name}", encoding="utf-8")
    uploaded = project.dir / UPLOADED_DIR
    uploaded.mkdir()
    (uploaded / "examples.md").write_text("new examples", encoding="utf-8")
    return publication.files_for(load_project(project.dir.parent, project.id, candidate=True))


def test_durable_json_replaces_atomically_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "publish.json"
    path.write_text("old", encoding="utf-8")
    publication.write_durable_json(path, {"Väkkärä": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"Väkkärä": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["publish.json"]


def test_durable_json_retries_a_briefly_locked_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    real_replace = publication.os.replace
    attempts = []

    def flaky(src, dst):
        attempts.append(dst)
        if len(attempts) == 1:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(publication.os, "replace", flaky)
    monkeypatch.setattr(publication.time, "sleep", lambda s: None)
    publication.write_durable_json(tmp_path / "publish.json", {"a": 1})
    assert len(attempts) == 2


def test_read_record_absent_is_none(tmp_path):
    assert publication.read_record(tmp_path) is None


def test_read_record_returns_what_was_written_and_clear_removes_it(tmp_path):
    record = {"alias": "a__docs", "collection": "a__v1", "files": {}, "job": {}, "job_message": "Done."}
    publication.write_record(tmp_path, record)
    assert publication.read_record(tmp_path) == record
    publication.clear_record(tmp_path)
    assert publication.read_record(tmp_path) is None
    publication.clear_record(tmp_path)  # replayed after a crash


@pytest.mark.parametrize("content", ["{", "[]", '{"collection": "a__v1"}'])
def test_read_record_unreadable_raises_publication_error(tmp_path, content):
    publication.record_path(tmp_path).write_text(content, encoding="utf-8")
    with pytest.raises(publication.PublicationError):
        publication.read_record(tmp_path)


def test_generation_round_trip_and_unreadable_is_none(tmp_path):
    assert publication.read_generation(tmp_path) is None
    publication.write_generation(tmp_path, "a__v1")
    assert publication.read_generation(tmp_path)["collection"] == "a__v1"
    assert "committed_at" in publication.read_generation(tmp_path)
    (tmp_path / publication.GENERATION_FILE).write_text("{", encoding="utf-8")
    assert publication.read_generation(tmp_path) is None


def test_files_for_lists_built_uploaded_and_unconfigured(norssit_project):
    files = _stage(norssit_project)
    assert files == {
        "generated": ["void.ttl", "prefixes.json", "entity_names.json", "fingerprint.json"],
        "uploaded": ["examples.md"],
        "remove": ["examples-synced.md"],
    }


def test_files_for_a_candidate_without_entities_removes_the_entity_names(norssit_project):
    _stage(norssit_project)
    plain = {k: v for k, v in NORSSIT.items() if k != "entities"}
    (norssit_project.dir / UPLOADED_DIR / "project.json").write_text(json.dumps(plain), encoding="utf-8")
    files = publication.files_for(load_project(norssit_project.dir.parent, "norssit", candidate=True))
    assert sorted(files["uploaded"]) == ["examples.md", "project.json"]
    assert files["remove"] == ["entity_names.json", "examples-synced.md"]


def test_apply_files_moves_generated_and_uploaded_and_keeps_prev(norssit_project):
    files = _stage(norssit_project)
    (norssit_project.dir / "examples-synced.md").write_text("stale", encoding="utf-8")
    publication.apply_files(norssit_project.dir, files)

    live = norssit_project.dir
    assert (live / "void.ttl").read_text(encoding="utf-8") == "new void.ttl"
    assert (live / "examples.md").read_text(encoding="utf-8") == "new examples"
    assert (live / "examples.md.prev").read_text(encoding="utf-8") == "old examples"
    assert not (live / "examples-synced.md").exists()
    assert not (live / BUILD_DIR).exists()
    assert not (live / UPLOADED_DIR).exists()


def test_apply_files_twice_changes_nothing(norssit_project):
    files = _stage(norssit_project)
    publication.apply_files(norssit_project.dir, files)
    once = _tree(norssit_project.dir)
    publication.apply_files(norssit_project.dir, files)
    assert _tree(norssit_project.dir) == once
    assert (norssit_project.dir / "examples.md.prev").read_text(encoding="utf-8") == "old examples"


@pytest.mark.parametrize("fail_at", range(1, 7))
def test_apply_files_resumes_after_failing_at_any_move(norssit_project, monkeypatch: pytest.MonkeyPatch, fail_at):
    files = _stage(norssit_project)
    real_replace = publication.os.replace
    calls = []

    def fails_once(src, dst):
        calls.append(dst)
        if len(calls) == fail_at:
            raise OSError("No space left on device")
        real_replace(src, dst)

    monkeypatch.setattr(publication.os, "replace", fails_once)
    with pytest.raises(OSError):
        publication.apply_files(norssit_project.dir, files)
    publication.apply_files(norssit_project.dir, files)

    live = _tree(norssit_project.dir)
    assert live["void.ttl"] == "new void.ttl"
    assert live["prefixes.json"] == "new prefixes.json"
    assert live["entity_names.json"] == "new entity_names.json"
    assert live["fingerprint.json"] == "new fingerprint.json"
    assert live["examples.md"] == "new examples"
    assert live["examples.md.prev"] == "old examples"
    assert not any(name.startswith((BUILD_DIR, UPLOADED_DIR)) for name in live)


def test_apply_files_resumes_when_the_old_file_moved_but_the_upload_did_not(norssit_project):
    files = _stage(norssit_project)
    live = norssit_project.dir
    (live / "examples.md").replace(live / "examples.md.prev")
    publication.apply_files(live, files)
    assert (live / "examples.md").read_text(encoding="utf-8") == "new examples"
    assert (live / "examples.md.prev").read_text(encoding="utf-8") == "old examples"


def _count_flushed_files(monkeypatch: pytest.MonkeyPatch, module) -> list[int]:
    flushed: list[int] = []
    real_fsync = module.os.fsync

    def fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            flushed.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fsync)
    return flushed


def test_sync_files_flushes_every_built_and_uploaded_file(norssit_project, monkeypatch: pytest.MonkeyPatch):
    _stage(norssit_project)
    flushed = _count_flushed_files(monkeypatch, publication)
    publication.sync_files(norssit_project.dir)
    assert len(flushed) == 5


def test_sync_files_with_nothing_built_does_nothing(tmp_path):
    publication.sync_files(tmp_path)


def test_a_snapshot_of_the_vector_store_is_flushed(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from sparql_llm.indexing.qdrant_meta import snapshot_meta

    (tmp_path / "meta.json").write_text('{"collections": {}, "aliases": {}}', encoding="utf-8")
    flushed = _count_flushed_files(monkeypatch, publication)
    assert snapshot_meta(tmp_path) is True
    assert len(flushed) == 1


def test_the_dataset_list_is_written_durably(norssit_project, monkeypatch: pytest.MonkeyPatch):
    from sparql_llm import datasets

    flushed = _count_flushed_files(monkeypatch, publication)
    datasets.set_published("norssit", False)
    assert len(flushed) == 1
    assert json.loads((norssit_project.dir.parent / "datasets.json").read_text(encoding="utf-8")) == [
        {"id": "norssit", "published": False}
    ]
