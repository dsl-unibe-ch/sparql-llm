"""Utilities for the AI agent, e.g. load model."""

import functools
import os
import re
import threading
from collections.abc import Sequence
from typing import Any

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from sparql_llm.config import Configuration
from sparql_llm.outbound import guarded_async_client, guarded_client


class GptOssChatOpenAI(ChatOpenAI):
    """ChatOpenAI for gpt-oss on GPUStack, recovering answers filed as reasoning.

    With tools bound and streaming off (see ``disable_streaming`` below), GPUStack
    sometimes returns gpt-oss's final answer in ``reasoning_content``, which
    ChatOpenAI ignores, and leaves ``content`` empty. A normal reply fills ``content``
    and keeps genuine reasoning in ``reasoning_content``, so only a reply with neither
    content nor tool calls takes its ``reasoning_content`` as content.
    """

    def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> ChatResult:
        response_dict = response if isinstance(response, dict) else response.model_dump()
        for choice in response_dict.get("choices") or []:
            message = choice.get("message") or {}
            if not message.get("content") and not message.get("tool_calls") and message.get("reasoning_content"):
                message["content"] = message["reasoning_content"]
        return super()._create_chat_result(response_dict, generation_info)


@functools.lru_cache(maxsize=1)
def chat_http_client() -> httpx.Client:
    """The client for every chat model with a user-set URL, shared so models built per request reuse connections."""
    return guarded_client("chat", follow_redirects=True)


@functools.lru_cache(maxsize=1)
def chat_async_http_client() -> httpx.AsyncClient:
    """The async counterpart of :func:`chat_http_client`."""
    return guarded_async_client("chat", follow_redirects=True)


