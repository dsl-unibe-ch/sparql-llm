"""The MCP tools agent must finish with an answer when its step budget runs out.

Tracing the tools graph showed a model that never finds its answer burning every
exploration step and then ending on a canned "maximum number of exploration
steps" message, discarding everything it had found. Once the budget is used up,
the model is now told to answer from the results it already has.

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
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402

from sparql_llm.agent import prompts  # noqa: E402
from sparql_llm.agent.graph import route_tools_output  # noqa: E402
from sparql_llm.agent.nodes import call_model as call_model_module  # noqa: E402
from sparql_llm.agent.state import State  # noqa: E402
from sparql_llm.agent.utils import count_tool_rounds  # noqa: E402


def _history(rounds: int) -> list[Any]:
    """A question followed by `rounds` completed tool rounds."""
    messages: list[Any] = [HumanMessage(content="Who was the spouse of Ernst Brenner?")]
    for i in range(rounds):
        messages.append(
            AIMessage(content="", tool_calls=[{"name": "execute_sparql_query", "args": {}, "id": f"c{i}"}])
        )
        messages.append(ToolMessage(content="results", tool_call_id=f"c{i}", name="execute_sparql_query"))
    return messages


def _config(max_steps: int) -> RunnableConfig:
    return RunnableConfig(
        configurable={"use_tools": True, "max_tool_iterations": max_steps, "model": "gpustack/fake"}
    )


class _FakeModel:
    """Records what call_model binds and sends; answers with a fixed message."""

    def __init__(self) -> None:
        self.bound_tools: list[Any] | None = None
        self.sent: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> "_FakeModel":
        self.bound_tools = tools
        return self

    async def ainvoke(self, prompt_value: Any, config: Any = None) -> AIMessage:
        self.sent = prompt_value.messages
        return AIMessage(content="Lina Sturzenegger.")


class _FakeMCPClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_tools(self) -> list[str]:
        return ["execute_sparql_query"]


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    model = _FakeModel()
    monkeypatch.setattr(call_model_module, "load_chat_model", lambda configuration: model)
    monkeypatch.setattr(call_model_module, "MultiServerMCPClient", _FakeMCPClient)
    return model


def test_count_tool_rounds_counts_only_ai_messages_that_requested_tools():
    assert count_tool_rounds(_history(0)) == 0
    assert count_tool_rounds(_history(3)) == 3


def test_route_runs_tools_within_budget():
    state = State(messages=[*_history(1), AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "n"}])])
    assert route_tools_output(state, _config(max_steps=2)) == "tools"


def test_route_ends_when_the_model_answers():
    state = State(messages=[*_history(2), AIMessage(content="Lina Sturzenegger.")])
    assert route_tools_output(state, _config(max_steps=2)) == "__end__"


def test_route_stops_a_tool_call_past_the_budget():
    # Safety net: the final turn is tool-free, so this only fires if a model
    # emits a tool call anyway.
    state = State(messages=[*_history(2), AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "n"}])])
    assert route_tools_output(state, _config(max_steps=2)) == "max_tries_reached"


def test_within_budget_the_model_is_not_told_to_stop(fake_model: _FakeModel):
    asyncio.run(call_model_module.call_model(State(messages=_history(1)), _config(max_steps=2)))
    assert fake_model.bound_tools == ["execute_sparql_query"]
    assert fake_model.sent[-1].content != prompts.FINAL_TURN_PROMPT


def test_budget_used_up_tells_the_model_to_answer_now(fake_model: _FakeModel):
    result = asyncio.run(call_model_module.call_model(State(messages=_history(2)), _config(max_steps=2)))
    # The instruction is the last thing the model reads...
    assert fake_model.sent[-1].content == prompts.FINAL_TURN_PROMPT
    # ...and tools stay bound, so a stray call is parsed and stopped by the router
    # rather than leaking into the answer as raw tool-call markup.
    assert fake_model.bound_tools == ["execute_sparql_query"]
    # Only the answer is stored in the conversation, not the instruction.
    assert [m.content for m in result["messages"]] == ["Lina Sturzenegger."]


def test_natural_language_mode_does_not_ask_for_think_blocks(fake_model: _FakeModel):
    # qwen on GPUStack returns its reasoning on a separate channel and cannot write
    # into a <think> block; asking it to made it answer and then restart the search.
    config = RunnableConfig(configurable={**_config(max_steps=5)["configurable"], "natural_language_only": True})
    asyncio.run(call_model_module.call_model(State(messages=_history(1)), config))
    system_prompt = fake_model.sent[0].content
    assert "NATURAL LANGUAGE ONLY MODE" in system_prompt
    assert "<think>" not in system_prompt


def test_tools_prompt_renders_as_a_template():
    # The system prompt goes through ChatPromptTemplate, so a stray brace in the
    # prompt text would be read as a template variable and fail every request.
    ChatPromptTemplate.from_messages([("system", prompts.TOOLS_RESOLUTION_PROMPT)]).invoke({})
