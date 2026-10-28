"""Settings precedence: env / .env > <data_dir>/settings.runtime.json > install file > code defaults."""

import json
from pathlib import Path

import pytest

from sparql_llm.config import RUNTIME_SETTINGS_FILE, Settings, SettingsError, env_pins

PINNED = ("PROJECTS", "AUTO_INIT", "DEFAULT_NUMBER_OF_RETRIEVED_DOCS", "APP_NAME", "APP_PUBLIC_HOST")


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in PINNED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def _install(tmp_path: Path, content: dict) -> str:
    path = tmp_path / "install.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


def _runtime(data_dir: Path, content: dict | str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (data_dir / RUNTIME_SETTINGS_FILE).write_text(text, encoding="utf-8")


def test_env_beats_install_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = _install(tmp_path, {"auto_init": True})
    monkeypatch.setenv("AUTO_INIT", "false")
    assert Settings.load(p).auto_init is False


def test_runtime_beats_install_file(tmp_path: Path):
    p = _install(tmp_path, {"default_number_of_retrieved_docs": 20})
    _runtime(tmp_path, {"default_number_of_retrieved_docs": 7})
    assert Settings.load(p).default_number_of_retrieved_docs == 7


def test_env_beats_runtime_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _runtime(tmp_path, {"default_number_of_retrieved_docs": 7})
    monkeypatch.setenv("DEFAULT_NUMBER_OF_RETRIEVED_DOCS", "3")
    assert Settings.load(None).default_number_of_retrieved_docs == 3


def test_install_beats_default(tmp_path: Path):
    assert Settings.load(_install(tmp_path, {"app_name": "X"})).app_name == "X"


def test_no_files_gives_defaults():
    assert Settings.load(None).default_number_of_retrieved_docs == 10


def test_empty_env_does_not_erase_install_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = _install(tmp_path, {"app_public_host": "a.example"})
    monkeypatch.setenv("APP_PUBLIC_HOST", "")
    assert Settings.load(p).app_public_host == "a.example"


def test_corrupt_runtime_file_refuses(tmp_path: Path):
    _runtime(tmp_path, "{")
    with pytest.raises(SettingsError, match="settings.runtime.json"):
        Settings.load(None)


def test_data_dir_from_install_file_locates_runtime_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATA_DIR")
    p = _install(tmp_path, {"data_dir": str(tmp_path / "d")})
    _runtime(tmp_path / "d", {"default_number_of_retrieved_docs": 5})
    assert Settings.load(p).default_number_of_retrieved_docs == 5


def test_install_file_lists_load_as_before(tmp_path: Path):
    models = ["gpustack/a", "gpustack/b"]
    p = _install(tmp_path, {"projects": ["norssit"], "available_llm_models": models})
    s = Settings.load(p)
    assert s.projects == ["norssit"]
    assert s.available_llm_models == models


def test_plain_settings_ignore_files_loaded_earlier(tmp_path: Path):
    Settings.load(_install(tmp_path, {"app_name": "X"}))
    assert Settings(_env_file=None).app_name == "SPARQL Chat"  # type: ignore[call-arg]


def test_env_pins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEFAULT_NUMBER_OF_RETRIEVED_DOCS", "3")
    assert env_pins("default_number_of_retrieved_docs") is True
    monkeypatch.delenv("DEFAULT_NUMBER_OF_RETRIEVED_DOCS")
    assert env_pins("default_number_of_retrieved_docs") is False
