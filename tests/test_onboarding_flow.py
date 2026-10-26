"""The admin-page onboarding loop: start without project files, upload, Check, Rebuild."""

import json

import pytest

from sparql_llm.config import Settings, settings
from sparql_llm.indexing import preflight, rebuild
from sparql_llm.indexing.preflight import CheckResult, run_checks
from sparql_llm.project import get_projects, project_problems, reload_projects
from sparql_llm.project_files import save_upload

from .conftest import NORSSIT


@pytest.fixture
def empty_instance(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """An instance whose install settings list 'norssit' but which has no project files yet."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "projects", ["norssit"])
    reload_projects()
    yield tmp_path
    monkeypatch.undo()
    reload_projects()


def test_missing_project_file_does_not_stop_the_instance(empty_instance):
    assert get_projects() == []
    assert "project.json" in project_problems()["norssit"]
    assert settings.endpoints == []


def test_check_reports_the_missing_project_file(empty_instance):
    results = run_checks()["norssit"]
    assert results[0]["status"] == "error"
    assert "project.json" in results[0]["message"]


def test_check_reads_the_uploaded_project_file(empty_instance, monkeypatch: pytest.MonkeyPatch):
    seen = []
    monkeypatch.setattr(preflight, "run_preflight", lambda project: seen.append(project.label) or [])
    save_upload("norssit", "project.json", json.dumps({**NORSSIT, "label": "Norssit uploaded"}).encode(), "s@x")
    run_checks()
    assert seen == ["Norssit uploaded"]


def test_rebuild_uses_the_uploaded_project_file(norssit_project, monkeypatch: pytest.MonkeyPatch):
    seen = []

    def stop(project):
        seen.append(project.label)
        return [CheckResult("endpoint", "error", "stop here")]

    monkeypatch.setattr(rebuild, "run_preflight", stop)
    save_upload("norssit", "project.json", json.dumps({**NORSSIT, "label": "Norssit v2"}).encode(), "s@x")
    rebuild.run_rebuild_job()
    assert seen == ["Norssit v2"]
    # The rebuild stopped, so the instance keeps serving the configuration it had.
    assert get_projects()[0].label == "Norssit"


def test_server_url_follows_the_configured_port():
    assert Settings(server_port=8010).server_url == "http://127.0.0.1:8010"
    assert Settings().server_url == "http://127.0.0.1:8000"
