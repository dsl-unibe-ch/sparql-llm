"""A corrected answer ("fix-message" step) reaches the UI as the streamed text does.

It replaces the streamed answer, so it gets the same treatment: no reasoning blocks,
plain hyphens and spaces, and no SPARQL in natural-language mode.

Requires the optional ``agent`` extra, like the other stream tests.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Annotated, Any

import pytest

pytest.importorskip("langgraph", reason="needs the optional 'agent' extra")
pytest.importorskip("langfuse", reason="needs the optional 'agent' extra")
pytest.importorskip("langchain_mcp_adapters", reason="needs the optional 'agent' extra")

from langgraph.graph import StateGraph

from sparql_llm.agent.main import stream_response
from sparql_llm.agent.state import StepOutput, add_to_list

NB_HYPHEN = "\u2011"
FIXED = f"<think>which query?</think>Perret{NB_HYPHEN}Gentil.\n\n```sparql\nSELECT ?x WHERE {{ ?x ?p ?o }}\n```\n"


@dataclass
class _State:
    steps: Annotated[list[StepOutput], add_to_list] = field(default_factory=list)


def _graph() -> Any:
    def fix(state: _State) -> dict[str, Any]:
        return {"steps": [StepOutput(type="fix-message", label="", fixed_message=FIXED)]}

    builder = StateGraph(_State)
    builder.add_node(fix)
    builder.add_edge("__start__", "fix")
    builder.add_edge("fix", "__end__")
    return builder.compile()


def _fixed_message_sent(natural_language_only: bool) -> str:
    async def run() -> list[str]:
        config = {"configurable": {"natural_language_only": natural_language_only}}
        return [chunk async for chunk in stream_response({}, config, run_graph=_graph())]

    for chunk in asyncio.run(run()):
        if chunk.startswith("data: {"):
            event = json.loads(chunk[len("data: ") :])
            if event["event"] == "updates":
                return event["data"]["fix"]["steps"][0]["fixed_message"]
    raise AssertionError("no fix-message step was streamed")


def test_fixed_answer_has_no_reasoning_and_plain_hyphens():
    sent = _fixed_message_sent(natural_language_only=False)
    assert sent.startswith("Perret-Gentil.")
    assert "```sparql" in sent


def test_fixed_answer_keeps_sparql_out_in_natural_language_mode():
    sent = _fixed_message_sent(natural_language_only=True)
    assert sent.startswith("Perret-Gentil.")
    assert "```sparql" not in sent
