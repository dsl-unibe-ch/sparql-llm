"""Rebuild the retrieval index without taking the assistant down.

The straightforward rebuild — delete the collection, recreate it, refill it — leaves the
assistant with *no* index for the several minutes the embeddings take. Queries do not
degrade during that window, they fail; and a rebuild that crashes half way leaves nothing
to serve at all.

Instead this builds into a fresh versioned collection and, only once it has finished,
points a Qdrant alias at it. Readers address the alias, so the switch is atomic and a
failed build leaves the previous index serving untouched.

Job state lives in a file rather than process memory: the app can run under several
uvicorn workers, and a status request may well land on a different worker from the one
that started the job.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qdrant_client import models

from sparql_llm.config import settings
from sparql_llm.indexing.drift import save_fingerprint, take_fingerprint
from sparql_llm.indexing.index_resources import init_vectordb, qdrant_client
from sparql_llm.utils import logger

#: Job status, shared across workers. Written atomically; safe to read at any moment.
JOB_FILE = Path("data") / "reindex_job.json"

#: Held for the duration of a rebuild so two admins clicking at once cannot both run one.
LOCK_FILE = Path("data") / "reindex.lock"

#: A rebuild that has not reported progress for this long is presumed dead (worker killed,
#: container restarted) and its lock may be broken.
STALE_JOB_SECONDS = 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write via a temp file + replace so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_job() -> dict[str, Any]:
    """Current (or last) rebuild job status. Always returns a usable dict."""
    if not JOB_FILE.exists():
        return {"state": "idle", "message": "No rebuild has been run yet."}
    try:
        return json.loads(JOB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "unknown", "message": "Job status file could not be read."}


def _set_job(state: str, message: str, **extra: Any) -> None:
    payload = {"state": state, "message": message, "updated_at": _now(), **extra}
    _write_json_atomic(JOB_FILE, payload)
    logger.info("Reindex job: %s — %s", state, message)


def _acquire_lock() -> bool:
    """Take the rebuild lock. Breaks a lock left behind by a dead job."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {_now()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            return False
        if age > STALE_JOB_SECONDS:
            logger.warning("Breaking a stale reindex lock (%.0fs old)", age)
            _release_lock()
            return _acquire_lock()
        return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def _live_alias() -> str:
    """The name readers query. Retrieval and the MCP server both use this."""
    return settings.docs_collection_name


def _collections() -> list[str]:
    return [c.name for c in qdrant_client.get_collections().collections]


def _point_alias(alias: str, collection: str) -> None:
    """Repoint ``alias`` at ``collection`` in a single operation."""
    ops: list[Any] = [
        models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)),
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
        ),
    ]
    qdrant_client.update_collection_aliases(change_aliases_operations=ops)


def rebuild_index_with_alias() -> dict[str, Any]:
    """Build a new index, then swap the alias to it. Blocking; call from a worker thread.

    An alias cannot share a name with a real collection, so the first run has to retire the
    original same-named collection. That is the one moment of downtime, and it lasts a
    single Qdrant call rather than the length of a rebuild.
    """
    alias = _live_alias()
    version = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = f"{alias}-{version}"

    _set_job("running", f"Building new index '{target}'…", collection=target)

    # Fingerprint BEFORE building so the recorded snapshot can never claim to be newer
    # than the data the index was actually built from.
    fingerprint = take_fingerprint()

    doc_count = init_vectordb(collection_name=target)
    if not doc_count:
        raise RuntimeError("Indexing produced 0 documents — refusing to swap the alias.")

    _set_job("swapping", f"Built {doc_count} documents. Switching traffic to '{target}'…",
             collection=target, documents=doc_count)

    existing = _collections()
    previous: str | None = None

    if alias in existing:
        # First run under the alias scheme: a real collection still occupies the name.
        logger.info("Retiring the original collection '%s' so the alias can take its name", alias)
        qdrant_client.delete_collection(alias)
    else:
        for name in existing:
            if name.startswith(f"{alias}-") and name != target:
                previous = name  # candidate for cleanup after a successful swap

    try:
        _point_alias(alias, target)
    except Exception:
        # No alias existed yet (nothing to delete) — create it outright.
        qdrant_client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=target, alias_name=alias)
                )
            ]
        )

    save_fingerprint(fingerprint)

    # Only now is the old index unreachable, so dropping it is safe.
    removed = []
    for name in _collections():
        if name.startswith(f"{alias}-") and name != target:
            try:
                qdrant_client.delete_collection(name)
                removed.append(name)
            except Exception as exc:
                logger.warning("Could not remove the superseded collection %s: %s", name, exc)

    result = {
        "collection": target,
        "documents": doc_count,
        "removed": removed,
        "previous": previous,
        "triples": fingerprint.triples,
        "classes": len(fingerprint.classes),
    }
    _set_job(
        "done",
        f"Index rebuilt: {doc_count} documents covering {len(fingerprint.classes)} classes "
        f"({fingerprint.triples:,} triples).",
        **result,
    )
    return result


def start_rebuild() -> dict[str, Any]:
    """Kick off a rebuild in the background. Returns immediately.

    Refuses if one is already running — the caller should surface that rather than queueing,
    since a second rebuild would only redo the first one's work.
    """
    if not _acquire_lock():
        job = read_job()
        return {"started": False, "reason": "already-running", "job": job}

    def _run() -> None:
        try:
            rebuild_index_with_alias()
        except Exception as exc:
            logger.exception("Index rebuild failed")
            _set_job(
                "failed",
                f"Rebuild failed: {exc}. The previous index is still serving.",
                error=str(exc),
                traceback=traceback.format_exc()[-2000:],
            )
        finally:
            _release_lock()

    _set_job("running", "Starting rebuild…")
    threading.Thread(target=_run, name="reindex", daemon=True).start()
    return {"started": True, "job": read_job()}
