"""/logs holds every user's questions: it needs its key, and is closed when no key is configured."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sparql_llm.agent import main
from sparql_llm.config import settings


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log = tmp_path / "user_questions.log"
    log.write_text("2026-10-27 10:00:00,000 - User question: a private question\n", encoding="utf-8")
    monkeypatch.setattr(settings, "logs_filepath", str(log))
    return TestClient(main.app, raise_server_exceptions=False)


def test_without_a_configured_key_the_logs_are_closed(logs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "logs_api_key", "")
    resp = logs.post("/logs", json={"api_key": ""})
    assert resp.status_code == 403
    assert "private question" not in resp.text


def test_a_wrong_key_is_refused(logs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "logs_api_key", "right-key")
    resp = logs.post("/logs", json={"api_key": "wrong"})
    assert resp.status_code == 401


def test_the_right_key_reads_the_questions(logs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "logs_api_key", "right-key")
    resp = logs.post("/logs", json={"api_key": "right-key"})
    assert resp.status_code == 200
    assert resp.json() == ["a private question"]
