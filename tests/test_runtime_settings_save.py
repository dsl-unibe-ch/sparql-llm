"""The admin top-k Save writes atomically to the data dir and reports failures."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm import config
from sparql_llm.agent import main

ADMIN = SimpleNamespace(id=1, email="admin@example.org", is_superuser=True, is_curator=False, is_active=True)
KEY = "default_number_of_retrieved_docs"


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(main.settings, KEY, 20)
    monkeypatch.delenv(KEY.upper(), raising=False)
    main.app.dependency_overrides[main.current_active_user] = lambda: ADMIN
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_save_writes_runtime_file_not_install_file(client, tmp_path: Path, monkeypatch):
    install = tmp_path / "install.json"
    install.write_text(json.dumps({KEY: 20}), encoding="utf-8")
    before = install.read_bytes()
    monkeypatch.setattr(main, "settings_filepath", str(install), raising=False)

    response = client.post("/admin/settings", json={KEY: 7})

    assert response.status_code == 200
    assert json.loads((tmp_path / "settings.runtime.json").read_text(encoding="utf-8"))[KEY] == 7
    assert install.read_bytes() == before
    assert main.settings.default_number_of_retrieved_docs == 7


def test_save_preserves_other_runtime_keys(client, tmp_path: Path):
    (tmp_path / "settings.runtime.json").write_text(json.dumps({"other": 1}), encoding="utf-8")

    assert client.post("/admin/settings", json={KEY: 5}).status_code == 200

    saved = json.loads((tmp_path / "settings.runtime.json").read_text(encoding="utf-8"))
    assert saved == {"other": 1, KEY: 5}


def test_save_failure_reports_error_and_keeps_value(client, tmp_path: Path, monkeypatch):
    def boom(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(config.os, "replace", boom)

    response = client.post("/admin/settings", json={KEY: 9})

    assert response.status_code == 500
    assert "read-only" in response.json()["error"]
    assert main.settings.default_number_of_retrieved_docs == 20
    assert list(tmp_path.glob("*.tmp")) == []


def test_get_reports_env_pin(client, monkeypatch):
    assert client.get("/admin/settings").json()["pinned"][KEY] is False
    monkeypatch.setenv(KEY.upper(), "3")
    assert client.get("/admin/settings").json()["pinned"][KEY] is True


def test_corrupt_runtime_file_reports_error_and_is_kept(client, tmp_path: Path):
    runtime = tmp_path / "settings.runtime.json"
    runtime.write_bytes(b'{"truncated": ')

    response = client.post("/admin/settings", json={KEY: 9})

    assert response.status_code == 500
    assert "settings.runtime.json" in response.json()["error"]
    assert main.settings.default_number_of_retrieved_docs == 20
    assert runtime.read_bytes() == b'{"truncated": '
    assert list(tmp_path.glob("*.tmp")) == []


def test_bad_encoding_runtime_file_reports_error(client, tmp_path: Path):
    (tmp_path / "settings.runtime.json").write_bytes(b"\xff\xfe\x00bad")

    response = client.post("/admin/settings", json={KEY: 9})

    assert response.status_code == 500
    assert "settings.runtime.json" in response.json()["error"]
    assert main.settings.default_number_of_retrieved_docs == 20


def test_data_dir_that_is_a_file_reports_error(client, tmp_path: Path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(main.settings, "data_dir", str(blocker))

    response = client.post("/admin/settings", json={KEY: 9})

    assert response.status_code == 500
    assert response.json()["error"].startswith("Could not save the setting")
    assert main.settings.default_number_of_retrieved_docs == 20
    assert blocker.read_text(encoding="utf-8") == "x"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["not-a-dir"]
