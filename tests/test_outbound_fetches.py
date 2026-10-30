"""Check and Rebuild: every fetch goes through the outbound guard, and a refusal degrades as a failure does."""

import json
import re
from pathlib import Path

import httpx
import pytest
import rdflib.parser

from sparql_llm import outbound
from sparql_llm.indexing import index_resources, sync_examples
from sparql_llm.indexing.preflight import run_preflight
from sparql_llm.indexing.sync_examples import discover_sources, sync_curator_examples
from sparql_llm.outbound import DestinationRefused
from sparql_llm.project import CuratorExamples
from sparql_llm.utils import query_sparql

PRIVATE_ENDPOINT = "http://127.0.0.1:9/sparql"
ENDPOINT_LINKS = {"label": "Alpha", "endpoint_url": "https://a.example/sparql", "homepage_url": "https://a.example/"}
CONFIG = CuratorExamples(github_repo="someone/queries", folder="examples")
SOURCE = Path(outbound.__file__).parent
DIRECT_CALL = re.compile(r"httpx\.(Client|AsyncClient|get|post|put|delete|head|stream|request)\(|urlopen\(|requests\.(get|post)\(")


@pytest.fixture(autouse=True)
def no_allow_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OUTBOUND_ALLOW", raising=False)


def _serve_homepage(monkeypatch: pytest.MonkeyPatch, json_ld: dict) -> None:
    """The homepage fetch answers a page holding this JSON-LD, without any network."""
    page = f'<html><script type="application/ld+json">{json.dumps(json_ld)}</script></html>'

    def client(purpose, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page)), **kwargs)

    monkeypatch.setattr(index_resources, "guarded_client", client)


def test_query_sparql_refuses_a_private_endpoint():
    with pytest.raises(DestinationRefused):
        query_sparql("ASK { ?s ?p ?o }", PRIVATE_ENDPOINT)


def test_preflight_reports_a_refused_endpoint(use_projects):
    project = use_projects({"id": "a", "label": "Alpha", "endpoint_url": PRIVATE_ENDPOINT})[0]
    results = run_preflight(project)
    assert [(r.step, r.status) for r in results] == [("endpoint", "error")]
    assert "OUTBOUND_ALLOW" in results[0].message


def test_a_refused_listing_gives_no_sources_and_says_so(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sync_examples, "listing_url", lambda config: "http://127.0.0.1:9/listing")
    urls, note = discover_sources(CONFIG)
    assert urls == ()
    assert "could not list" in note


def test_sync_keeps_previous_examples_when_refused(use_projects, monkeypatch: pytest.MonkeyPatch):
    project = use_projects(
        {
            "id": "a",
            "label": "Alpha",
            "endpoint_url": "https://a.example/sparql",
            "curator_examples": {"github_repo": "someone/queries", "folder": "examples"},
        }
    )[0]
    monkeypatch.setattr(sync_examples, "listing_url", lambda config: "http://127.0.0.1:9/listing")
    report = sync_curator_examples(project)
    assert report["error"].endswith("the previously synced examples are kept")
    assert report["written"] is False


def test_homepage_refused_degrades():
    assert index_resources.load_schemaorg_description({**ENDPOINT_LINKS, "homepage_url": "http://127.0.0.1:9/"}) == []


def test_foreign_json_ld_context_is_never_fetched(monkeypatch: pytest.MonkeyPatch):
    fetched: list = []
    monkeypatch.setattr(rdflib.parser, "URLInputSource", lambda *args, **kwargs: fetched.append(args))
    _serve_homepage(monkeypatch, {"@context": "http://169.254.169.254/ctx", "@type": "Dataset"})
    assert index_resources.load_schemaorg_description(ENDPOINT_LINKS) == []
    assert fetched == []


def test_a_look_alike_host_is_a_foreign_context(monkeypatch: pytest.MonkeyPatch):
    parsed: list = []
    monkeypatch.setattr(index_resources.Dataset, "parse", lambda self, **kwargs: parsed.append(kwargs))
    _serve_homepage(monkeypatch, {"@context": "https://schema.org@10.0.0.1/", "@type": "Dataset"})
    assert index_resources.load_schemaorg_description(ENDPOINT_LINKS) == []
    assert parsed == []


def test_schema_org_context_still_parses(monkeypatch: pytest.MonkeyPatch):
    parsed: list = []
    monkeypatch.setattr(index_resources.Dataset, "parse", lambda self, **kwargs: parsed.append(kwargs))
    _serve_homepage(monkeypatch, {"@context": "https://schema.org", "@type": "Dataset"})
    index_resources.load_schemaorg_description(ENDPOINT_LINKS)
    assert len(parsed) == 1


def test_no_direct_http_calls_outside_the_guard():
    found = [
        f"{path.relative_to(SOURCE)}:{number}: {line.strip()}"
        for path in SOURCE.rglob("*.py")
        if path.name != "outbound.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if DIRECT_CALL.search(line)
    ]
    assert not found, "\n".join(found)
