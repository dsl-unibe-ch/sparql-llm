"""Every call to an endpoint gives up in bounded time, and status polling adds none while a rebuild runs."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main
from sparql_llm.indexing import drift, rebuild
from sparql_llm.utils import query_sparql

ADMIN = SimpleNamespace(id=1, email="admin@example.org", is_superuser=True, is_curator=False, is_active=True)


def test_query_sparql_without_a_timeout_still_bounds_each_wait(monkeypatch: pytest.MonkeyPatch):
    seen: list[dict] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"results": {"bindings": []}})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    query_sparql("SELECT * WHERE { ?s ?p ?o }", "https://a.example/sparql")
    assert seen and all(v is not None for v in seen[0].values())


def test_a_fingerprint_bounds_each_query(use_projects):
    project = use_projects({"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"})[0]
    calls: list[dict] = []

    def fetch(query, url, **kw):
        calls.append(kw)
        return {"results": {"bindings": []}}

    drift.take_fingerprint(project, fetch=fetch)
    assert len(calls) == 3
    assert all(kw.get("timeout") and kw.get("deadline") for kw in calls)


def test_status_skips_the_drift_check_while_a_rebuild_runs(use_projects, monkeypatch: pytest.MonkeyPatch):
    use_projects({"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"})
    checked: list[str] = []
    monkeypatch.setattr(main, "check_drift", lambda pid: checked.append(pid) or {"stale": False})
    rebuild._set_job("a", "running", "Indexing…")
    main.app.dependency_overrides[main.current_active_user] = lambda: ADMIN
    try:
        response = TestClient(main.app).get("/admin/index-status?dataset=a")
    finally:
        main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["job"]["state"] == "running"
    assert checked == []
