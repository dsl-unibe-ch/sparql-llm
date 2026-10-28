"""/healthz and /readyz: no login, no network, one-line reasons."""

import sqlite3

import httpx
import pytest
from starlette.testclient import TestClient

from sparql_llm.agent import main
from sparql_llm.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A test install: its own data dir and an existing, empty users database."""
    db = tmp_path / "users.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "auth_db_path", str(db))
    monkeypatch.setattr(settings, "projects", [])
    return TestClient(main.app)


def test_healthz_ok(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ready_on_test_install(client: TestClient):
    resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ready"}


def test_readyz_reports_unreadable_users_db(client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_db_path", str(tmp_path / "missing.db"))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not ready"
    assert "users_db" in body["failing"]
    assert "\n" not in body["failing"]["users_db"]


def test_readyz_reports_unwritable_data_dir(client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", str(not_a_dir))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert "data_dir" in resp.json()["failing"]


def test_readyz_leaves_no_probe_file(client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    assert client.get("/readyz").status_code == 200
    assert list((tmp_path / "data").iterdir()) == []


# TestClient is an httpx.Client too: patch the real network transports, not Client.send.
def test_readyz_makes_no_network_call(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise AssertionError("network call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)
    assert client.get("/readyz").status_code == 200
