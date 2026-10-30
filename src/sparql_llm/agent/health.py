"""Liveness and readiness checks for the container healthcheck.

Nothing here contacts a SPARQL endpoint, an LLM or the embedding service, and nothing
runs the drift check: a probe must stay cheap and must not depend on remote services.
"""

import sqlite3
import uuid
from pathlib import Path

from sparql_llm.config import settings


def _reason(exc: Exception) -> str:
    """A one-line reason without paths or secrets: the error class and its first line, paths dropped."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    words = [w for w in text.split() if "/" not in w and "\\" not in w]
    return f"{type(exc).__name__}: {' '.join(words)}".rstrip(": ")[:200]


def _check_data_dir() -> None:
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    probe = data_dir / f".readyz-probe-{uuid.uuid4().hex}"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)


def _check_users_db() -> None:
    if not settings.auth_enabled:
        return
    uri = Path(settings.auth_db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("select 1")
    finally:
        conn.close()


def _check_datasets() -> None:
    from sparql_llm.datasets import list_dataset_ids

    list_dataset_ids()


def _check_vectordb() -> None:
    from sparql_llm.indexing import index_resources

    index_resources.qdrant_client.get_collections()


def _check_embedding_model() -> None:
    from sparql_llm.indexing import index_resources

    if index_resources.embedding_model is None:
        raise RuntimeError("embedding model is not configured")


CHECKS = {
    "data_dir": _check_data_dir,
    "users_db": _check_users_db,
    "datasets": _check_datasets,
    "vectordb": _check_vectordb,
    "embedding_model": _check_embedding_model,
}


def readiness_warnings() -> list[str]:
    """Problems that do not stop the app, as a count: this answers without a login, so no dataset is named."""
    try:
        from sparql_llm.indexing.rebuild import publication_problems

        count = len(publication_problems())
    except Exception:
        return []
    if not count:
        return []
    return ["1 dataset needs attention" if count == 1 else f"{count} datasets need attention"]


def readiness_checks() -> dict[str, str | None]:
    """Each check's result: ``None`` when ok, else a one-line reason."""
    results: dict[str, str | None] = {}
    for name, check in CHECKS.items():
        try:
            check()
            results[name] = None
        except Exception as exc:
            results[name] = _reason(exc)
    return results
