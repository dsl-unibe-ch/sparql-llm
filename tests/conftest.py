"""Shared fixtures: tests run against project folders in temporary data dirs, never the install settings."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO_DATA = Path(__file__).parent.parent / "data"

SWISS_ENDPOINT = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"

# A minimal SDHSS-like project, used until data/swiss-elites/ exists in the repo.
MINIMAL_SWISS = {
    "id": "swiss-elites",
    "label": "Swiss Elites",
    "endpoint_url": SWISS_ENDPOINT,
    "homepage_url": "https://elites-suisses.lod4hss.org/",
    "graphs": ["https://swiss-elites.lod4hss.cloud/resource/"],
    "prefixes": {
        "crm": "http://www.cidoc-crm.org/cidoc-crm/",
        "sdh-short": "https://sdhss.org/ontology/shortcuts/",
        "swel": "https://elites-suisses.lod4hss.org/resource/",
    },
    "entities": {
        "label_property": "https://sdhss.org/ontology/shortcuts/P9",
        "types": {"Person": {"class": "http://www.cidoc-crm.org/cidoc-crm/E21"}},
    },
}

NORSSIT = {
    "id": "norssit",
    "label": "Norssit",
    "endpoint_url": "https://ldf.fi/norssit/sparql",
    "method": "GET",
    "description": "Alumni of the Helsinki Normal Lyceum",
    "homepage_url": "http://www.ldf.fi/dataset/norssit",
    "graphs": [],
    "prefixes": {
        "schema": "http://schema.org/",
        "bioc": "http://ldf.fi/schema/bioc/",
        "norssit": "http://ldf.fi/norssit/",
        "foaf": "http://xmlns.com/foaf/0.1/",
        "skos": "http://www.w3.org/2004/02/skos/core#",
    },
    "entities": {
        "label_property": "http://www.w3.org/2004/02/skos/core#prefLabel",
        "types": {
            "Person": {
                "class": "http://xmlns.com/foaf/0.1/Person",
                "birth_property": "http://schema.org/birthDate",
                "death_property": "http://schema.org/deathDate",
            },
            "Group": {"class": "http://xmlns.com/foaf/0.1/Organization"},
        },
    },
    "schema_classes": {"min_instances": 50, "exclude": ["http://ldf.fi/norssit/titles/"]},
}


def _write_swiss(data_dir: Path) -> None:
    target = data_dir / "swiss-elites"
    target.mkdir(parents=True, exist_ok=True)
    source = REPO_DATA / "swiss-elites"
    if (source / "project.json").is_file():
        for name in ("project.json", "dataset.md", "examples.md"):
            if (source / name).is_file():
                shutil.copy(source / name, target / name)
    else:
        (target / "project.json").write_text(json.dumps(MINIMAL_SWISS), encoding="utf-8")
        (target / "dataset.md").write_text("## Rules for this dataset\n\nUse the bare class name.\n", encoding="utf-8")
        (target / "examples.md").write_text("", encoding="utf-8")


def _write_norssit(data_dir: Path) -> None:
    target = data_dir / "norssit"
    target.mkdir(parents=True, exist_ok=True)
    (target / "project.json").write_text(json.dumps(NORSSIT), encoding="utf-8")
    shutil.copy(FIXTURES / "norssit" / "dataset.md", target / "dataset.md")
    shutil.copy(FIXTURES / "norssit" / "examples.md", target / "examples.md")


# The local .env may point SETTINGS_FILEPATH at a server path; tests use neutral defaults.
# Several modules read settings.endpoints at import time, so the session gets a default
# Swiss-like project before sparql_llm.config is imported (env vars beat .env values).
_SESSION_DATA = Path(tempfile.mkdtemp(prefix="sparql-llm-tests-"))
_write_swiss(_SESSION_DATA)
os.environ["SETTINGS_FILEPATH"] = ""
os.environ["DATA_DIR"] = str(_SESSION_DATA)
os.environ["PROJECTS"] = '["swiss-elites"]'
# Never fetch endpoint metadata over the network while importing modules.
os.environ["AUTO_INIT"] = "false"


def _use_projects(monkeypatch: pytest.MonkeyPatch, data_dir: Path, project_ids: list[str]):
    from sparql_llm.config import settings
    from sparql_llm.project import reload_projects

    monkeypatch.setattr(settings, "data_dir", str(data_dir))
    monkeypatch.setattr(settings, "projects", project_ids)
    return reload_projects()


@pytest.fixture
def use_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Serve the given project dicts (written to tmp_path) for the duration of the test."""

    def _serve(*projects: dict):
        for project in projects:
            (tmp_path / project["id"]).mkdir(parents=True, exist_ok=True)
            (tmp_path / project["id"] / "project.json").write_text(json.dumps(project), encoding="utf-8")
        return _use_projects(monkeypatch, tmp_path, [p["id"] for p in projects])

    yield _serve
    monkeypatch.undo()
    from sparql_llm.project import reload_projects

    reload_projects()


@pytest.fixture
def swiss_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_swiss(tmp_path)
    yield _use_projects(monkeypatch, tmp_path, ["swiss-elites"])[0]
    monkeypatch.undo()
    from sparql_llm.project import reload_projects

    reload_projects()


@pytest.fixture
def norssit_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_norssit(tmp_path)
    yield _use_projects(monkeypatch, tmp_path, ["norssit"])[0]
    monkeypatch.undo()
    from sparql_llm.project import reload_projects

    reload_projects()
