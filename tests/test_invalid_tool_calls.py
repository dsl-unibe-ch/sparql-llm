"""A tool call the model garbled must not end the run with its JSON as the answer.

gpt-oss on GPUStack sometimes writes tool-call arguments that are not valid JSON.
LangChain files such a call under ``invalid_tool_calls`` and leaves ``tool_calls``
empty, so the router took it for a finished answer and the chat showed the raw
JSON (traced: finish_reason "tool_calls", tool_calls []). The call is now
answered with an error so the model can retry, it counts against the step
budget, and its JSON never reaches the chat.

Requires the optional ``agent`` extra (langgraph, langfuse…), which CI does not
install, so this skips there and runs in a dev environment set up with
``uv sync --extra agent``.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Annotated, Any

import pytest

pytest.importorskip("langgraph", reason="needs the optional 'agent' extra")
pytest.importorskip("langfuse", reason="needs the optional 'agent' extra")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import StateGraph, add_messages  # noqa: E402

from sparql_llm.agent.graph import route_tools_output  # noqa: E402
from sparql_llm.agent.main import stream_response  # noqa: E402
from sparql_llm.agent.nodes import mcp_tools as mcp_tools_module  # noqa: E402
from sparql_llm.agent.state import State  # noqa: E402
from sparql_llm.agent.utils import count_tool_rounds  # noqa: E402

# Arguments cut off mid-string, as gpt-oss produced them.
GARBLED = '{"sparql_query": "SELECT * WHERE { ?s ?p ?o }", "endpoint_url": "https://example.org/spa'


def _garbled_call() -> AIMessage:
    return AIMessage(
        content=GARBLED,
        invalid_tool_calls=[
            {
                "name": "execute_sparql_query",
                "args": GARBLED,
                "id": "bad1",
                "error": "Function execute_sparql_query arguments are not valid JSON",
                "type": "invalid_tool_call",
            }
        ],
    )


def _config(max_steps: int = 10) -> RunnableConfig:
    return RunnableConfig(configurable={"use_tools": True, "max_tool_iterations": max_steps, "model": "gpustack/fake"})


def test_a_garbled_call_counts_as_a_tool_round():
    # Otherwise a model that keeps garbling its calls would never hit the budget.
    assert count_tool_rounds([HumanMessage(content="q"), _garbled_call()]) == 1


def test_a_garbled_call_goes_to_the_tools_node():
    state = State(messages=[HumanMessage(content="q"), _garbled_call()])
    assert route_tools_output(state, _config()) == "tools"


def test_a_garbled_call_is_answered_with_an_error_without_calling_mcp(monkeypatch: pytest.MonkeyPatch):
    class _NoMCP:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def session(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a garbled call must not be sent to the MCP server")

    monkeypatch.setattr(mcp_tools_module, "MultiServerMCPClient", _NoMCP)
    state = State(messages=[HumanMessage(content="q"), _garbled_call()])
    result = asyncio.run(mcp_tools_module.mcp_tools_node(state, _config()))
    (reply,) = result["messages"]
    assert isinstance(reply, ToolMessage)
    assert reply.tool_call_id == "bad1"
    assert "not valid JSON" in reply.content


@dataclass
class _StreamState:
    messages: Annotated[list[AnyMessage], add_messages] = field(default_factory=list)


def test_garbled_call_json_is_not_streamed_as_the_answer():
    # gpt-oss runs non-streamed when tools are bound, so the whole message, JSON
    # content and invalid call together, arrives as one event.
    async def call_model(state: _StreamState) -> dict[str, Any]:
        model = GenericFakeChatModel(messages=iter([_garbled_call()]), disable_streaming=True)
        return {"messages": [await model.ainvoke("hi")]}

    builder = StateGraph(_StreamState)
    builder.add_node(call_model)
    builder.add_edge("__start__", "call_model")
    builder.add_edge("call_model", "__end__")
    graph = builder.compile()

    async def shown() -> str:
        text = ""
        async for raw in stream_response({"messages": []}, RunnableConfig(configurable={}), graph):
            line = raw.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            payload = json.loads(line[6:])
            if payload.get("event") == "messages" and payload["data"][0].get("type") == "AIMessageChunk":
                text += payload["data"][0].get("content") or ""
        return text

    assert asyncio.run(shown()) == ""
