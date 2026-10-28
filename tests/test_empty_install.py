"""A fresh install (empty data dir, no datasets) boots without network and a new admin can log in."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from sparql_llm.agent import auth, main
from sparql_llm.config import settings


def test_empty_data_dir_boots_and_creates_admin(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "users.db"
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_db_path", str(db_path))
    monkeypatch.setattr(settings, "projects", [])
    monkeypatch.setattr(settings, "admin_email", "admin@example.org")
    monkeypatch.setattr(settings, "admin_password", "pw-" + "x" * 20)
    monkeypatch.setattr(settings, "cookie_secure", False)

    # The auth engine binds to the install's database file at import: point it at this one.
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for module in (auth, main):
        monkeypatch.setattr(module, "engine", engine)
        monkeypatch.setattr(module, "async_session_maker", maker)

    def boom(*args, **kwargs):
        raise AssertionError("network call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)

    with TestClient(main.app, base_url="http://localhost:8000") as c:
        assert c.get("/readyz").status_code == 200
        resp = c.post(
            "/login",
            data={"username": "admin@example.org", "password": "pw-" + "x" * 20, "next": "/"},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.status_code
        assert c.get("/admin").status_code == 200
        chat = c.get("/")
        assert chat.status_code == 200
        assert "no datasets yet" in chat.text.lower()
