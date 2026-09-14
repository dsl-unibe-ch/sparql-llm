"""Utilities for the AI agent, e.g. load model."""

import os
import re
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from sparql_llm.config import Configuration


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


def load_chat_model(configuration: Configuration) -> BaseChatModel:
    """Load the chat model named by ``configuration.model``, in the format 'provider/model'."""
    provider, model_name = configuration.model.split("/", maxsplit=1)
    if provider == "openrouter":
        # https://openrouter.ai/docs/community/lang-chain
        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            model=model_name,
            temperature=configuration.temperature,
            api_key=SecretStr(os.getenv("OPENROUTER_API_KEY") or ""),
            seed=configuration.seed,
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
            base_url=os.getenv("OPENAI_BASE_URL", "https://gpustack.unibe.ch/v1"),
            model=model_name,
            temperature=configuration.temperature,
            api_key=SecretStr(os.getenv("OPENAI_API_KEY") or ""),
            seed=configuration.seed,
            timeout=90.0,
            max_retries=1,
            disable_streaming=disable_streaming,
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
