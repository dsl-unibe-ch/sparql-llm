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

from sparql_llm.outbound import guarded_async_client
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
            "Any OpenAI-compatible API (e.g. Ollama, LM Studio, Together AI). Provide the base URL and API key."
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
    "embed",
    "rerank",
    "colbert",
    "minilm",
    "mpnet",
    "sentence-transformers",
    # Audio
    "whisper",
    "tts",
    "transcribe",
    "speech",
    "wav2vec",
    "parakeet",
    "kokoro",
    "realtime",
    "audio",
    # Images / video
    "diffusion",
    "sdxl",
    "dall-e",
    "dalle",
    "flux",
    "kandinsky",
    "imagen",
    "gpt-image",
    "sora",
    # Moderation / safety classifiers
    "moderation",
    "llama-guard",
    "shieldgemma",
)

# Short keywords that must appear as whole tokens (delimited by - _ / . : or
# string boundaries), otherwise they'd match inside unrelated names.
_REJECT_TOKENS_RE = re.compile(
    r"(?:^|[-_/.:])(bge|e5|gte|nomic|mxbai|jina|clip|siglip|stt|asr|sd[0-9.]*|ocr[0-9]*|tts)(?=$|[-_/.:])"
)

# Values of model metadata fields that indicate a non-chat model.
_NON_CHAT_TYPES = {
    "embedding",
    "embeddings",
    "rerank",
    "reranker",
    "image",
    "audio",
    "transcribe",
    "moderation",
    "speech_to_text",
    "text_to_speech",
    "tts",
    "stt",
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


# Tool-calling probe results, keyed by (base_url, model_id) -> (supports_tools, expires_at).
# A conclusive answer is kept for hours. An inconclusive one (timeout, 5xx) counts as
# "supported" and is kept for minutes, so a model that hangs is not probed again on
# every page load: on 2026-11-17 a hanging gpt-oss made each /models call wait 20 s.
_TOOL_SUPPORT_CACHE: dict[tuple[str, str], tuple[bool, float]] = {}
_TOOL_SUPPORT_TTL = 6 * 3600
_TOOL_PROBE_RETRY_AFTER = 600
_TOOL_PROBE_TIMEOUT = 20.0
# How long a model list waits for probes. A model still unanswered by then is listed
# (as an inconclusive probe would) and its probe goes on in the background.
_TOOL_PROBE_WAIT = 3.0
# Probes running, so that page loads close together share one probe per model.
_PROBES_IN_FLIGHT: dict[tuple[str, str], asyncio.Task[bool]] = {}


def _cached_tool_support(base_url: str, model_id: str) -> bool | None:
    cached = _TOOL_SUPPORT_CACHE.get((base_url, model_id))
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    return None


def _remember_tool_support(base_url: str, model_id: str, supported: bool, ttl: float) -> bool:
    _TOOL_SUPPORT_CACHE[(base_url, model_id)] = (supported, time.monotonic() + ttl)
    return supported


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
    cached = _cached_tool_support(base_url, model_id)
    if cached is not None:
        return cached

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
        # The class name too: a timeout's message is empty.
        logger.warning(
            "Tool-calling probe for %s failed (%s: %s); assuming supported", model_id, type(exc).__name__, exc
        )
        return _remember_tool_support(base_url, model_id, True, _TOOL_PROBE_RETRY_AFTER)

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
        logger.warning("Tool-calling probe for %s returned %d; assuming supported", model_id, resp.status_code)
        return _remember_tool_support(base_url, model_id, True, _TOOL_PROBE_RETRY_AFTER)

    _remember_tool_support(base_url, model_id, supported, _TOOL_SUPPORT_TTL)
    if not supported:
        logger.info("Model %s at %s does not support tool calling; hiding it", model_id, base_url)
    return supported


async def _probe_in_background(base_url: str, api_key: str, model_id: str) -> bool:
    """One probe with its own client, so it can outlive the request that started it."""
    try:
        async with guarded_async_client("byok") as client:
            return await _supports_tool_calling(client, base_url, api_key, model_id)
    except Exception as exc:  # a refusal or anything else: never hide a model over it
        logger.warning("Tool-calling probe for %s failed (%s); assuming supported", model_id, type(exc).__name__)
        return True
    finally:
        _PROBES_IN_FLIGHT.pop((base_url, model_id), None)


async def _tool_support(base_url: str, api_key: str, model_ids: list[str]) -> dict[str, bool]:
    """Which of *model_ids* accept tool calls, waiting at most ``_TOOL_PROBE_WAIT`` seconds.

    A model whose probe has not answered by then counts as supported, as a probe that
    times out does; the probe goes on and caches its answer for the next model list.
    """
    verdicts: dict[str, bool] = {}
    running: dict[str, asyncio.Task[bool]] = {}
    for mid in model_ids:
        cached = _cached_tool_support(base_url, mid)
        if cached is not None:
            verdicts[mid] = cached
            continue
        task = _PROBES_IN_FLIGHT.get((base_url, mid))
        if task is None:
            task = asyncio.create_task(_probe_in_background(base_url, api_key, mid))
            _PROBES_IN_FLIGHT[(base_url, mid)] = task
        running[mid] = task
    if running:
        await asyncio.wait(running.values(), timeout=_TOOL_PROBE_WAIT)
        for mid, task in running.items():
            verdicts[mid] = task.result() if task.done() and not task.cancelled() else True
    return verdicts


async def validate_and_list_models(
    base_url: str,
    api_key: str,
    provider: str,
) -> list[str]:
    """Call ``GET /v1/models`` with the given credentials.

    For non-OpenAI providers, models that reject tool calling are filtered out
    (see :func:`_supports_tool_calling`).
    Returns a list of model IDs formatted as ``provider/model-name``.
    Raises ``httpx.HTTPStatusError`` on 401/403 (invalid key) or network errors, and
    ``DestinationRefused`` when the outbound policy refuses ``base_url``.
    """
    url = f"{base_url.rstrip('/')}/models"
    async with guarded_async_client("byok", timeout=15.0) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        data = resp.json()

        # Ollama with no model pulled sends "data": null.
        raw_models: list[dict] = data.get("data") or []
        model_ids: list[str] = sorted(m["id"] for m in raw_models if _is_chat_model(m, provider))

    # OpenAI chat models all support tools; other servers depend on how
    # each model was deployed, so probe them.
    if provider != "openai" and model_ids:
        supported = await _tool_support(base_url, api_key, model_ids)
        model_ids = [mid for mid in model_ids if supported[mid]]

    logger.info("Provider %s at %s: %d chat models found", provider, base_url, len(model_ids))
    return [f"{provider}/{mid}" for mid in model_ids]
