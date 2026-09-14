"""The query validator's schema must follow the VoID file, not a stale cache.

The validator reads its classes schema from data/endpoints_metadata.json, which
was written only when missing and is not tracked by git. On vm7 it was a
months-old snapshot (it listed the dead sdh-slc:C9 and lacked sdh-slc:C3), so
every correct marriage query was rejected as using an unknown class, and the
agent concluded Ernst Brenner had no spouse. The admin rebuild now refreshes it.
"""

import json

import pytest

from sparql_llm import utils
from sparql_llm.utils import EndpointsMetadataManager

ENDPOINT = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"
VOID_FILE = "data/elites-suisses-void.ttl"
C3 = "https://sdhss.org/ontology/social-life-core/C3"
C9 = "https://sdhss.org/ontology/social-life-core/C9"


@pytest.fixture
def stale_cache(tmp_path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "endpoints_metadata.json"
    cache.write_text(
        json.dumps(
            {
                "prefixes_map": {"swel": "https://swiss-elites.lod4hss.cloud/resource/"},
                "classes_schema": {ENDPOINT: {C9: {"http://www.w3.org/1999/02/22-rdf-syntax-ns#type": []}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(utils, "ENDPOINTS_METADATA_FILE", cache)
    # Prefixes come from the VoID file here; do not ask the live endpoint.
    monkeypatch.setattr(
        utils, "get_prefixes_for_endpoint", lambda url, examples_file=None, prefixes_map=None: prefixes_map or {}
    )
    return cache


def _manager() -> EndpointsMetadataManager:
    endpoint = {"label": "Swiss Elites", "endpoint_url": ENDPOINT, "void_file": VOID_FILE}
    return EndpointsMetadataManager([endpoint], auto_init=False)  # type: ignore[list-item]


def test_the_cache_file_is_used_until_refreshed(stale_cache):
    assert C9 in _manager().void_dict[ENDPOINT]


def test_refresh_replaces_a_stale_schema(stale_cache):
    manager = _manager()
    manager.refresh()
    assert C3 in manager.void_dict[ENDPOINT]
    assert C9 not in manager.void_dict[ENDPOINT]
    saved = json.loads(stale_cache.read_text(encoding="utf-8"))
    assert C3 in saved["classes_schema"][ENDPOINT]


def test_refresh_gives_swel_the_entity_uri_space(stale_cache):
    # Person URIs live under elites-suisses.lod4hss.org; the named graph is on
    # swiss-elites.lod4hss.cloud. The validator adds missing prefixes from this map,
    # so the wrong host would turn a correct swel:p50001 into a URI that matches nothing.
    manager = _manager()
    manager.refresh()
    assert manager.prefixes_map["swel"] == "https://elites-suisses.lod4hss.org/resource/"


def test_refresh_keeps_the_previous_schema_when_none_is_found(stale_cache, monkeypatch: pytest.MonkeyPatch):
    manager = _manager()
    assert C9 in manager.void_dict[ENDPOINT]
    monkeypatch.setattr(utils, "get_schema_for_endpoint", lambda url, void_file=None: {})
    with pytest.raises(RuntimeError):
        manager.refresh()
    assert C9 in manager.void_dict[ENDPOINT]
    assert C9 in json.loads(stale_cache.read_text(encoding="utf-8"))["classes_schema"][ENDPOINT]
