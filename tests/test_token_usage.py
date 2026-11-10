"""The chat shows the tokens an answer took, summed over every LLM call it made.

Users can bring a paid API key, and one answer takes several calls (the question
extraction, then a call per validation retry or tool round), so the last call
alone would understate what they paid. ``stream_response`` sends the totals as a
last "usage" event, which the UI shows under the answer.

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

from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AnyMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import StateGraph, add_messages  # noqa: E402

from sparql_llm.agent.main import stream_response  # noqa: E402
from sparql_llm.agent.utils import TokenUsage  # noqa: E402


class _CountingModel(BaseChatModel):
    """Answers "ok" and reports the given usage, or none."""

    usage: dict[str, Any] | None = None

    @property
    def _llm_type(self) -> str:
        return "counting"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok", usage_metadata=self.usage))])


@dataclass
class _State:
    messages: Annotated[list[AnyMessage], add_messages] = field(default_factory=list)


def _graph(*models: _CountingModel, fail: bool = False) -> Any:
    """One node calling each model in turn, like extraction then call_model."""

    async def call_model(state: _State) -> dict[str, Any]:
        replies = [await model.ainvoke("hi") for model in models]
        if fail:
            raise RuntimeError("model server went away")
        return {"messages": replies}

    builder = StateGraph(_State)
    builder.add_node(call_model)
    builder.add_edge("__start__", "call_model")
    builder.add_edge("call_model", "__end__")
    return builder.compile()


def _usage_event(run_graph: Any) -> dict[str, Any] | None:
    async def collect() -> dict[str, Any] | None:
        usage = TokenUsage()
        config = RunnableConfig(callbacks=[usage])
        found = None
        async for line in stream_response({"messages": []}, config, run_graph, usage):
            line = line.strip()
            if line.startswith("data: {"):
                chunk = json.loads(line[len("data: ") :])
                if chunk["event"] == "usage":
                    found = chunk["data"]
        return found

    return asyncio.run(collect())


def _reports(input_tokens: int, output_tokens: int, reasoning: int = 0) -> _CountingModel:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if reasoning:
        usage["output_token_details"] = {"reasoning": reasoning}
    return _CountingModel(usage=usage)


def test_usage_is_summed_over_every_call() -> None:
    usage = _usage_event(_graph(_reports(1000, 50), _reports(4000, 800, reasoning=300)))
    assert usage == {
        "calls": 2,
        "unreported_calls": 0,
        "input_tokens": 5000,
        "output_tokens": 850,
        "reasoning_tokens": 300,
    }


def test_calls_without_counts_are_said_not_zeroed() -> None:
    assert _usage_event(_graph(_CountingModel(), _CountingModel())) == {
        "calls": 2,
        "unreported_calls": 2,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
    }
    partly = _usage_event(_graph(_reports(100, 10), _CountingModel()))
    assert partly is not None
    assert (partly["input_tokens"], partly["unreported_calls"]) == (100, 1)


def test_usage_is_sent_after_a_failure_too() -> None:
    """Calls made before the error were paid for."""
    usage = _usage_event(_graph(_reports(1000, 50), fail=True))
    assert usage is not None and usage["input_tokens"] == 1000


def test_no_event_without_an_llm_call() -> None:
    assert _usage_event(_graph()) is None


def test_chat_models_ask_for_usage_when_streaming() -> None:
    """ChatOpenAI leaves streamed usage off with a custom URL or client, which every provider here sets."""
    from sparql_llm.agent.utils import load_chat_model
    from sparql_llm.config import Configuration

    for model in ("gpustack/qwen3.8-27b", "gpustack/gpt-oss-120b", "openai/gpt-4o-mini", "custom/llama3"):
        config = Configuration(model=model, llm_api_key="k", llm_base_url="https://llm.example/v1")
        assert load_chat_model(config).stream_usage is True, model  # type: ignore[attr-defined]
