"""POST /admin/check runs the preflight for managers, and refuses while a rebuild runs."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main
from sparql_llm.indexing import preflight
from sparql_llm.indexing.rebuild import lock_file

ADMIN = SimpleNamespace(id=1, email="admin@example.org", is_superuser=True, is_curator=False, is_active=True)
USER = SimpleNamespace(id=2, email="user@example.org", is_superuser=False, is_curator=False, is_active=True)


@pytest.fixture
def client_as():
    def _client(user):
        main.app.dependency_overrides[main.current_active_user] = lambda: user
        return TestClient(main.app)

    yield _client
    main.app.dependency_overrides.clear()


def test_check_returns_each_project_results(norssit_project, client_as, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        preflight, "run_preflight", lambda project: [preflight.CheckResult("endpoint", "ok", "Norssit: fine.")]
    )
    response = client_as(ADMIN).post("/admin/check")
    assert response.status_code == 200
    assert response.json()["projects"]["norssit"][0]["message"] == "Norssit: fine."


def test_check_refused_while_a_rebuild_runs(norssit_project, client_as):
    lock_file().write_text("1 now", encoding="utf-8")
    response = client_as(ADMIN).post("/admin/check")
    assert response.status_code == 409
    assert "rebuild is running" in response.json()["error"]


def test_check_is_for_managers_only(norssit_project, client_as):
    assert client_as(USER).post("/admin/check").status_code == 403
