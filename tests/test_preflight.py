"""Preflight checks: a misconfigured project is reported in plain words before anything is rebuilt."""

import json

import httpx

from sparql_llm.indexing.preflight import run_preflight

NORSSIT = "https://ldf.fi/norssit/sparql"


def _ask(value: bool) -> dict:
    return {"results": {"bindings": [{"ask-variable": {"value": str(value).lower()}}]}}


def fake_fetch(empty: tuple[str, ...] = (), failing_examples: tuple[str, ...] = ()):
    """ASK queries answer true unless they mention one of ``empty``; SELECTs return a row."""

    def fetch(query, endpoint_url, **kwargs):
        if query.lstrip().upper().startswith("ASK"):
            return _ask(not any(marker in query for marker in empty))
        if any(marker in query for marker in failing_examples):
            return {"results": {"bindings": []}}
        return {"results": {"bindings": [{"n": {"value": "1"}}]}}

    return fetch


def _by_step(results):
    return {r.step: r for r in results}


def test_a_healthy_project_passes(norssit_project):
    results = run_preflight(norssit_project, fetch=fake_fetch())
    assert [r.step for r in results] == ["endpoint", "graphs", "entities", "files", "examples"]
    assert all(r.status == "ok" for r in results), [r.message for r in results]
    assert "6/6" in _by_step(results)["examples"].message


def test_endpoint_html_response_is_one_plain_error(norssit_project):
    def html(query, endpoint_url, **kwargs):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    results = run_preflight(norssit_project, fetch=html)
    assert len(results) == 1
    error = results[0]
    assert (error.step, error.status) == ("endpoint", "error")
    assert NORSSIT in error.message
    assert "Traceback" not in error.message and "JSONDecodeError" not in error.message


def test_endpoint_timeout_is_one_plain_error(norssit_project):
    def slow(query, endpoint_url, **kwargs):
        raise httpx.ReadTimeout("timed out")

    results = run_preflight(norssit_project, fetch=slow)
    assert len(results) == 1
    assert "did not answer in time" in results[0].message


def test_empty_graph_is_error_naming_field(swiss_project):
    results = _by_step(run_preflight(swiss_project, fetch=fake_fetch(empty=("GRAPH <https://swiss-elites",))))
    assert results["graphs"].status == "error"
    assert "<https://swiss-elites.lod4hss.cloud/resource/>" in results["graphs"].message
    assert "graphs" in results["graphs"].message


def test_missing_entity_instances_is_error(norssit_project):
    results = _by_step(run_preflight(norssit_project, fetch=fake_fetch(empty=("foaf/0.1/Organization",))))
    assert results["entities"].status == "error"
    assert "entities.types.Group" in results["entities"].message


def test_missing_dataset_file_is_error(norssit_project):
    norssit_project.dataset_path.unlink()
    results = _by_step(run_preflight(norssit_project, fetch=fake_fetch()))
    assert results["files"].status == "error"
    assert "dataset.md" in results["files"].message


def test_examples_file_without_examples_is_error(norssit_project):
    norssit_project.examples_path.write_text("# Nothing here yet\n", encoding="utf-8")
    results = _by_step(run_preflight(norssit_project, fetch=fake_fetch()))
    assert results["files"].status == "error"
    assert "examples.md" in results["files"].message


def test_failing_example_is_warning_not_error(norssit_project):
    results = _by_step(run_preflight(norssit_project, fetch=fake_fetch(failing_examples=("waltari",))))
    assert results["examples"].status == "warning"
    assert "Find a person by name" in results["examples"].message
    assert "5/6" in results["examples"].message
