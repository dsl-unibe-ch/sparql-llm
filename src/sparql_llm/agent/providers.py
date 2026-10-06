"""Registry of known LLM providers and their defaults.

Each entry describes a provider the user can select in the API Settings page.
The ``default_base_url`` is pre-filled in the UI; the user can override it
for the "custom" provider.
"""

from __future__ import annotations

import asyncio
import re
import time
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


# OpenAI chat model IDs: gpt-*, chatgpt-*, and the o-series reasoning models
# (o1, o3-mini, o4-mini, ...). Non-chat gpt-* variants are caught by the
# reject patterns below (gpt-image-1, gpt-4o-transcribe, gpt-4o-mini-tts, ...).
_OPENAI_CHAT_RE = re.compile(r"^(gpt-|chatgpt-|o\d+(-|$))")

# Patterns matched anywhere in the lowercased model ID.
# Only distinctive keywords that won't collide with LLM names.
_REJECT_SUBSTRINGS = (
    # Embeddings / retrieval
    "embed", "rerank", "colbert", "minilm", "mpnet", "sentence-transformers",
    # Audio
    "whisper", "tts", "transcribe", "speech", "wav2vec", "parakeet", "kokoro",
    "realtime", "audio",
    # Images / video
    "diffusion", "sdxl", "dall-e", "dalle", "flux", "kandinsky", "imagen",
    "gpt-image", "sora",
    # Moderation / safety classifiers
    "moderation", "llama-guard", "shieldgemma",
)

# Short keywords that must appear as whole tokens (delimited by - _ / . : or
# string boundaries), otherwise they'd match inside unrelated names.
_REJECT_TOKENS_RE = re.compile(
    r"(?:^|[-_/.:])(bge|e5|gte|nomic|mxbai|jina|clip|siglip|stt|asr|sd[0-9.]*|ocr[0-9]*|tts)(?=$|[-_/.:])"
)

# Values of model metadata fields that indicate a non-chat model.
_NON_CHAT_TYPES = {
    "embedding", "embeddings", "rerank", "reranker", "image", "audio",
    "transcribe", "moderation", "speech_to_text", "text_to_speech", "tts", "stt",
}


def _metadata_says_chat(model: dict) -> bool | None:
    """Use type info some OpenAI-compatible servers attach to ``/v1/models``.

    Returns True/False if the metadata is conclusive, None otherwise.
    - Together AI / LM Studio: ``type`` ("chat", "llm", "embedding", ...)
    - GPUStack: ``categories`` (["llm"], ["embedding"], ["reranker"], ...)
    - OpenRouter: ``architecture.output_modalities`` (["text"], ["image"], ...)
    """
    types: list[str] = []
    for key in ("type", "model_type", "task", "categories"):
        value = model.get(key)
        if isinstance(value, str):
            types.append(value.lower())
        elif isinstance(value, list):
            types.extend(str(v).lower() for v in value)
    if types:
        return not any(t in _NON_CHAT_TYPES for t in types)

    arch = model.get("architecture")
    if isinstance(arch, dict) and isinstance(arch.get("output_modalities"), list):
        return "text" in arch["output_modalities"]
    return None


def _is_chat_model(model: dict, provider: str) -> bool:
    """Heuristic: is this ``/v1/models`` entry a chat/completion model?

    Prefers explicit type metadata when the server provides it, then falls back
    to name-based filtering of embedding, audio, image and moderation models.
    For OpenAI, additionally require a known chat model prefix.
    """
    model_id = str(model.get("id", ""))
    if not model_id:
        return False
    model_lower = model_id.lower()

    from_metadata = _metadata_says_chat(model)
    if from_metadata is not None:
        return from_metadata

    if any(sub in model_lower for sub in _REJECT_SUBSTRINGS):
        return False
    if _REJECT_TOKENS_RE.search(model_lower):
        return False

    if provider == "openai":
        return bool(_OPENAI_CHAT_RE.match(model_lower))
    return True


# Tool-calling probe results, keyed by (base_url, model_id) -> (supports_tools, checked_at).
# Only conclusive results are cached; transient failures are retried next time.
_TOOL_SUPPORT_CACHE: dict[tuple[str, str], tuple[bool, float]] = {}
_TOOL_SUPPORT_TTL = 6 * 3600
_TOOL_PROBE_TIMEOUT = 20.0
_TOOL_PROBE_CONCURRENCY = 8

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Health check. Do not call.",
        "parameters": {"type": "object", "properties": {}},
    },
}


async def _supports_tool_calling(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model_id: str,
) -> bool:
    """Probe whether *model_id* accepts ``tools`` with ``tool_choice="auto"``.

    Sends a minimal chat completion (1 output token). Servers such as vLLM
    reject it with HTTP 400 when the model was started without
    ``--enable-auto-tool-choice``/``--tool-call-parser``. Any other outcome
    (success, timeout, 5xx, rate limit) is treated as "supported" so that
    transient errors never hide a working model.
    """
    key = (base_url, model_id)
    cached = _TOOL_SUPPORT_CACHE.get(key)
    if cached and time.monotonic() - cached[1] < _TOOL_SUPPORT_TTL:
        return cached[0]

    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [_PROBE_TOOL],
                "tool_choice": "auto",
                "max_tokens": 1,
            },
            timeout=_TOOL_PROBE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.warning("Tool-calling probe for %s failed (%s); assuming supported", model_id, exc)
        return True

    try:
        body = resp.json()
    except ValueError:
        body = None

    if resp.status_code in (400, 422) and "tool" in resp.text.lower():
        supported = False
    elif resp.is_success and isinstance(body, dict) and "error" in body:
        # Some servers (e.g. GPUStack) wrap upstream errors in an HTTP 200, such as
        # "The model does not support Chat Completions API" -- the model is unusable.
        logger.info("Tool-calling probe for %s returned an error body: %s", model_id, body["error"])
        supported = False
    elif resp.is_success:
        supported = True
    else:
        logger.warning(
            "Tool-calling probe for %s returned %d; assuming supported", model_id, resp.status_code
        )
        return True

    _TOOL_SUPPORT_CACHE[key] = (supported, time.monotonic())
    if not supported:
        logger.info("Model %s at %s does not support tool calling; hiding it", model_id, base_url)
    return supported


async def validate_and_list_models(
    base_url: str,
    api_key: str,
    provider: str,
) -> list[str]:
    """Call ``GET /v1/models`` with the given credentials.

    For non-OpenAI providers, models that reject tool calling are filtered out
    (see :func:`_supports_tool_calling`).
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
            m["id"] for m in raw_models if _is_chat_model(m, provider)
        )

        # OpenAI chat models all support tools; other servers depend on how
        # each model was deployed, so probe them.
        if provider != "openai" and model_ids:
            semaphore = asyncio.Semaphore(_TOOL_PROBE_CONCURRENCY)

            async def probe(mid: str) -> bool:
                async with semaphore:
                    return await _supports_tool_calling(client, base_url, api_key, mid)

            supported = await asyncio.gather(*(probe(mid) for mid in model_ids))
            model_ids = [mid for mid, ok in zip(model_ids, supported) if ok]

    logger.info("Provider %s at %s: %d chat models found", provider, base_url, len(model_ids))
    return [f"{provider}/{mid}" for mid in model_ids]
