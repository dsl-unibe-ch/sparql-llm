"""Generated files live in each project's own folder; app-wide job files in the data folder."""

import pytest

from sparql_llm import utils
from sparql_llm.indexing import drift
from sparql_llm.indexing.drift import Fingerprint, load_fingerprint, save_fingerprint, take_fingerprint
from sparql_llm.indexing.rebuild import job_file, lock_file
from sparql_llm.indexing.sync_examples import sync_curator_examples
from sparql_llm.utils import EndpointsMetadataManager

PERSON = "http://xmlns.com/foaf/0.1/Person"


def _recording_fetch(queries: list[str]):
    def fetch(query, endpoint_url, **kwargs):
        queries.append(query)
        return {"results": {"bindings": []}}

    return fetch


def test_generated_files_land_in_project_dir(norssit_project, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(utils, "get_schema_for_endpoint", lambda url, void_file=None: {PERSON: {"p": []}})
    monkeypatch.setattr(
        utils, "get_prefixes_for_endpoint", lambda url, examples_file=None, prefixes_map=None: prefixes_map or {}
    )
    manager = EndpointsMetadataManager([norssit_project], auto_init=False)
    manager.refresh()
    assert norssit_project.prefixes_path == norssit_project.dir / "prefixes.json"
    assert norssit_project.prefixes_path.is_file()
    assert PERSON in manager.void_dict["https://ldf.fi/norssit/sparql"]

    save_fingerprint(Fingerprint(triples=3), norssit_project)
    assert (norssit_project.dir / "fingerprint.json").is_file()
    assert load_fingerprint(norssit_project).triples == 3


def test_job_files_live_in_the_data_dir(norssit_project):
    assert job_file("norssit") == norssit_project.dir / "reindex_job.json"
    assert lock_file() == norssit_project.dir.parent / "reindex.lock"


def test_fingerprint_query_without_graphs_has_no_graph_clause(norssit_project):
    queries: list[str] = []
    take_fingerprint(norssit_project, fetch=_recording_fetch(queries))
    assert queries
    assert not any("GRAPH" in q for q in queries)


def test_fingerprint_query_with_graph_uses_it(swiss_project):
    queries: list[str] = []
    take_fingerprint(swiss_project, fetch=_recording_fetch(queries))
    assert all("GRAPH <https://swiss-elites.lod4hss.cloud/resource/>" in q for q in queries)


def test_fingerprint_query_with_several_graphs_uses_values(use_projects):
    project = use_projects(
        {"id": "two", "label": "Two", "endpoint_url": "https://x/sparql", "graphs": ["https://g/1", "https://g/2"]}
    )[0]
    queries: list[str] = []
    take_fingerprint(project, fetch=_recording_fetch(queries))
    assert all("GRAPH ?g" in q and "VALUES ?g { <https://g/1> <https://g/2> }" in q for q in queries)


def test_drift_check_defaults_to_the_first_project(norssit_project, monkeypatch: pytest.MonkeyPatch):
    seen = []
    monkeypatch.setattr(drift, "take_fingerprint", lambda project: seen.append(project.id) or Fingerprint())
    drift.check_drift()
    assert seen == ["norssit"]


def test_sync_skipped_without_curator_config(norssit_project):
    report = sync_curator_examples(norssit_project)
    assert report["skipped"] is True
    assert not norssit_project.synced_examples_path.exists()
