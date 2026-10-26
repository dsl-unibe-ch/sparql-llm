"""The shipped template and the Norssit worked example are valid project files."""

import json
from pathlib import Path

from sparql_llm.indexing.sync_examples import parse_examples
from sparql_llm.project_files import validate_upload

ROOT = Path(__file__).parent.parent
TEMPLATE = ROOT / "projects" / "_template"
NORSSIT = ROOT / "docs" / "examples" / "norssit"


def _with_id(path: Path, project_id: str) -> bytes:
    content = json.loads(path.read_text(encoding="utf-8"))
    content["id"] = project_id
    return json.dumps(content).encode("utf-8")


def test_template_project_validates(norssit_project):
    # The template's id is a placeholder; a curator renames it to their project's id.
    assert validate_upload("norssit", "project.json", _with_id(TEMPLATE / "project.json", "norssit")) == []
    assert validate_upload("norssit", "dataset.md", (TEMPLATE / "dataset.md").read_bytes()) == []
    assert validate_upload("norssit", "examples.md", (TEMPLATE / "examples.md").read_bytes()) == []


def test_norssit_example_project_validates(norssit_project):
    for name in ("project.json", "dataset.md", "examples.md"):
        assert validate_upload("norssit", name, (NORSSIT / name).read_bytes()) == [], name


def test_norssit_examples_parse():
    assert len(parse_examples((NORSSIT / "examples.md").read_text(encoding="utf-8"))) == 6


def test_norssit_questions_file():
    questions = json.loads((NORSSIT / "questions.json").read_text(encoding="utf-8"))
    assert len(questions) == 8
    assert all(set(q) == {"question", "expected"} for q in questions)


def test_guide_explains_every_preflight_and_upload_message():
    guide = (ROOT / "docs" / "ONBOARDING.md").read_text(encoding="utf-8")
    for phrase in (
        "did not answer a SPARQL query",
        "listed in the graphs field is empty",
        "has a label",
        "is missing",
        "has no example",
        "examples return rows",
        "is not served by this instance",
        "must be GET or POST",
        "is not valid JSON",
        "A rebuild is running",
        "could not be read back",
    ):
        assert phrase in guide, phrase
