"""A corrupted endpoint URL in a tool call must not cost the model a round.

gpt-oss sometimes damages the endpoint_url argument of execute_sparql_query
(traced: ".../default_wiss4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"),
which gets a 404 and sends the model round the loop again. With a single
endpoint configured, an unknown URL can only mean that endpoint.
"""

import pytest

from sparql_llm import mcp_server
from sparql_llm.mcp_server import resolve_endpoint_url

ENDPOINT = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"
OTHER = "https://sparql.uniprot.org/sparql/"


@pytest.fixture
def one_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.settings, "endpoints", [{"label": "Swiss Elites", "endpoint_url": ENDPOINT}])


def test_corrupted_url_falls_back_to_the_only_endpoint(one_endpoint):
    corrupted = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wiss4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"
    assert resolve_endpoint_url(corrupted) == ENDPOINT


def test_configured_url_is_kept(one_endpoint):
    assert resolve_endpoint_url(ENDPOINT) == ENDPOINT


def test_unknown_url_is_kept_when_several_endpoints_are_configured(monkeypatch: pytest.MonkeyPatch):
    # With more than one endpoint there is no safe guess.
    monkeypatch.setattr(
        mcp_server.settings,
        "endpoints",
        [{"label": "A", "endpoint_url": ENDPOINT}, {"label": "B", "endpoint_url": OTHER}],
    )
    assert resolve_endpoint_url("https://example.org/sparql") == "https://example.org/sparql"
