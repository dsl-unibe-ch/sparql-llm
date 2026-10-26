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
        def refresh(self):
            calls.append("refresh")

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
    rebuild.rebuild_index_with_alias()
    # The uploaded project files are read first, then checked.
    assert pipeline[:5] == ["reload", "preflight", "sync", "void", "refresh"]
    last_reload = max(i for i, call in enumerate(pipeline) if call == "reload")
    assert pipeline.index("entities") < pipeline.index("index") < pipeline.index("swap") < last_reload


def test_rebuild_stops_before_swap_on_preflight_error(pipeline, monkeypatch: pytest.MonkeyPatch):
    error = CheckResult("endpoint", "error", "Norssit: the endpoint https://ldf.fi/norssit/sparql did not answer.")
    monkeypatch.setattr(rebuild, "run_preflight", lambda project: [error])
    rebuild.run_rebuild_job()
    assert "swap" not in pipeline and "index" not in pipeline
    job = rebuild.read_job()
    assert job["state"] == "failed"
    assert "did not answer" in job["message"]
    assert "traceback" not in job


def test_rebuild_stops_when_the_schema_cannot_be_read(pipeline, monkeypatch: pytest.MonkeyPatch):
    from sparql_llm.indexing.void_gen import VoidError

    def broken(project):
        raise VoidError("The generated schema for Norssit could not be read back: bad name")

    monkeypatch.setattr(rebuild, "generate_void", broken)
    rebuild.run_rebuild_job()
    assert "swap" not in pipeline
    assert "could not be read back" in rebuild.read_job()["message"]


def test_rebuild_reloads_projects_after_success(pipeline):
    rebuild.run_rebuild_job()
    assert pipeline[-1] == "reload" or "reload" in pipeline[pipeline.index("swap") :]
    job = rebuild.read_job()
    assert job["state"] == "done"
    assert "Norssit: Person 10,137 · 330 → 40 classes" in job["message"]
    assert job["projects"]["norssit"]["void"]["classes_kept"] == 40
