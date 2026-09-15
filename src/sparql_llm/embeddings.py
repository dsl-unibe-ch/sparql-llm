"""Pluggable embedding backends: local (fastembed/ONNX) or remote (OpenAI-compatible API).

The rest of the codebase imports ``embedding_model`` from ``index_resources.py``
and calls two things on it:

* ``embedding_model.embed(texts)`` → iterable of vectors (list[float] or ndarray)
* ``embedding_model.embedding_size`` → int  (the vector dimension)

Both backends expose exactly this interface so the swap is transparent.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Iterable

from sparql_llm.utils import logger

if TYPE_CHECKING:
    import numpy as np


# ---------------------------------------------------------------------------
# Remote backend — calls an OpenAI-compatible /v1/embeddings endpoint
# ---------------------------------------------------------------------------

class OpenAIEmbedding:
    """Thin wrapper around the OpenAI embeddings API (works with GPUStack, vLLM, etc.).

    Designed as a drop-in replacement for ``fastembed.TextEmbedding``:
    * ``.embed(texts)`` yields one ``list[float]`` per input text.
    * ``.embedding_size`` returns the configured vector dimension.
    """

    def __init__(
        self,
        model: str,
        *,
        dimensions: int = 1024,
        api_key: str | None = None,
        base_url: str | None = None,
        batch_size: int = 64,
    ) -> None:
        self.model = model
        self._dimensions = dimensions
        self._batch_size = batch_size

        # Fall back to the same env vars the LLM client uses
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._base_url = base_url or os.getenv("OPENAI_BASE_URL", "")

        # Lazily import openai so the dependency is only required when this
        # backend is actually selected.
        from openai import OpenAI  # noqa: E402

        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        logger.info(
            "OpenAI embedding backend: model=%s, dim=%d, base_url=%s",
            self.model,
            self._dimensions,
            self._base_url,
        )

    @property
    def embedding_size(self) -> int:
        return self._dimensions

    def embed(
        self,
        texts: Iterable[str],
        **_kwargs,
    ) -> Iterable[list[float]]:
        """Embed *texts* via the remote API, yielding one vector per input.

        Large inputs are chunked into batches of ``self._batch_size`` to stay
        within typical API limits while keeping the generator-based interface
        the callers expect.
        """
        texts_list: list[str] = list(texts) if not isinstance(texts, list) else texts
        for batch_start in range(0, len(texts_list), self._batch_size):
            batch = texts_list[batch_start : batch_start + self._batch_size]
            response = self._client.embeddings.create(model=self.model, input=batch)
            # The API returns data sorted by index; yield in order.
            for item in sorted(response.data, key=lambda d: d.index):
                yield item.embedding


# ---------------------------------------------------------------------------
# Local backend — wraps fastembed (loads ONNX model into RAM)
# ---------------------------------------------------------------------------

class LocalEmbedding:
    """Wrapper around ``fastembed.TextEmbedding`` that matches the interface above.

    Only imports / instantiates fastembed when constructed, so the ONNX model
    is never loaded unless this backend is explicitly selected.
    """

    def __init__(self, model: str, **kwargs) -> None:
        from fastembed import TextEmbedding  # noqa: E402

        logger.info("Local fastembed backend: model=%s", model)
        self._inner = TextEmbedding(model, **kwargs)

    @property
    def embedding_size(self) -> int:
        return self._inner.embedding_size  # type: ignore[return-value]

    def embed(self, texts: Iterable[str], **kwargs) -> Iterable[np.ndarray]:
        """Delegate to the fastembed model."""
        return self._inner.embed(texts, **kwargs)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_embedding_model(
    backend: str,
    model: str,
    *,
    dimensions: int = 1024,
    api_key: str | None = None,
    base_url: str | None = None,
) -> OpenAIEmbedding | LocalEmbedding:
    """Instantiate the configured embedding backend.

    Args:
        backend: ``"openai"`` for the remote API, ``"local"`` for fastembed.
        model: The model identifier (e.g. ``"qwen3-embedding-0.6b"`` or
               ``"intfloat/multilingual-e5-large"``).
        dimensions: Vector size (only used by the openai backend).
        api_key: Override for ``OPENAI_API_KEY`` (openai backend only).
        base_url: Override for ``OPENAI_BASE_URL`` (openai backend only).
    """
    if backend == "openai":
        return OpenAIEmbedding(
            model,
            dimensions=dimensions,
            api_key=api_key,
            base_url=base_url,
        )
    if backend == "local":
        return LocalEmbedding(model)
    raise ValueError(
        f"Unknown embedding backend {backend!r}. Choose 'openai' or 'local'."
    )
