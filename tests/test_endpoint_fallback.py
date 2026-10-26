"""A corrupted endpoint URL in a tool call must not cost the model a round.

gpt-oss sometimes damages the endpoint_url argument of execute_sparql_query
(traced: ".../default_wiss4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"),
which gets a 404 and sends the model round the loop again. With a single
endpoint configured, an unknown URL can only mean that endpoint.
"""

import pytest

from sparql_llm.mcp_server import resolve_endpoint_url

ENDPOINT = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"
OTHER = "https://sparql.uniprot.org/sparql/"


@pytest.fixture
def one_endpoint(use_projects) -> None:
    use_projects({"id": "swiss-elites", "label": "Swiss Elites", "endpoint_url": ENDPOINT})


def test_corrupted_url_falls_back_to_the_only_endpoint(one_endpoint):
    corrupted = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wiss4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"
    assert resolve_endpoint_url(corrupted)[0] == ENDPOINT


def test_missing_url_means_the_only_endpoint(one_endpoint):
    # gpt-oss once left endpoint_url out entirely ("Field required"); the tool
    # now defaults it to "".
    assert resolve_endpoint_url("")[0] == ENDPOINT


def test_configured_url_is_kept(one_endpoint):
    assert resolve_endpoint_url(ENDPOINT) == (ENDPOINT, "")


def test_unknown_url_is_refused_when_several_endpoints_are_configured(use_projects):
    # With more than one endpoint there is no safe guess, and queries only run on configured ones.
    use_projects({"id": "a", "label": "A", "endpoint_url": ENDPOINT}, {"id": "b", "label": "B", "endpoint_url": OTHER})
    url, error = resolve_endpoint_url("https://example.org/sparql")
    assert url is None
    assert ENDPOINT in error and OTHER in error
