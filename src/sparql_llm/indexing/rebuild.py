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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qdrant_client import models

from sparql_llm.config import settings
from sparql_llm.indexing.drift import Fingerprint, save_fingerprint, take_fingerprint
from sparql_llm.indexing.entity_names import build_entity_names
from sparql_llm.indexing.index_resources import endpoints_metadata, init_vectordb, qdrant_client
from sparql_llm.indexing.preflight import run_preflight
from sparql_llm.indexing.sync_examples import format_report, sync_curator_examples
from sparql_llm.indexing.void_gen import VoidError, generate_void
from sparql_llm.migration import migrate_configured
from sparql_llm.project import Project, get_projects, project_problems, reload_projects, restore_projects
from sparql_llm.utils import logger



def job_file() -> Path:
    """Job status, shared across workers. Written atomically; safe to read at any moment."""
    return Path(settings.data_dir) / "reindex_job.json"


def lock_file() -> Path:
    """Held for the duration of a rebuild so two admins clicking at once cannot both run one."""
    return Path(settings.data_dir) / "reindex.lock"


#: A rebuild that has not reported progress for this long is presumed dead (worker killed,
#: container restarted) and its lock may be broken.
STALE_JOB_SECONDS = 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write via a temp file + replace so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    # On Windows the replace is refused while a reader (the status poll) has the file open.
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def read_job() -> dict[str, Any]:
    """Current (or last) rebuild job status. Always returns a usable dict."""
    if not job_file().exists():
        return {"state": "idle", "message": "No rebuild has been run yet."}
    try:
        return json.loads(job_file().read_text(encoding="utf-8"))
    except Exception:
        return {"state": "unknown", "message": "Job status file could not be read."}


def _set_job(state: str, message: str, **extra: Any) -> None:
    payload = {"state": state, "message": message, "updated_at": _now(), **extra}
    _write_json_atomic(job_file(), payload)
    logger.info("Reindex job: %s — %s", state, message)


