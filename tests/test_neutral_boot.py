"""Neutral code defaults and a metadata manager that never touches the network at boot."""

import json

import pytest

from sparql_llm.config import Settings
from sparql_llm.utils import EndpointsMetadataManager


def test_code_defaults_are_neutral(monkeypatch: pytest.MonkeyPatch):
    for name in ("EMBEDDING_BACKEND", "EMBEDDING_MODEL", "DEFAULT_LLM_MODEL", "AUTO_INIT"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.embedding_backend == "local"
    assert s.embedding_model == "intfloat/multilingual-e5-large"
    assert s.default_llm_model == ""
    assert s.auto_init is False


def test_missing_cache_never_fetches(swiss_project, monkeypatch: pytest.MonkeyPatch, caplog):
    def boom(self):
        raise AssertionError("fetched")

    monkeypatch.setattr(EndpointsMetadataManager, "_fetch", boom)
    swiss_project.prefixes_path.unlink(missing_ok=True)
    manager = EndpointsMetadataManager([swiss_project], auto_init=True)
    assert manager.prefixes_map == {}
    assert manager.void_dict == {}


def test_cached_metadata_still_loads(swiss_project):
    swiss_project.prefixes_path.parent.mkdir(parents=True, exist_ok=True)
    swiss_project.prefixes_path.write_text(
        json.dumps({"prefixes_map": {"crm": "http://www.cidoc-crm.org/cidoc-crm/"}, "classes_schema": {"x": {}}}),
        encoding="utf-8",
    )
    manager = EndpointsMetadataManager([swiss_project], auto_init=True)
    assert manager.prefixes_map["crm"] == "http://www.cidoc-crm.org/cidoc-crm/"
