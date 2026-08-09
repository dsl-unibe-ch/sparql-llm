"""Utilities for the AI agent, e.g. load model."""

import os

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from sparql_llm.config import Configuration


def load_chat_model(configuration: Configuration) -> BaseChatModel:
    """Load a chat model from a fully specified name.

    Args:
        fully_specified_name (str): String in the format 'provider/model'.
    """
    provider, model_name = configuration.model.split("/", maxsplit=1)
    if provider == "openrouter":
        # https://openrouter.ai/docs/community/lang-chain
        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            model=model_name,
            temperature=configuration.temperature,
            api_key=SecretStr(os.getenv("OPENROUTER_API_KEY") or ""),
            seed=configuration.seed,
            # default_headers={
            #     "HTTP-Referer": getenv("YOUR_SITE_URL"),
            #     "X-Title": getenv("YOUR_SITE_NAME"),
            # },
        )
    if provider == "gpustack":
        # gpt-oss-120b served via GPUStack/vLLM is broken for STREAMING
        # tool-calling requests: with ``stream=true`` and tools bound it
        # intermittently (≈5 out of 6 times) returns an empty completion —
        # finish_reason="stop", no content, no tool calls — because it produced
        # only hidden reasoning and never emitted a final message or tool call.
        # Non-streaming requests are reliable (6/6 return a proper tool call).
        # LangGraph's ``stream_mode="messages"`` (used by the chat UI) forces
        # streaming, so gpt-oss returns nothing and the UI shows a blank reply.
        # ``disable_streaming="tool_calling"`` makes LangChain use a
        # non-streaming request whenever tools are bound, while keeping token
        # streaming for ordinary chat (which gpt-oss handles fine). Other models
        # (minimax, qwen3-coder) stream tool calls correctly, so we only disable
        # streaming for gpt-oss to preserve the nicer live token UX elsewhere.
        disable_streaming: bool | str = "tool_calling" if "gpt-oss" in model_name else False

        # Explicit timeout + max_retries: without these, a hung GPUStack
        # connection waits the openai SDK default of 600 seconds, which looks
        # like "the second question never returns". With them, a stuck call
        # raises after ~90s and the upstream code paths (including the
        # structured-extraction fallback) can recover.
        return ChatOpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", "https://gpustack.unibe.ch/v1"),
            model=model_name,
            temperature=configuration.temperature,
            api_key=SecretStr(os.getenv("OPENAI_API_KEY") or ""),
            seed=configuration.seed,
            timeout=90.0,
            max_retries=1,
            disable_streaming=disable_streaming,
        )

    # if provider == "groq":
    #     # https://python.langchain.com/docs/integrations/chat/groq/
    #     from langchain_groq import ChatGroq

    #     return ChatGroq(
    #         model=model_name,
    #         max_tokens=configuration.max_tokens,
    #         temperature=configuration.temperature,
    #         timeout=None,
    #         max_retries=2,
    #     )
    # if provider == "together":
    #     # https://python.langchain.com/docs/integrations/chat/together/
    #     from langchain_together import ChatTogether
    #     return ChatTogether(
    #         model=model_name,
    #         max_tokens=configuration.max_tokens,
    #         temperature=configuration.temperature,
    #         timeout=None,
    #         max_retries=2,
    #     )
    # if provider == "hf":
    #     # https://python.langchain.com/docs/integrations/chat/huggingface/
    #     from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
    #     return ChatHuggingFace(
    #         llm=HuggingFaceEndpoint(
    #             # repo_id="HuggingFaceH4/zephyr-7b-beta",
    #             repo_id=model_name,
    #             task="text-generation",
    #             max_new_tokens=configuration.max_tokens,
    #             do_sample=False,
    #             repetition_penalty=1.03,
    #         )
    #     )
    # if provider == "azure":
    #     # https://learn.microsoft.com/en-us/azure/ai-studio/how-to/develop/langchain
    #     from langchain_azure_ai.chat_models import AzureAIChatCompletionsModel
    #     return AzureAIChatCompletionsModel(
    #         endpoint=settings.azure_inference_endpoint,
    #         credential=settings.azure_inference_credential,
    #         model_name=model_name,
    #     )
    # if provider == "deepseek":
    #     # https://python.langchain.com/docs/integrations/chat/deepseek/
    #     from langchain_deepseek import ChatDeepSeek
    #     return ChatDeepSeek(
    #         model=model_name,
    #         temperature=configuration.temperature,
    #     )
    return init_chat_model(
        model_name,
        model_provider=provider,
        max_tokens=configuration.max_tokens,
        temperature=configuration.temperature,
        timeout=None,
        max_retries=2,
        seed=configuration.seed,
        # reasoning={
        #     "effort": "low",  # 'low', 'medium', or 'high'
        #     "summary": "auto",  # 'detailed', 'auto', or None
        # },
    )


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
