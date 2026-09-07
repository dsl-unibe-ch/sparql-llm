"""The display filter must not rewrite the model's message in graph state.

``stream_response`` hides reasoning and (in natural-language mode) SPARQL from
the chat body. Those are *display* concerns. The chunk objects yielded by
``astream(stream_mode="messages")`` are the same objects LangGraph aggregates
into the node's returned AIMessage, so filtering them in place rewrote the
assistant's own turn in the conversation history: on the next tool round the
model saw a history in which it had never produced a query, ran more tools and
answered again, producing duplicated answers in the UI.

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
from langchain_core.messages import AIMessage, AnyMessage  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import StateGraph, add_messages  # noqa: E402

from sparql_llm.agent.main import stream_response  # noqa: E402

ANSWER = "Spouse was Lina.\n```sparql\nASK {}\n```\nMore?"


@dataclass
class _State:
    messages: Annotated[list[AnyMessage], add_messages] = field(default_factory=list)


def _build_graph() -> Any:
    async def call_model(state: _State) -> dict[str, Any]:
        model = GenericFakeChatModel(messages=iter([AIMessage(content=ANSWER)]))
        return {"messages": [await model.ainvoke(state.messages or "hi")]}

    builder = StateGraph(_State)
    builder.add_node(call_model)
    builder.add_edge("__start__", "call_model")
    builder.add_edge("call_model", "__end__")
    return builder.compile()


async def _stream(natural_language_only: bool) -> tuple[str, str]:
    """Return (text shown in the chat body, text recorded in graph state)."""
    config = RunnableConfig(configurable={"natural_language_only": natural_language_only})
    shown = ""
    stored = ""
    async for line in stream_response({"messages": []}, config, _build_graph()):
        line = line.strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = json.loads(line[6:])
        if payload.get("event") == "messages":
            shown += payload["data"][0].get("content", "") or ""
        elif payload.get("event") == "updates":
            for node_data in payload.get("data", {}).values():
                for msg in (node_data or {}).get("messages", []) or []:
                    stored += msg.get("content", "") or ""
    return shown, stored


def _run(natural_language_only: bool) -> tuple[str, str]:
    # Driven with asyncio.run rather than an async test: pytest-asyncio is not a
    # declared dependency, so an `async def` test errors out instead of running.
    return asyncio.run(_stream(natural_language_only))


def test_natural_language_mode_hides_sparql_from_the_chat_body():
    shown, _ = _run(natural_language_only=True)
    assert "ASK {}" not in shown
    assert shown == "Spouse was Lina.\nMore?"


def test_natural_language_mode_leaves_graph_state_intact():
    # The regression: the model's own turn must survive verbatim, or the next
    # tool round sees a history it never wrote.
    _, stored = _run(natural_language_only=True)
    assert stored == ANSWER


def test_default_mode_leaves_graph_state_intact():
    # Same guarantee without the natural-language filter, where only <think>
    # stripping runs.
    shown, stored = _run(natural_language_only=False)
    assert stored == ANSWER
    assert "ASK {}" in shown
