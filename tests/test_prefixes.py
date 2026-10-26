"""A project's declared prefixes win over discovered ones; a missing service description is not an error."""

import logging

import httpx
import pytest

from sparql_llm import utils
from sparql_llm.utils import EndpointsMetadataManager, merge_prefixes, query_sparql


def test_declared_schema_beats_rdflib_default():
    discovered = {"schema": "https://schema.org/", "schema1": "http://schema.org/"}
    declared = {"schema": "http://schema.org/"}
    assert merge_prefixes(discovered, declared) == {"schema": "http://schema.org/"}


def test_discovered_prefixes_are_kept_when_they_do_not_clash():
    discovered = {"skos": "http://www.w3.org/2004/02/skos/core#", "schema": "https://schema.org/"}
    declared = {"bioc": "http://ldf.fi/schema/bioc/"}
    assert merge_prefixes(discovered, declared) == {
        "bioc": "http://ldf.fi/schema/bioc/",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "schema": "https://schema.org/",
    }


def test_service_description_404_is_not_an_error(caplog: pytest.LogCaptureFixture):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("accept") == "text/turtle":
            return httpx.Response(404, text="Not found")
        return httpx.Response(200, json={"results": {"bindings": []}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="sparql_llm"):
        result = query_sparql("SELECT * WHERE { ?s ?p ?o }", "https://ldf.fi/norssit/sparql", client=client, check_service_desc=True)
    assert result == {"results": {"bindings": []}}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_metadata_applies_the_project_prefixes(norssit_project, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(utils, "get_schema_for_endpoint", lambda url, void_file=None: {"http://x/C": {}})
    monkeypatch.setattr(
        utils,
        "get_prefixes_for_endpoint",
        lambda url, examples_file=None, prefixes_map=None: {"schema": "https://schema.org/", "schema1": "http://schema.org/"},
    )
    manager = EndpointsMetadataManager([norssit_project], auto_init=False)
    manager.refresh()
    assert manager.prefixes_map["schema"] == "http://schema.org/"
    assert "schema1" not in manager.prefixes_map