def _acquire_lock() -> bool:
    """Take the rebuild lock. Breaks a lock left behind by a dead job."""
    lock_file().parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_file(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {_now()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - lock_file().stat().st_mtime
        except OSError:
            return False
        if age > STALE_JOB_SECONDS:
            logger.warning("Breaking a stale reindex lock (%.0fs old)", age)
            _release_lock()
            return _acquire_lock()
        return False


def _release_lock() -> None:
    try:
        lock_file().unlink()
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


class RebuildStopped(Exception):
    """A project failed a check: the rebuild stops before the alias swap. Plain message for the admin page."""


def _project_headline(project: Project, report: dict[str, Any], fingerprint: Fingerprint) -> str:
    """'Norssit: Person 10,137 · Group 2,368 · 330 → 40 classes · 6/6 examples OK.'"""
    bits = []
    if project.entities:
        for type_name, entity_type in project.entities.types.items():
            if entity_type.class_iri in fingerprint.classes:
                bits.append(f"{type_name} {fingerprint.classes[entity_type.class_iri]:,}")
    void = report.get("void") or {}
    if void:
        bits.append(f"{void['classes_total']} → {void['classes_kept']} classes")
    examples = report.get("examples") or {}
    if examples.get("total"):
        bits.append(f"{examples['ok']}/{examples['total']} examples OK")
    return f"{project.label}: {' · '.join(bits)}."


def rebuild_index_with_alias() -> dict[str, Any]:
    """Run the onboarding pipeline, build a new index, then swap the alias to it.

    Blocking; call from a worker thread. Per project: preflight checks, curator example
    sync, VoID generation, entity names; then once: the validator schema, the index, the
    alias swap, the fingerprints and a reload of the project files. Any error raises
    RebuildStopped before the swap, so the live index keeps serving.

    An alias cannot share a name with a real collection, so the first run has to retire the
    original same-named collection. That is the one moment of downtime, and it lasts a
    single Qdrant call rather than the length of a rebuild.
    """
    migrate_configured()
    # Run on the project files as uploaded; if the rebuild stops, serve the previous ones again.
    previous, previous_problems = get_projects(), project_problems()
    projects = reload_projects()
    try:
        return _rebuild(projects)
    except Exception:
        restore_projects(previous, previous_problems)
        raise


def _rebuild(projects: list[Project]) -> dict[str, Any]:
    problems = project_problems()
    if problems:
        raise RebuildStopped(" ".join(problems.values()))
    if not projects:
        raise RebuildStopped("No project is configured for this instance (projects in the install settings).")
    alias = _live_alias()
    version = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = f"{alias}-{version}"
    reports: dict[str, dict[str, Any]] = {p.id: {} for p in projects}

    # 1. Check every project first: a misconfigured one must not produce a running bot
    #    that answers from the wrong data or schema.
    for project in projects:
        _set_job("running", f"{project.label}: checking the configuration…", collection=target)
        checks = run_preflight(project)
        reports[project.id]["checks"] = [asdict(c) for c in checks]
        examples_check = next((c for c in checks if c.step == "examples"), None)
        reports[project.id]["examples"] = examples_check.details if examples_check else {}
        errors = [c.message for c in checks if c.status == "error"]
        if errors:
            raise RebuildStopped(" ".join(errors))

    # 2. Pull the curators' examples, so a rebuild always indexes what they have published.
    #    Every query is executed before it is written; the ones that fail are left out and
    #    reported. This never raises — an unreachable repo leaves the previous corpus in place.
    _set_job("running", "Fetching and testing the curators' example queries…", collection=target)
    examples_reports = {p.id: sync_curator_examples(p) for p in projects}
    synced = {pid: r for pid, r in examples_reports.items() if not r.get("skipped")}
    examples_report = {
        "accepted": sum(r["accepted"] for r in synced.values()),
        "rejected": [rej for r in synced.values() for rej in r["rejected"]],
        "error": "; ".join(r["error"] for r in synced.values() if r["error"]),
    }
    examples_summary = (
        "; ".join(format_report(r) for r in synced.values()) if synced else "No curator example sync configured"
    )
    logger.info("Example sync: %s", examples_summary)

    # 3. Describe each endpoint's schema. An unreadable result stops the rebuild.
    for project in projects:
        _set_job("running", f"{project.label}: describing the schema…", collection=target)
        try:
            reports[project.id]["void"] = generate_void(project)
        except VoidError as exc:
            raise RebuildStopped(str(exc)) from exc
        except Exception as exc:
            raise RebuildStopped(
                f"{project.label}: the schema could not be generated from {project.endpoint_url} ({exc})."
            ) from exc

    # 4. Refresh the query validator's schema and prefixes (each project's prefixes.json)
    #    from the VoID just written, so it never validates against a stale copy.
    try:
        endpoints_metadata.refresh()
    except Exception as exc:
        raise RebuildStopped(f"The query validator's schema could not be refreshed ({exc}).") from exc

    # 5. The name → URI lists the entity resolver matches against. Never raises: on failure
    #    the previous list stays in use and the result message says so.
    _set_job("running", f"{examples_summary}. Collecting entity names…", collection=target)
    entity_reports = {}
    for project in projects:
        entity_reports[project.label] = build_entity_names(project)
        reports[project.id]["entities"] = entity_reports[project.label]["entities"]
    entities_report = {
        "entities": sum(r["entities"] for r in entity_reports.values()),
        "error": "; ".join(f"{label}: {r['error']}" for label, r in entity_reports.items() if r["error"]),
    }
    entities_warning = (
        f" ⚠ Entity names not rebuilt ({entities_report['error']}); the previous list is still in use."
        if entities_report["error"]
        else ""
    )

    _set_job("running", f"{examples_summary}. Building new index '{target}'…", collection=target)

    # Fingerprint BEFORE building so the recorded snapshot can never claim to be newer
    # than the data the index was actually built from.
    fingerprints = {p.id: take_fingerprint(p) for p in projects}
    triples = sum(fp.triples for fp in fingerprints.values())
    class_count = sum(len(fp.classes) for fp in fingerprints.values())

    doc_count = init_vectordb(collection_name=target)
    if not doc_count:
        raise RebuildStopped("Indexing produced 0 documents — refusing to swap the alias.")

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

    for project in projects:
        save_fingerprint(fingerprints[project.id], project)

    # Only now is the old index unreachable, so dropping it is safe.
    removed = []
    for name in _collections():
        if name.startswith(f"{alias}-") and name != target:
            try:
                qdrant_client.delete_collection(name)
                removed.append(name)
            except Exception as exc:
                logger.warning("Could not remove the superseded collection %s: %s", name, exc)

    # The project files may have been uploaded since the last start: serve the new ones.
    reload_projects()

    result = {
        "collection": target,
        "documents": doc_count,
        "removed": removed,
        "previous": previous,
        "triples": triples,
        "classes": class_count,
        "examples_accepted": examples_report["accepted"],
        "entities": entities_report["entities"],
        "examples_rejected": examples_report["rejected"],
        "projects": reports,
    }
    # A rejected example is the one outcome an admin must not miss: the rebuild
    # succeeded, but something the curators wrote is not in the assistant. Put it in the
    # headline message rather than only in the structured payload.
    warning = ""
    if examples_report["rejected"] or examples_report["error"]:
        warning = f" ⚠ {examples_summary}"
    example_warnings = [
        c["message"] for r in reports.values() for c in r.get("checks", []) if c["status"] == "warning"
    ]
    if example_warnings:
        warning += " ⚠ " + " ".join(example_warnings)
    headlines = " ".join(_project_headline(p, reports[p.id], fingerprints[p.id]) for p in projects)
    _set_job(
        "done",
        f"{headlines} Index rebuilt: {doc_count} documents covering {class_count} classes "
        f"({triples:,} triples), {entities_report['entities']:,} entity names."
        f"{warning}{entities_warning}",
        **result,
    )
    return result


def run_rebuild_job() -> None:
    """The body of a background rebuild: run it, record how it ended, release the lock."""
    try:
        rebuild_index_with_alias()
    except RebuildStopped as exc:
        logger.warning("Index rebuild stopped: %s", exc)
        _set_job("failed", f"Rebuild stopped: {exc} The previous index is still serving.", error=str(exc))
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


def start_rebuild() -> dict[str, Any]:
    """Kick off a rebuild in the background. Returns immediately.

    Refuses if one is already running — the caller should surface that rather than queueing,
    since a second rebuild would only redo the first one's work.
    """
    if not _acquire_lock():
        job = read_job()
        return {"started": False, "reason": "already-running", "job": job}

    _set_job("running", "Starting rebuild…")
    threading.Thread(target=run_rebuild_job, name="reindex", daemon=True).start()
    return {"started": True, "job": read_job()}
