"""The model's message and the streamed answer carry plain hyphens and spaces.

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
pytest.importorskip("langchain_mcp_adapters", reason="needs the optional 'agent' extra")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, add_messages

from sparql_llm.agent.main import stream_response
from sparql_llm.agent.nodes.call_model import normalize_message_typography

NB_HYPHEN = "\u2011"


def test_model_message_text_and_tool_args_are_normalized():
    msg = AIMessage(
        content=f"Perret{NB_HYPHEN}Gentil",
        tool_calls=[{"name": "execute_sparql_query", "args": {"sparql_query": f'"a{NB_HYPHEN}b"'}, "id": "1"}],
    )
    normalize_message_typography(msg)
    assert msg.content == "Perret-Gentil"
    assert msg.tool_calls[0]["args"] == {"sparql_query": '"a-b"'}

    blocks = AIMessage(content=[{"type": "text", "text": f"x{NB_HYPHEN}y"}, f"z{NB_HYPHEN}w"])
    normalize_message_typography(blocks)
    assert blocks.content == [{"type": "text", "text": "x-y"}, "z-w"]


@dataclass
class _State:
    messages: Annotated[list[AnyMessage], add_messages] = field(default_factory=list)


def _graph(answer: str) -> Any:
    async def call_model(state: _State) -> dict[str, Any]:
        model = GenericFakeChatModel(messages=iter([AIMessage(content=answer)]))
        return {"messages": [await model.ainvoke(state.messages or "hi")]}

    builder = StateGraph(_State)
    builder.add_node(call_model)
    builder.add_edge("__start__", "call_model")
    builder.add_edge("call_model", "__end__")
    return builder.compile()


async def _shown(answer: str) -> str:
    shown = ""
    async for raw in stream_response({"messages": []}, RunnableConfig(configurable={}), _graph(answer)):
        line = raw.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            payload = json.loads(line[6:])
            if payload.get("event") == "messages":
                shown += payload["data"][0].get("content", "") or ""
    return shown


def test_streamed_answer_shows_plain_hyphens():
    # The reasoning block keeps the visible text shorter than the buffer: the offsets
    # must still line up after normalization.
    answer = f"<think>Perret{NB_HYPHEN}Gentil?</think>Perret{NB_HYPHEN}Gentil, Charles\u00a0: Brésil"
    # Driven with asyncio.run: pytest-asyncio is not a declared dependency.
    assert asyncio.run(_shown(answer)) == "Perret-Gentil, Charles : Brésil"
