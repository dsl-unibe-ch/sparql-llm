"""Prompts are assembled from the settings and each project's dataset.md, not hard-coded."""

from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate

from sparql_llm.agent.nodes.retrieval_entities import entities_preamble
from sparql_llm.agent.prompts import (
    escape_braces,
    extraction_prompt,
    resolution_prompt,
    tools_resolution_prompt,
)
from sparql_llm.entity_resolver import EntityResolver, describe_for_tool

FIXTURE = Path(__file__).parent / "fixtures" / "swiss_prompts_2026-10-26.txt"
FORBIDDEN = ["swiss-elites.lod4hss", "elites-suisses.lod4hss", "sdh-short", "crm:E21", "Brenner", "58,7"]


def test_swiss_resolution_prompt_keeps_every_fixture_line(swiss_project):
    prompt = resolution_prompt()
    lines = [line.strip() for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    # The opening line framed the dataset description as "You are an assistant for ..."; the
    # description now lives in dataset.md and the framing is generic (checked separately).
    assert lines[0].startswith("You are an assistant for the Elites Suisses knowledge graph")
    missing = [line for line in lines[1:] if line.rstrip(":") not in prompt]
    assert missing == []
    assert "approximately 58,700 Swiss elites" in prompt


def test_norssit_prompt_has_no_swiss_text(norssit_project):
    for prompt in (resolution_prompt(), tools_resolution_prompt(), extraction_prompt()):
        assert [s for s in FORBIDDEN if s in prompt] == []
    assert "Endpoint: https://ldf.fi/norssit/sparql (GET only)" in resolution_prompt()
    assert "named graph" not in resolution_prompt()


def test_extraction_examples_come_from_entity_types(norssit_project):
    prompt = extraction_prompt()
    assert '"foaf:Person" (Person)' in prompt
    assert '"foaf:Organization" (Group)' in prompt


def test_braces_in_dataset_md_survive_template(norssit_project):
    norssit_project.dataset_path.write_text("## Query shape\n\nSELECT * WHERE { ?s ?p ?o }\n", encoding="utf-8")
    for prompt in (resolution_prompt(), tools_resolution_prompt()):
        messages = ChatPromptTemplate.from_messages([("system", prompt)]).format_messages()
        assert "{ ?s ?p ?o }" in messages[0].content


def test_escape_braces():
    assert escape_braces("a { b } c") == "a {{ b }} c"


def test_resolver_fallback_names_configured_label_property(norssit_project):
    text = describe_for_tool(EntityResolver(norssit_project.dir / "absent.json"), "Mika Waltari")
    assert "skos:prefLabel" in text
    assert "sdh-short" not in text and "Surname, Firstname" not in text
    preamble = entities_preamble()
    assert "label filter on skos:prefLabel" in preamble
    assert "sdh-short" not in preamble
