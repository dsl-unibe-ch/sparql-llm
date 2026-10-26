"""Every page shows the instance's own name, intro, endpoint and counts — never Swiss text by default."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main
from sparql_llm.config import settings

FORBIDDEN = ["swiss-elites.lod4hss", "elites-suisses.lod4hss", "sdh-short", "crm:E21", "Brenner", "58,7"]
ADMIN = SimpleNamespace(id=1, email="admin@example.org", is_superuser=True, is_curator=False, is_active=True)


@pytest.fixture
def norssit_site(norssit_project, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "app_name", "Norssit Chat")
    monkeypatch.setattr(settings, "app_intro", "Ask about the **Norssit** alumni register of [the lyceum](http://www.ldf.fi/dataset/norssit).")
    main.app.dependency_overrides[main.current_active_user] = lambda: ADMIN
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_index_page_has_no_swiss_text_for_norssit(norssit_site):
    page = norssit_site.get("/").text
    assert [s for s in FORBIDDEN if s in page] == []
    assert "Norssit Chat" in page
    assert "<strong>Norssit</strong>" in page
    assert 'href="https://ldf.fi/norssit/sparql"' in page


def test_other_pages_use_the_app_name(norssit_site):
    for path in ("/login", "/api-settings", "/change-password"):
        page = norssit_site.get(path).text
        assert "Swiss Elites" not in page, path
        assert "Norssit Chat" in page, path


def test_intro_markdown_is_escaped(norssit_site, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "app_intro", "<script>alert(1)</script> **ok**")
    page = norssit_site.get("/").text
    assert "<script>alert(1)</script>" not in page
    assert "<strong>ok</strong>" in page


def test_stats_pills_come_from_fingerprint(norssit_site, norssit_project):
    norssit_project.fingerprint_path.write_text(
        json.dumps({"triples": 1, "classes": {"http://xmlns.com/foaf/0.1/Person": 10137}}), encoding="utf-8"
    )
    page = norssit_site.get("/").text
    assert "Person: 10,137" in page
    assert "Group:" not in page  # no count recorded for it


def test_no_pills_without_fingerprint(norssit_site):
    assert "Person:" not in norssit_site.get("/").text


def test_saved_chat_export_names_this_instance(monkeypatch: pytest.MonkeyPatch):
    from datetime import datetime

    from sparql_llm.agent.conversations import conversation_to_markdown

    monkeypatch.setattr(settings, "app_name", "Norssit Chat")
    text = conversation_to_markdown("T", [], datetime(2026, 10, 26, 12, 0))
    assert "Norssit Chat · started 2026-10-26 12:00 UTC" in text


def test_entity_tool_description_has_no_swiss_examples():
    from sparql_llm.mcp_server import get_mcp_app

    tools = {t.name: t for t in get_mcp_app()._tool_manager.list_tools()}
    description = tools["resolve_entity_uri"].description
    assert [s for s in FORBIDDEN + ["Study title"] if s in description] == []
