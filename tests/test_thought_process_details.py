"""The natural-language "Thought process" must render tool results cleanly.

Tool results carry their own ``` fences. The thought process wrapped each one in
another ``` fence, so the chat's markdown renderer paired them up wrongly: the
result showed as loose text followed by an empty code box. And the executed query
was looked up under an argument called "query", while the tool's argument is
"sparql_query", so it showed as a raw argument dict instead of SPARQL.

Requires the optional ``agent`` extra (langgraph, langfuse…), which CI does not
install, so this skips there and runs in a dev environment set up with
``uv sync --extra agent``.
"""

import asyncio
from typing import Any

import pytest

pytest.importorskip("langgraph", reason="needs the optional 'agent' extra")
pytest.importorskip("langfuse", reason="needs the optional 'agent' extra")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402

from sparql_llm.agent.nodes import call_model as call_model_module  # noqa: E402
from sparql_llm.agent.state import State  # noqa: E402
from sparql_llm.agent.utils import fenced  # noqa: E402

QUERY = "SELECT ?spouse WHERE { ?m <https://sdhss.org/ontology/social-life-core/P15> ?spouse }"
RESULT = 'Results of SPARQL query execution:\n```\n{"spouseName": "Sturzenegger, Lina"}\n```'


def test_plain_text_gets_a_normal_fence():
    assert fenced("rows") == "```\nrows\n```"


def test_text_with_fences_gets_a_longer_fence():
    assert fenced(RESULT).startswith("````\n")
    assert fenced(RESULT).endswith("\n````")


class _FakeModel:
    def bind_tools(self, tools: list[Any]) -> "_FakeModel":
        return self

    async def ainvoke(self, prompt_value: Any, config: Any = None) -> AIMessage:
        return AIMessage(content="Ernst Brenner was married to Lina Sturzenegger.")


class _FakeMCPClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_tools(self) -> list[str]:
        return ["execute_sparql_query"]


def test_thought_process_shows_the_query_and_fences_results(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(call_model_module, "load_chat_model", lambda configuration: _FakeModel())
    monkeypatch.setattr(call_model_module, "MultiServerMCPClient", _FakeMCPClient)
    state = State(
        messages=[
            HumanMessage(content="Who was the spouse of Ernst Brenner?"),
            AIMessage(content="", tool_calls=[{"name": "execute_sparql_query", "args": {"sparql_query": QUERY}, "id": "c1"}]),
            ToolMessage(content=RESULT, tool_call_id="c1", name="execute_sparql_query"),
        ]
    )
    config = RunnableConfig(
        configurable={"use_tools": True, "natural_language_only": True, "max_tool_iterations": 10, "model": "gpustack/fake"}
    )
    result = asyncio.run(call_model_module.call_model(state, config))
    details = result["steps"][0].details
    assert f"**Executed SPARQL query:**\n```sparql\n{QUERY}\n```" in details
    assert f"**Query results:**\n{fenced(RESULT)}" in details