class TokenUsage(BaseCallbackHandler):
    """Tokens spent on one chat request, summed over every LLM call it made.

    One answer takes several calls: the question extraction, then a call per
    validation retry or tool round. Users who bring a paid key see the total
    under the answer. A call whose provider reports no counts is still counted
    as a call, so the chat can say the total is incomplete.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.calls = 0
        self.unreported_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        message = generation.message if isinstance(generation, ChatGeneration) else None
        usage = message.usage_metadata if isinstance(message, AIMessage) else None
        with self._lock:
            self.calls += 1
            if not usage:
                self.unreported_calls += 1
                return
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)
            self.reasoning_tokens += (usage.get("output_token_details") or {}).get("reasoning", 0)

    def summary(self) -> dict[str, Any] | None:
        """The totals as sent to the chat, or None when no LLM call was made."""
        if not self.calls:
            return None
        reported = self.calls > self.unreported_calls
        return {
            "calls": self.calls,
            "unreported_calls": self.unreported_calls,
            "input_tokens": self.input_tokens if reported else None,
            "output_tokens": self.output_tokens if reported else None,
            "reasoning_tokens": self.reasoning_tokens if reported else None,
        }


def load_chat_model(configuration: Configuration) -> BaseChatModel:
    """Load the chat model named by ``configuration.model``, in the format 'provider/model'.

    Per-user credentials (``llm_api_key``, ``llm_base_url``) are read from
    *configuration*, which is populated from the ``RunnableConfig`` at request
    time.  Falls back to environment variables for backwards compatibility
    (e.g. CLI usage, tests).

    ``stream_usage=True`` asks for token counts on streamed replies too: ChatOpenAI
    only does so by itself for the default OpenAI URL and client, and the chat shows
    the counts under each answer (see :class:`TokenUsage`).
    """
    provider, model_name = configuration.model.split("/", maxsplit=1)

    # Per-user credentials injected via RunnableConfig → Configuration
    api_key = configuration.llm_api_key or os.getenv("OPENAI_API_KEY") or ""
    base_url = configuration.llm_base_url or os.getenv("OPENAI_BASE_URL", "")

    # Some reasoning models (e.g., minimax, deepseek-r1, o1, o3) strictly reject temperature 
    # parameters or require them to be exactly 1.0. To avoid BadRequestErrors, we omit it 
    # (pass None) for these models so the provider uses its default.
    reasoning_prefixes = ("o1", "o3", "minimax", "deepseek", "reasoning")
    safe_temperature = None if any(rm in model_name.lower() for rm in reasoning_prefixes) else configuration.temperature

    if provider == "openrouter":
        # https://openrouter.ai/docs/community/lang-chain
        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            model=model_name,
            temperature=safe_temperature,
            api_key=SecretStr(os.getenv("OPENROUTER_API_KEY") or ""),
            seed=configuration.seed,
            stream_usage=True,
            http_client=chat_http_client(),
            http_async_client=chat_async_http_client(),
            openai_proxy=None,  # With a client passed, ChatOpenAI refuses to start if OPENAI_PROXY is set
        )
    if provider == "openai":
        return ChatOpenAI(
            base_url=base_url or "https://api.openai.com/v1",
            model=model_name,
            temperature=safe_temperature,
            api_key=SecretStr(api_key),
            seed=configuration.seed,
            stream_usage=True,
            timeout=120.0,
            max_retries=1,
            http_client=chat_http_client(),
            http_async_client=chat_async_http_client(),
            openai_proxy=None,  # With a client passed, ChatOpenAI refuses to start if OPENAI_PROXY is set
        )
    if provider == "gpustack":
        # With tools bound, gpt-oss on GPUStack/vLLM often returns an empty completion
        # to a streamed request but answers reliably when not streamed. So it streams
        # only when no tools are bound; the other models stream tool calls fine.
        disable_streaming: bool | str = "tool_calling" if "gpt-oss" in model_name else False

        # The openai SDK would wait 600 s on a hung connection; fail after ~90 s
        # instead, so the callers' fallbacks can recover.
        chat_class = GptOssChatOpenAI if "gpt-oss" in model_name else ChatOpenAI
        return chat_class(
            base_url=base_url or "https://gpustack.unibe.ch/v1",
            model=model_name,
            temperature=safe_temperature,
            api_key=SecretStr(api_key),
            seed=configuration.seed,
            stream_usage=True,
            timeout=90.0,
            max_retries=1,
            disable_streaming=disable_streaming,
            http_client=chat_http_client(),
            http_async_client=chat_async_http_client(),
            openai_proxy=None,  # With a client passed, ChatOpenAI refuses to start if OPENAI_PROXY is set
        )
    if provider == "custom":
        # Generic OpenAI-compatible endpoint (e.g. Ollama, LM Studio, Together AI)
        return ChatOpenAI(
            base_url=base_url,
            model=model_name,
            temperature=safe_temperature,
            api_key=SecretStr(api_key),
            seed=configuration.seed,
            stream_usage=True,
            timeout=120.0,
            max_retries=1,
            http_client=chat_http_client(),
            http_async_client=chat_async_http_client(),
            openai_proxy=None,  # With a client passed, ChatOpenAI refuses to start if OPENAI_PROXY is set
        )
    return init_chat_model(
        model_name,
        model_provider=provider,
        max_tokens=configuration.max_tokens,
        temperature=configuration.temperature,
        timeout=None,
        max_retries=2,
        seed=configuration.seed,
    )


def count_tool_rounds(messages: list[AnyMessage]) -> int:
    """Count tool-call rounds (exploration steps): one per AIMessage that requested tools.

    Calls whose arguments could not be parsed (``invalid_tool_calls``) count too,
    or a model that keeps garbling its calls would never reach the budget.
    """
    return sum(1 for m in messages if isinstance(m, AIMessage) and (m.tool_calls or m.invalid_tool_calls))


def fenced(text: str, lang: str = "") -> str:
    """Wrap text in a markdown code fence longer than any backtick run inside it.

    Tool results carry their own ``` fences, which a plain ``` wrapper would close early.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def get_msg_text(msg: AnyMessage) -> str:
    """Get the text content of a chat message."""
    content = msg.content
    if isinstance(content, str):
        return content
    elif isinstance(content, dict):
        return content.get("text", "")
    else:
        txts = [c if isinstance(c, str) else (c.get("text") or "") for c in content]
        return "".join(txts).strip()


def latest_user_question(messages: Sequence[AnyMessage]) -> str:
    """Text of the user's latest message, skipping messages the graph's nodes added (they carry a name)."""
    for msg in reversed(messages):
        if msg.type == "human" and not getattr(msg, "name", None):
            return get_msg_text(msg)
    return get_msg_text(messages[-1]) if messages else ""
