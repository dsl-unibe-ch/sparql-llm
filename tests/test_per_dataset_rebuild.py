"""Check and Rebuild work on one dataset at a time; a broken dataset never touches another."""

import json
from pathlib import Path

import pytest

from sparql_llm.indexing import preflight, rebuild
from sparql_llm.indexing.drift import Fingerprint
from sparql_llm.indexing.preflight import CheckResult

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}
B = {"id": "b", "label": "Beta", "endpoint_url": "https://b.example/sparql"}


@pytest.fixture
def two(use_projects, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Datasets a and b, both live, with every external rebuild step faked to write where the real ones would."""
    projects = {p.id: p for p in use_projects(A, B)}
    for project in projects.values():
        project.void_path.write_text(f"# live void of {project.id}\n", encoding="utf-8")
    swaps: list[tuple[str, str]] = []

    class FakeMetadata:
        def __init__(self, projects=None, auto_init=False):
            self.projects = projects

        def refresh(self):
            for p in self.projects:
                p.prefixes_path.write_text('{"prefixes_map": {}, "classes_schema": {}}', encoding="utf-8")

        def reload(self):
            pass

    class FakeQdrant:
        def delete_collection(self, name):
            swaps.append(("delete", name))

    def void(project):
        project.void_path.write_text(f"# new void of {project.id}\n", encoding="utf-8")
        return {"triples": 1, "entities": 1, "classes_total": 1, "classes_kept": 1, "predicates": 1}

    monkeypatch.setattr(rebuild, "run_preflight", lambda p: [CheckResult("endpoint", "ok", "fine")])
    monkeypatch.setattr(rebuild, "sync_curator_examples", lambda p: {"skipped": True, "accepted": 0, "rejected": [], "error": ""})
    monkeypatch.setattr(rebuild, "generate_void", void)
    monkeypatch.setattr(rebuild, "EndpointsMetadataManager", FakeMetadata)
    monkeypatch.setattr(rebuild, "endpoints_metadata", FakeMetadata())
    monkeypatch.setattr(rebuild, "build_entity_names", lambda p: {"entities": 0, "error": ""})
    monkeypatch.setattr(rebuild, "take_fingerprint", lambda p: Fingerprint(triples=1))
    monkeypatch.setattr(rebuild, "init_vectordb", lambda collection_name=None, projects=None, prefixes_map=None: 3)
    monkeypatch.setattr(rebuild, "_collections", lambda: [])
    monkeypatch.setattr(rebuild, "_aliases", lambda: set())
    monkeypatch.setattr(rebuild, "_point_alias", lambda alias, target: swaps.append((alias, target)))
    monkeypatch.setattr(rebuild, "qdrant_client", FakeQdrant())
    projects["swaps"] = swaps
    return projects


def _snapshot(project) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(project.dir.iterdir()) if p.is_file()}


def test_rebuilding_b_leaves_a_untouched(two, monkeypatch: pytest.MonkeyPatch):
    before = _snapshot(two["a"])

    rebuild.run_rebuild_job("b")
    assert rebuild.read_job("b")["state"] == "done", rebuild.read_job("b")["message"]
    assert [s for s in two["swaps"] if s[0] != "delete"] == [("b__docs", two["swaps"][0][1])]
    assert two["swaps"][0][1].startswith("b__v")
    assert (two["b"].dir / "void.ttl").read_text(encoding="utf-8") == "# new void of b\n"

    monkeypatch.setattr(rebuild, "init_vectordb", lambda collection_name=None, projects=None, prefixes_map=None: 0)
    rebuild.run_rebuild_job("b")
    assert rebuild.read_job("b")["state"] == "failed"

    assert _snapshot(two["a"]) == before
    assert not rebuild.job_file("a").exists()
    assert all(s[0] == "b__docs" for s in two["swaps"] if s[0] != "delete")


def test_failed_check_of_b_does_not_block_a(two, monkeypatch: pytest.MonkeyPatch):
    def preflight_of(project):
        if project.id == "b":
            return [CheckResult("endpoint", "error", "Beta: the endpoint did not answer.")]
        return [CheckResult("endpoint", "ok", "Alpha: fine.")]

    monkeypatch.setattr(preflight, "run_preflight", preflight_of)
    monkeypatch.setattr(rebuild, "run_preflight", preflight_of)

    assert preflight.run_checks("b")[0]["status"] == "error"
    assert [r["status"] for r in preflight.run_checks("a")] == ["ok"]
    rebuild.run_rebuild_job("b")
    rebuild.run_rebuild_job("a")
    assert rebuild.read_job("b")["state"] == "failed"
    assert rebuild.read_job("a")["state"] == "done"


def test_second_rebuild_waits_for_the_first(two, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rebuild.threading, "Thread", lambda **kwargs: type("T", (), {"start": lambda self: None})())

    assert rebuild.start_rebuild("a")["started"] is True
    second = rebuild.start_rebuild("b")

    assert second == {**second, "started": False, "reason": "already-running", "dataset": "a"}
    rebuild._release_lock()


def test_first_successful_rebuild_makes_a_new_dataset_ready_not_published(two):
    from sparql_llm.datasets import add_dataset, dataset_state
    from sparql_llm.project_files import save_upload

    add_dataset("c", "Gamma")
    save_upload("c", "project.json", json.dumps({"id": "c", "label": "Gamma", "endpoint_url": "https://c.example/sparql"}).encode(), "s@x")
    assert dataset_state("c") == "new"

    rebuild.run_rebuild_job("c")

    assert rebuild.read_job("c")["state"] == "done", rebuild.read_job("c")["message"]
    assert dataset_state("c") == "ready"


def test_undo_cannot_bring_back_another_datasets_endpoint(two):
    """GPT review I1: Undo staged a previous project.json without the endpoint-uniqueness check, and Rebuild promoted it."""
    from sparql_llm.project_files import ProjectFileError, restore_previous, save_upload

    a_dir = two["a"].dir
    (a_dir / "project.json.prev").write_text(json.dumps({**A, "endpoint_url": B["endpoint_url"]}), encoding="utf-8")
    with pytest.raises(ProjectFileError, match="is already used by dataset 'b'"):
        restore_previous("a", "project.json")

    # However the duplicate got staged, Check and Rebuild refuse it.
    save_upload("a", "project.json", json.dumps({**A, "endpoint_url": B["endpoint_url"]}).encode(), "s@x")
    assert "is already used by dataset 'b'" in preflight.run_checks("a")[0]["message"]
    rebuild.run_rebuild_job("a")
    assert rebuild.read_job("a")["state"] == "failed"
    assert json.loads((a_dir / "project.json").read_text(encoding="utf-8"))["endpoint_url"] == A["endpoint_url"]
