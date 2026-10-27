"""The chat page shows one dataset at a time: picked from the sidebar, remembered in a cookie."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sparql_llm.agent import main
from tests.conftest import NORSSIT

USER = SimpleNamespace(id=1, email="user@example.org", is_superuser=False, is_curator=False, is_active=True)
CURATOR = SimpleNamespace(id=2, email="curator@example.org", is_superuser=False, is_curator=True, is_active=True)
SWISS = {
    "id": "swiss",
    "label": "Swiss Elites",
    "endpoint_url": "https://s.example/sparql",
    "intro": "Ask about **Swiss** elites.",
    "example_questions": ["How many Swiss persons?"],
}
NORSSIT_TEXTS = {**NORSSIT, "intro": "Ask about the **Norssit** alumni.", "example_questions": ["How many alumni?"]}


class _Session:
    """Stands in for the users database: one saved chat, owned by whoever asks."""

    def __init__(self, project: str, owner):
        self.conversation = SimpleNamespace(id="c1", project=project, user_id=owner.id)

    async def get(self, model, key):
        return self.conversation if key == "c1" else None


@pytest.fixture
def datasets(use_projects, tmp_path: Path):
    """swiss published, norssit ready (rebuilt) but not published."""
    from sparql_llm.project import reload_projects

    use_projects(SWISS, NORSSIT_TEXTS)
    (tmp_path / "datasets.json").write_text(
        json.dumps([{"id": "swiss", "published": True}, {"id": "norssit", "published": False}]), encoding="utf-8"
    )
    (tmp_path / "norssit" / "fingerprint.json").write_text("{}", encoding="utf-8")
    reload_projects()


def _client_as(user, chat_project: str = "norssit") -> TestClient:
    main.app.dependency_overrides[main.current_active_user] = lambda: user
    main.app.dependency_overrides[main.get_async_session] = lambda: _Session(chat_project, user)
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main.app.dependency_overrides.clear()


def _selected(page: str) -> str:
    return page.split('data-selected-dataset="', 1)[1].split('"', 1)[0]


def test_page_opens_on_the_last_used_dataset(datasets):
    client = _client_as(CURATOR)
    client.cookies.set("last_dataset", "norssit")

    assert _selected(client.get("/").text) == "norssit"


def test_dataset_parameter_switches_and_sets_cookie(datasets):
    client = _client_as(CURATOR)

    response = client.get("/?dataset=norssit")

    assert _selected(response.text) == "norssit"
    cookie = response.headers["set-cookie"]
    assert "last_dataset=norssit" in cookie and "Max-Age=31536000" in cookie and "samesite=lax" in cookie.lower()


def test_user_does_not_see_unpublished_dataset_in_picker(datasets):
    user_page = _client_as(USER).get("/?dataset=norssit").text
    curator_page = _client_as(CURATOR).get("/").text

    assert _selected(user_page) == "swiss"
    assert 'href="/?dataset=norssit"' not in user_page
    assert 'href="/?dataset=norssit"' in curator_page
    assert "only visible to curators" in curator_page


def test_page_shows_the_selected_datasets_intro_and_examples_only(datasets):
    page = _client_as(CURATOR).get("/?dataset=norssit").text

    assert "<strong>Norssit</strong>" in page and "<strong>Swiss</strong>" not in page
    assert 'examples="How many alumni?"' in page and "How many Swiss persons?" not in page
    assert 'href="https://ldf.fi/norssit/sparql"' in page and "https://s.example/sparql" not in page
    assert "Ask about Norssit…" in page


def test_chat_requests_carry_the_dataset(datasets):
    page = _client_as(CURATOR).get("/?dataset=norssit").text

    assert 'payload.project = "norssit";' in page
    # Saves and the history list are tagged by the same interceptor: the widget builds
    # each chat's URL from history-endpoint, so the attribute itself stays bare.
    assert 'history-endpoint="/conversations"' in page


def test_opening_a_saved_chat_switches_to_its_dataset(datasets):
    response = _client_as(CURATOR, chat_project="norssit").get("/?chat=c1")

    assert _selected(response.text) == "norssit"
    assert "last_dataset=norssit" in response.headers["set-cookie"]


def test_a_user_with_no_dataset_sees_no_unpublished_one(datasets, tmp_path: Path):
    """Final review: with nothing published, the page fell back to every served dataset's endpoint."""
    (tmp_path / "datasets.json").write_text(
        json.dumps([{"id": "swiss", "published": False}, {"id": "norssit", "published": False}]), encoding="utf-8"
    )
    page = _client_as(USER).get("/").text

    assert "https://s.example/sparql" not in page and "https://ldf.fi/norssit/sparql" not in page
    assert "Ask about **Swiss**" not in page and "<strong>Swiss</strong>" not in page


def test_api_description_names_no_dataset(datasets):
    """Final review: /docs named every served dataset (unpublished ones too) and the Swiss topics."""
    description = main._api_description()
    assert "s.example" not in description and "ldf.fi" not in description and "Norssit" not in description


def test_no_history_without_a_dataset(datasets, tmp_path: Path):
    """GPT review I5: with nothing published, the sidebar asked for every chat, unfiltered."""
    (tmp_path / "datasets.json").write_text(
        json.dumps([{"id": "swiss", "published": False}, {"id": "norssit", "published": False}]), encoding="utf-8"
    )
    assert 'history-endpoint=""' in _client_as(USER).get("/").text


def test_the_page_saves_and_restores_chat_settings(datasets):
    """The page records the chat's settings with each save and applies them when a saved chat opens."""
    page = _client_as(CURATOR).get("/").text

    assert "saved.settings = currentSettings()" in page
    assert "applySettings(" in page
