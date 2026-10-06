"""Registry of known LLM providers and their defaults.

Each entry describes a provider the user can select in the API Settings page.
The ``default_base_url`` is pre-filled in the UI; the user can override it
for the "custom" provider.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from sparql_llm.utils import logger


@dataclass(frozen=True)
class ProviderInfo:
    """Immutable metadata for a known LLM provider."""

    label: str
    default_base_url: str
    help_text: str


PROVIDERS: dict[str, ProviderInfo] = {
    "gpustack": ProviderInfo(
        label="GPUStack (University of Bern)",
        default_base_url="https://gpustack.unibe.ch/v1",
        help_text="For University of Bern users. Enter your GPUStack API key.",
    ),
    "openai": ProviderInfo(
        label="OpenAI",
        default_base_url="https://api.openai.com/v1",
        help_text="Use your own OpenAI API key.",
    ),
    "custom": ProviderInfo(
        label="Custom (OpenAI-compatible)",
        default_base_url="",
        help_text=(
            "Any OpenAI-compatible API (e.g. Ollama, LM Studio, Together AI). "
            "Provide the base URL and API key."
        ),
    ),
}


# OpenAI model ID prefixes that correspond to chat models.
# Used to filter out embedding, TTS, whisper, and other non-chat models.
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1-", "o3-", "o4-", "chatgpt-")


def _is_chat_model(model_id: str, provider: str) -> bool:
    """Heuristic: is *model_id* a chat/completion model?

    GPUStack only serves what is deployed, so everything is accepted.
    OpenAI's ``/v1/models`` returns embedding, TTS, DALL·E, etc. — keep only
    known chat prefixes.
    """
    if provider != "openai":
        return True
    return model_id.startswith(_OPENAI_CHAT_PREFIXES)


async def validate_and_list_models(
    base_url: str,
    api_key: str,
    provider: str,
) -> list[str]:
    """Call ``GET /v1/models`` with the given credentials.

    Returns a list of model IDs formatted as ``provider/model-name``.
    Raises ``httpx.HTTPStatusError`` on 401/403 (invalid key) or network errors.
    """
    url = f"{base_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        data = resp.json()

    raw_models: list[dict] = data.get("data", [])
    model_ids: list[str] = sorted(
        m["id"] for m in raw_models if _is_chat_model(m.get("id", ""), provider)
    )
    logger.info("Provider %s at %s: %d chat models found", provider, base_url, len(model_ids))
    return [f"{provider}/{mid}" for mid in model_ids]
