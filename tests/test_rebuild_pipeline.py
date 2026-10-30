"""The admin rebuild runs the onboarding pipeline and never swaps in an index after an error."""

import pytest

from sparql_llm.indexing import rebuild
from sparql_llm.indexing.drift import Fingerprint
from sparql_llm.indexing.preflight import CheckResult


@pytest.fixture
def pipeline(norssit_project, monkeypatch: pytest.MonkeyPatch):
    """Every external step of the rebuild replaced by a recorder."""
    calls: list[str] = []

    class FakeMetadata:
        prefixes_map: dict = {}

        def __init__(self, projects=None, auto_init=False):
            pass

        def refresh(self):
            calls.append("refresh")

        def reload(self):
            calls.append("metadata reload")

    class FakeQdrant:
        def delete_collection(self, name):
            calls.append(f"delete {name}")

    def record(name, result=None):
        def step(*args, **kwargs):
            calls.append(name)
            return result

        return step

    ok = [CheckResult("endpoint", "ok", "Norssit: the endpoint answers.")]
    monkeypatch.setattr(rebuild, "run_preflight", record("preflight", ok))
    monkeypatch.setattr(
        rebuild, "sync_curator_examples", record("sync", {"skipped": True, "accepted": 0, "rejected": [], "error": ""})
    )
    monkeypatch.setattr(
        rebuild,
        "generate_void",
        record("void", {"triples": 9, "entities": 3, "classes_total": 330, "classes_kept": 40, "predicates": 5}),
    )
    monkeypatch.setattr(rebuild, "EndpointsMetadataManager", FakeMetadata)
    monkeypatch.setattr(rebuild, "endpoints_metadata", FakeMetadata())
    monkeypatch.setattr(rebuild, "build_entity_names", record("entities", {"entities": 12, "error": ""}))
    monkeypatch.setattr(
        rebuild,
        "take_fingerprint",
        record("fingerprint", Fingerprint(triples=9, classes={"http://xmlns.com/foaf/0.1/Person": 10137})),
    )
    monkeypatch.setattr(rebuild, "init_vectordb", record("index", 42))
    monkeypatch.setattr(rebuild, "_collections", lambda: [])
    monkeypatch.setattr(rebuild, "_point_alias", record("swap"))
    monkeypatch.setattr(rebuild, "qdrant_client", FakeQdrant())
    monkeypatch.setattr(rebuild, "reload_projects", record("reload", [norssit_project]))
    return calls


def test_pipeline_runs_in_order(pipeline):
    rebuild.rebuild_index_with_alias("norssit")
    assert pipeline[:4] == ["preflight", "sync", "void", "refresh"]
    last_reload = max(i for i, call in enumerate(pipeline) if call == "reload")
    assert pipeline.index("entities") < pipeline.index("index") < pipeline.index("swap") < last_reload


def test_rebuild_stops_before_swap_on_preflight_error(pipeline, monkeypatch: pytest.MonkeyPatch):
    error = CheckResult("endpoint", "error", "Norssit: the endpoint https://ldf.fi/norssit/sparql did not answer.")
    monkeypatch.setattr(rebuild, "run_preflight", lambda project: [error])
    rebuild.run_rebuild_job("norssit")
    assert "swap" not in pipeline and "index" not in pipeline
    job = rebuild.read_job("norssit")
    assert job["state"] == "failed"
    assert "did not answer" in job["message"]
    assert "traceback" not in job


def test_rebuild_stops_when_the_schema_cannot_be_read(pipeline, monkeypatch: pytest.MonkeyPatch):
    from sparql_llm.indexing.void_gen import VoidError

    def broken(project):
        raise VoidError("The generated schema for Norssit could not be read back: bad name")

    monkeypatch.setattr(rebuild, "generate_void", broken)
    rebuild.run_rebuild_job("norssit")
    assert "swap" not in pipeline
    assert "could not be read back" in rebuild.read_job("norssit")["message"]


def test_rebuild_reloads_projects_after_success(pipeline):
    rebuild.run_rebuild_job("norssit")
    assert pipeline[-1] == "reload" or "reload" in pipeline[pipeline.index("swap") :]
    job = rebuild.read_job("norssit")
    assert job["state"] == "done"
    assert "Norssit: Person 10,137 · 330 → 40 classes" in job["message"]
    assert job["projects"]["norssit"]["void"]["classes_kept"] == 40


def test_job_status_write_retries_a_briefly_locked_file(norssit_project, monkeypatch: pytest.MonkeyPatch):
    # On Windows, replacing a file that a status poll has open is refused for a moment.
    real_replace = rebuild.os.replace
    attempts = []

    def flaky(src, dst):
        attempts.append(dst)
        if len(attempts) == 1:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(rebuild.os, "replace", flaky)
    monkeypatch.setattr(rebuild.time, "sleep", lambda s: None)
    rebuild._set_job("norssit", "running", "Checking…")
    assert len(attempts) == 2
    assert rebuild.read_job("norssit")["message"] == "Checking…"


# ── Commit order: nothing live is lost, and a failure says what is serving ────


def test_a_failed_index_build_drops_its_half_built_collection(pipeline, monkeypatch: pytest.MonkeyPatch):
    created: list[str] = []

    def broken_index(collection_name, projects):
        created.append(collection_name)
        raise RuntimeError("upsert failed")

    monkeypatch.setattr(rebuild, "init_vectordb", broken_index)
    monkeypatch.setattr(rebuild, "_collections", lambda: list(created))
    monkeypatch.setattr(rebuild, "_alias_target", lambda alias: None)
    rebuild.run_rebuild_job("norssit")
    assert created and f"delete {created[0]}" in pipeline
    assert "previous index is still serving" in rebuild.read_job("norssit")["message"]


def test_the_fingerprint_is_written_with_the_other_files_before_the_swap(pipeline, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rebuild, "save_fingerprint", lambda fp, project: pipeline.append("save fingerprint"))
    rebuild.rebuild_index_with_alias("norssit")
    assert pipeline.index("save fingerprint") < pipeline.index("swap")


def test_old_collections_are_retired_only_after_the_files_are_live(pipeline, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rebuild, "_collections", lambda: ["norssit__v20200101-000000"])
    monkeypatch.setattr(rebuild, "apply_files", lambda project_dir, files: pipeline.append("promote"))
    rebuild.rebuild_index_with_alias("norssit")
    assert pipeline.index("swap") < pipeline.index("promote") < pipeline.index("delete norssit__v20200101-000000")


def test_a_failure_after_the_swap_keeps_the_built_files_and_says_the_new_index_is_live(
    pipeline, norssit_project, monkeypatch: pytest.MonkeyPatch
):
    def full_disk(project_dir, files):
        raise OSError("No space left on device")

    monkeypatch.setattr(rebuild, "apply_files", full_disk)
    monkeypatch.setattr(rebuild, "_collections", lambda: ["norssit__v20200101-000000"])
    rebuild.run_rebuild_job("norssit")

    job = rebuild.read_job("norssit")
    assert job["state"] == "failed"
    assert "previous index is still serving" not in job["message"]
    assert "is live" in job["message"]
    assert norssit_project.build_dir.is_dir()
    # The old index stays: its files are still the live ones.
    assert "delete norssit__v20200101-000000" not in pipeline
