"""Rebuild the retrieval index without taking the assistant down.

The straightforward rebuild — delete the collection, recreate it, refill it — leaves the
assistant with *no* index for the several minutes the embeddings take. Queries do not
degrade during that window, they fail; and a rebuild that crashes half way leaves nothing
to serve at all.

Instead this builds into a fresh versioned collection and, only once it has finished,
points a Qdrant alias at it. Readers address the alias, so the switch is atomic and a
failed build leaves the previous index serving untouched.

Publishing has one commit point: writing ``<id>/publish.json`` (``indexing.publication``).
Before it nothing live has changed. After it the publish only ever rolls forward: alias,
files, generation, cleanup, each step safe to repeat, finished by Rebuild or at the next
start. So a kill at any moment leaves one coherent version once the app is up again.

Each dataset has its own index (alias ``<id>__docs``), job file and rebuild: rebuilding
one never touches another's collection, files or job. One rebuild runs at a time per
instance (a global lock), since they share the embedding model.

Job state lives in a file rather than process memory: the app can run under several
uvicorn workers, and a status request may well land on a different worker from the one
that started the job.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from qdrant_client import models

from sparql_llm.config import settings
from sparql_llm.indexing.drift import Fingerprint, save_fingerprint, take_fingerprint
from sparql_llm.indexing.entity_names import build_entity_names
from sparql_llm.indexing.index_resources import endpoints_metadata, init_vectordb, qdrant_client
from sparql_llm.indexing.preflight import run_preflight
from sparql_llm.indexing.publication import (
    PublicationError,
    apply_files,
    clear_record,
    files_for,
    read_generation,
    read_record,
    record_path,
    write_generation,
    write_record,
)
from sparql_llm.indexing.qdrant_meta import snapshot_meta
from sparql_llm.indexing.sync_examples import format_report, sync_curator_examples
from sparql_llm.indexing.void_gen import VoidError, generate_void
from sparql_llm.migration import migrate_configured
from sparql_llm.project import BUILD_DIR, Project, ProjectError, discard_build, load_project, reload_projects
from sparql_llm.utils import EndpointsMetadataManager, logger


#: Set on shutdown: a running rebuild stops at its next checkpoint.
_stop = threading.Event()
#: The thread of the last rebuild started.
_worker: threading.Thread | None = None


def job_file(project_id: str) -> Path:
    """A dataset's rebuild status, shared across workers. Written atomically; safe to read at any moment."""
    return Path(settings.data_dir) / project_id / "reindex_job.json"


def lock_file() -> Path:
    """Held for the duration of any rebuild: one at a time per instance, whichever dataset."""
    return Path(settings.data_dir) / "reindex.lock"


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


def read_job(project_id: str) -> dict[str, Any]:
    """A dataset's current (or last) rebuild job status. Always returns a usable dict."""
    path = job_file(project_id)
    if not path.exists():
        return {"state": "idle", "message": "No rebuild has been run yet."}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "unknown", "message": "Job status file could not be read."}


def _set_job(project_id: str, state: str, message: str, **extra: Any) -> None:
    payload = {"state": state, "message": message, "updated_at": _now(), "dataset": project_id, **extra}
    _write_json_atomic(job_file(project_id), payload)
    logger.info("Reindex job %s: %s — %s", project_id, state, message)


def _acquire_lock(project_id: str) -> str | None:
    """Take the rebuild lock for this dataset: its owner token, or None while another job holds it.

    A held lock is never broken here, however old: its job may still be running. A lock
    left by a killed process is cleared at startup (:func:`recover_after_restart`).
    """
    lock_file().parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    try:
        fd = os.open(lock_file(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {_now()} {project_id} {token}".encode())
        os.close(fd)
        return token
    except FileExistsError:
        return None


def running_dataset() -> str:
    """The dataset whose rebuild holds the lock ("" when none, or an old-style lock)."""
    try:
        parts = lock_file().read_text(encoding="utf-8").split()
    except OSError:
        return ""
    return parts[2] if len(parts) > 2 else ""


def _release_lock(token: str) -> None:
    """Release the lock if ``token`` still owns it: a job never removes another job's lock."""
    try:
        parts = lock_file().read_text(encoding="utf-8").split()
    except OSError:
        return
    if len(parts) > 3 and parts[3] == token:
        lock_file().unlink(missing_ok=True)


def _project_dir(project_id: str) -> Path:
    return Path(settings.data_dir) / project_id


def _snapshot_vector_store() -> None:
    """Back up Qdrant's list of collections and aliases; only local mode has that file."""
    if not settings.vectordb_url.startswith(("http", "https")):
        snapshot_meta(Path(settings.vectordb_url))


def publication_pending(project_id: str) -> bool:
    """Whether a publish of this dataset has passed its commit point and not finished. An unreadable record counts."""
    return record_path(_project_dir(project_id)).exists()


def _unreadable_record(project_id: str) -> str:
    return f"The record of an unfinished publish could not be read. Ask a developer to look at {project_id}/publish.json."


def recover_after_restart() -> None:
    """Finish or clear what a killed process left. Called at startup, before anything is served.

    The app runs one worker and rebuilds in its threads, so none can be running yet: a
    lock, a "running" job or a next/ folder on disk belongs to a process that is gone.
    One dataset that cannot recover never stops the others, nor the app.
    """
    from sparql_llm.datasets import list_dataset_ids

    _stop.clear()
    if lock_file().exists():
        logger.warning("Clearing the rebuild lock left by a stopped process: %s", lock_file().read_text(encoding="utf-8"))
        lock_file().unlink(missing_ok=True)
    _finish_removals()
    for project_id in list_dataset_ids():
        try:
            _recover_dataset(project_id)
        except Exception:
            logger.exception("Could not recover dataset %s at startup", project_id)
    try:
        _snapshot_vector_store()
    except Exception:
        logger.exception("Could not back up the vector store's meta.json")


def _finish_removals() -> None:
    """Finish the dataset removals a stop cut short. A failure keeps the intent for the next start."""
    from sparql_llm.datasets import finish_removal, pending_removals

    finished = False
    for dataset_id in pending_removals():
        try:
            drop_index(dataset_id)
            finish_removal(dataset_id)
            finished = True
        except Exception:
            logger.exception("Could not finish removing dataset %s at startup", dataset_id)
    if finished:
        reload_projects()
        endpoints_metadata.reload()


def _recover_dataset(project_id: str) -> None:
    try:
        record = read_record(_project_dir(project_id))
    except PublicationError as exc:
        logger.error("%s", exc)
        _set_job(project_id, "failed", _unreadable_record(project_id), error="unreadable-record")
        return
    if record is not None:
        try:
            finish_publication(project_id, record, resumed=True)
        except RebuildPartlyPublished as exc:
            logger.exception("Could not finish the publish of %s at startup", project_id)
            _set_job(project_id, "failed", str(exc), error=str(exc))
        return
    if read_job(project_id).get("state") in ("running", "swapping"):
        _set_job(
            project_id,
            "failed",
            "The rebuild was interrupted by a restart. Nothing changed. Run Check, then Rebuild again.",
            error="interrupted",
        )
    shutil.rmtree(_project_dir(project_id) / BUILD_DIR, ignore_errors=True)
    _tidy_index(project_id)


def _delete_collections(names: list[str]) -> list[str]:
    """Delete what can be deleted, logging the rest: leftovers do no harm and the next publish retries them."""
    removed = []
    for name in names:
        try:
            qdrant_client.delete_collection(name)
            removed.append(name)
        except Exception as exc:
            logger.warning("Could not remove the superseded collection %s: %s", name, exc)
    return removed


def _tidy_index(project_id: str) -> None:
    """Drop the collections nothing serves, and link a lost alias again."""
    project = SimpleNamespace(id=project_id)
    alias = alias_for(project)  # type: ignore[arg-type]
    keep = _alias_target(alias)
    existing = _collections()
    if keep is None:
        generation = read_generation(_project_dir(project_id)) or {}
        if generation.get("collection") in existing:
            keep = generation["collection"]
            logger.warning("The alias %s was lost: linking it to %s again.", alias, keep)
            _publish_alias(alias, keep)
        elif (_project_dir(project_id) / "fingerprint.json").exists():
            return  # Published, and nothing says with which index: the health check reports it.
    prefix = version_prefix(project)  # type: ignore[arg-type]
    _delete_collections([name for name in existing if name.startswith(prefix) and name != keep])


def alias_for(project: Project) -> str:
    """The name readers query for this dataset's index; its own rebuild swaps it."""
    return f"{project.id}__docs"


def versioned_collection(project: Project, version: str) -> str:
    return f"{version_prefix(project)}{version}"


def version_prefix(project: Project) -> str:
    """What every versioned collection of this dataset starts with, and no other's.

    Ids cannot contain "_", so "a__v" never prefixes a collection of "a-v2" and vice versa.
    """
    return f"{project.id}__v"


def _aliases() -> set[str]:
    return {a.alias_name for a in qdrant_client.get_aliases().aliases}


def collection_for(project: Project) -> str:
    """The collection to search for this dataset.

    Its own alias once a per-dataset rebuild made one. Until then the first dataset is
    still served by the index of the single-dataset install (``docs_collection_name``),
    so an upgrade needs no rebuild.
    """
    alias = alias_for(project)
    aliases = _aliases()
    if alias in aliases:
        return alias
    from sparql_llm.datasets import list_dataset_ids

    ids = list_dataset_ids()
    if ids and ids[0] == project.id:
        return settings.docs_collection_name
    return alias


def _collections() -> list[str]:
    return [c.name for c in qdrant_client.get_collections().collections]


def _alias_target(alias: str) -> str | None:
    """The collection ``alias`` points at, or None."""
    return next((a.collection_name for a in qdrant_client.get_aliases().aliases if a.alias_name == alias), None)


def _point_alias(alias: str, collection: str) -> None:
    """Repoint ``alias`` at ``collection`` in a single operation."""
    ops: list[Any] = [
        models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)),
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
        ),
    ]
    qdrant_client.update_collection_aliases(change_aliases_operations=ops)


def _publish_alias(alias: str, collection: str) -> None:
    """Point ``alias`` at ``collection``, creating it when this dataset has none yet."""
    try:
        _point_alias(alias, collection)
    except Exception:
        # No alias existed yet (nothing to delete) — create it outright.
        qdrant_client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
                )
            ]
        )


class RebuildStopped(Exception):
    """A project failed a check: the rebuild stops before the alias swap. Plain message for the admin page."""


class RebuildCancelled(Exception):
    """A shutdown stopped the rebuild before its commit point."""


class RebuildPartlyPublished(Exception):
    """A publish passed its commit point and could not finish: Rebuild finishes it. Plain message for the admin page."""


def _check_stop() -> None:
    if _stop.is_set():
        raise RebuildCancelled


def stop_rebuilds(timeout: float = 15.0) -> bool:
    """Ask a running rebuild to stop and wait for it: True when none is alive afterwards.

    A courtesy that lets a stop finish inside the container's grace period. A publish past
    its commit point is not stopped, and a thread that does not answer dies with the
    process: startup recovery is what guarantees a coherent state.
    """
    worker = _worker
    if worker is None or not worker.is_alive():
        return True
    _stop.set()
    worker.join(timeout)
    return not worker.is_alive()


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


def load_candidate(project_id: str) -> Project:
    """The dataset as Check and Rebuild see it: uploaded files over live ones."""
    from sparql_llm.datasets import list_dataset_ids

    if project_id not in list_dataset_ids():
        raise RebuildStopped(f"There is no dataset '{project_id}'.")
    try:
        project = load_project(Path(settings.data_dir), project_id, candidate=True)
    except ProjectError as exc:
        raise RebuildStopped(str(exc)) from exc
    from sparql_llm.project_files import endpoint_owner

    # Checked here too, not only on upload: Undo and older staged files reach a rebuild as well.
    owner = endpoint_owner(project_id, project.endpoint_url)
    if owner:
        raise RebuildStopped(
            f"{project.label}: endpoint_url: is already used by dataset '{owner}'. Each dataset needs its own endpoint."
        )
    return project


def rebuild_index_with_alias(project_id: str) -> dict[str, Any]:
    """Run the onboarding pipeline for one dataset, build its new index, then swap its alias.

    Blocking; call from a worker thread. It runs on the candidate project (uploaded files
    over live ones) and writes everything it generates to its next/ folder: preflight
    checks, curator example sync, VoID generation, validator schema, entity names, the
    index. Only then does it commit (see the module docstring) and move the alias, promote
    next/ and the uploaded files to live and reload the projects. Any error before the
    commit leaves the live index and every live file as they were. A failure after it
    raises RebuildPartlyPublished and keeps the record, next/ and uploaded/ for Rebuild to
    finish. No other dataset is read or written.
    """
    migrate_configured()
    project = load_candidate(project_id)
    discard_build(project)
    project.build_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _rebuild(project)
    except RebuildPartlyPublished:
        # The live index was built from next/ and uploaded/: keep them for the rerun.
        raise
    except BaseException:
        if not record_path(project.dir).exists():
            discard_build(project)
        raise


def _legacy_collections(existing: list[str]) -> list[str]:
    """The single-dataset install's versioned collections: "<docs_collection_name>-YYYYmmdd-HHMMSS"."""
    pattern = re.compile(re.escape(settings.docs_collection_name) + r"-\d{8}-\d{6}")
    return [name for name in existing if pattern.fullmatch(name)]


def _retire_legacy_index(project: Project) -> list[str]:
    """Once the first dataset has its own alias, drop the single-dataset install's index.

    Only the exact legacy names: the alias and its timestamped collections. A prefix match
    could catch another dataset whose id starts with the legacy name.
    """
    from sparql_llm.datasets import list_dataset_ids

    ids = list_dataset_ids()
    if not ids or ids[0] != project.id:
        return []
    removed = []
    legacy = settings.docs_collection_name
    try:
        if legacy in _aliases():
            qdrant_client.update_collection_aliases(
                change_aliases_operations=[models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=legacy))]
            )
        existing = _collections()
        for name in ([legacy] if legacy in existing else []) + _legacy_collections(existing):
            qdrant_client.delete_collection(name)
            removed.append(name)
    except Exception as exc:
        logger.warning("Could not remove the single-dataset index %s: %s", legacy, exc)
    return removed


def drop_index(project_id: str) -> list[str]:
    """Delete a dataset's search index: its alias and its versioned collections, nothing else.

    Call it before the dataset leaves the list: when it is the first dataset, the
    single-dataset install's index goes too, or the next dataset, then first, would fall
    back to it and answer from the removed dataset's documents.
    """
    project = SimpleNamespace(id=project_id)
    removed = _retire_legacy_index(project)  # type: ignore[arg-type]
    alias = alias_for(project)  # type: ignore[arg-type]
    if alias in _aliases():
        qdrant_client.update_collection_aliases(
            change_aliases_operations=[models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))]
        )
        removed.append(alias)
    for name in _collections():
        if name.startswith(version_prefix(project)):  # type: ignore[arg-type]
            qdrant_client.delete_collection(name)
            removed.append(name)
    return removed


def _rebuild(project: Project) -> dict[str, Any]:
    projects = [project]
    alias = alias_for(project)
    version = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = versioned_collection(project, version)
    reports: dict[str, dict[str, Any]] = {p.id: {} for p in projects}

    # 1. Check every project first: a misconfigured one must not produce a running bot
    #    that answers from the wrong data or schema.
    _check_stop()
    for project in projects:
        _set_job(project.id, "running", f"{project.label}: checking the configuration…", collection=target)
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
    _check_stop()
    _set_job(project.id, "running", "Fetching and testing the curators' example queries…", collection=target)
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
    _check_stop()
    for project in projects:
        _set_job(project.id, "running", f"{project.label}: describing the schema…", collection=target)
        try:
            reports[project.id]["void"] = generate_void(project)
        except VoidError as exc:
            raise RebuildStopped(str(exc)) from exc
        except Exception as exc:
            raise RebuildStopped(
                f"{project.label}: the schema could not be generated from {project.endpoint_url} ({exc})."
            ) from exc

    # 4. The query validator's schema and prefixes (each project's prefixes.json), from the
    #    VoID just written. Built aside; the live validator switches only after the swap.
    _check_stop()
    metadata = EndpointsMetadataManager(projects, auto_init=False)
    try:
        metadata.refresh()
    except Exception as exc:
        raise RebuildStopped(f"The query validator's schema could not be refreshed ({exc}).") from exc

    # 5. The name → URI lists the entity resolver matches against. Never raises: on failure
    #    the previous list stays in use and the result message says so.
    _check_stop()
    _set_job(project.id, "running", f"{examples_summary}. Collecting entity names…", collection=target)
    entity_reports = {}
    for project in projects:
        entity_reports[project.label] = build_entity_names(project)
        reports[project.id]["entities"] = entity_reports[project.label]["entities"]
    entities_report = {
        "entities": sum(r["entities"] for r in entity_reports.values()),
        "error": "; ".join(f"{label}: {r['error']}" for label, r in entity_reports.items() if r["error"]),
    }
    entities_warning = (
        f" ⚠ Entity names not rebuilt ({entities_report['error']})."
        if any(r.get("dropped") for r in entity_reports.values())
        else f" ⚠ Entity names not rebuilt ({entities_report['error']}); the previous list is still in use."
        if entities_report["error"]
        else ""
    )

    _check_stop()
    _set_job(project.id, "running", f"{examples_summary}. Building new index '{target}'…", collection=target)

    # Fingerprint BEFORE building so the recorded snapshot can never claim to be newer
    # than the data the index was actually built from.
    fingerprints = {p.id: take_fingerprint(p) for p in projects}
    triples = sum(fp.triples for fp in fingerprints.values())
    class_count = sum(len(fp.classes) for fp in fingerprints.values())

    # Into next/ with the other generated files: promoted with them, discarded with them.
    for project in projects:
        save_fingerprint(fingerprints[project.id], project)

    # Each project's docs use its own prefixes.json from next/, written by the refresh above.
    try:
        doc_count = init_vectordb(collection_name=target, projects=projects, checkpoint=_check_stop)
        if not doc_count:
            raise RebuildStopped("Indexing produced 0 documents — refusing to swap the alias.")
        _check_stop()  # The last one: past here a publish is never cancelled.
    except BaseException:
        _drop_unpublished(target, alias, project)
        raise

    _set_job(project.id, "swapping", f"Built {doc_count} documents. Switching traffic to '{target}'…",
             collection=target, documents=doc_count)

    previous: str | None = None

    for name in _collections():
        if name.startswith(version_prefix(project)) and name != target:
            previous = name  # candidate for cleanup after a successful swap

    result = {
        "collection": target,
        "documents": doc_count,
        "removed": [],
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
    message = (
        f"{headlines} Index rebuilt: {doc_count} documents covering {class_count} classes "
        f"({triples:,} triples), {entities_report['entities']:,} entity names."
        f"{warning}{entities_warning}"
    )

    try:
        # Lists the new collection, so a restored backup still knows it.
        _snapshot_vector_store()
        record = {
            "version": 1,
            "dataset": project.id,
            "alias": alias,
            "collection": target,
            "files": files_for(project),
            "job_message": message,
            "job": result,
            "created_at": _now(),
        }
        write_record(project.dir, record)  # The commit point: from here on the publish only rolls forward.
    except BaseException:
        _drop_unpublished(target, alias, project)
        raise
    return finish_publication(project.id, record)


def _drop_unpublished(target: str, alias: str, project: Project) -> None:
    """Never served: drop what was built, or each failed retry leaves a collection behind."""
    if record_path(project.dir).exists():
        return
    if target in _collections() and _alias_target(alias) != target:
        try:
            qdrant_client.delete_collection(target)
        except Exception as exc:
            logger.warning("Could not remove the unfinished collection %s: %s", target, exc)


def _abandon_publication(project_id: str) -> dict[str, Any]:
    """The target collection is gone and the alias never moved: only now may a publish be given up."""
    clear_record(_project_dir(project_id))
    shutil.rmtree(_project_dir(project_id) / BUILD_DIR, ignore_errors=True)
    _set_job(
        project_id,
        "failed",
        "The new index was lost before it went live. The previous index is still serving and your uploads are still waiting. "
        "Run Rebuild again.",
        error="abandoned",
    )
    return read_job(project_id)


def _unfinished_message(record: dict[str, Any], exc: Exception) -> str:
    """Says what serves now. An alias that did not move leaves the old index and the old files, which still agree."""
    collection = record["collection"]
    try:
        live = _alias_target(record["alias"]) == collection
    except Exception:
        live = True
    if not live:
        return (
            f"Publishing is unfinished: the switch to the new index '{collection}' did not complete ({exc}). "
            "Fix the cause, then press Rebuild to finish."
        )
    return (
        f"Publishing is unfinished: the new index '{collection}' is live, but its files could not all be made live ({exc}). "
        "Fix the cause, then press Rebuild to finish. The new index keeps serving meanwhile."
    )


def finish_publication(project_id: str, record: dict[str, Any], resumed: bool = False) -> dict[str, Any]:
    """Take a committed publish to its end. Every step is safe to repeat; any failure raises RebuildPartlyPublished."""
    project_dir = _project_dir(project_id)
    alias, collection = record["alias"], record["collection"]
    try:
        if collection not in _collections():
            if _alias_target(alias) != collection:
                return _abandon_publication(project_id)
            raise RuntimeError(f"the collection {collection} is gone")
        if _alias_target(alias) != collection:
            _publish_alias(alias, collection)
        apply_files(project_dir, record["files"])
        write_generation(project_dir, collection)
        reload_projects()
        endpoints_metadata.reload()

        # Only now is the old index unreachable and unneeded, so dropping it is safe.
        project = SimpleNamespace(id=project_id)
        removed = _retire_legacy_index(project)  # type: ignore[arg-type]
        prefix = version_prefix(project)  # type: ignore[arg-type]
        removed += _delete_collections([name for name in _collections() if name.startswith(prefix) and name != collection])
        _snapshot_vector_store()

        result = {**record["job"], "removed": removed}
        message = record["job_message"]
        if resumed:
            message += " Publishing was finished after a restart."
        _set_job(project_id, "done", message, **result)
        clear_record(project_dir)
    except Exception as exc:
        raise RebuildPartlyPublished(_unfinished_message(record, exc)) from exc
    return result


def run_rebuild_job(project_id: str, lock_token: str = "") -> None:
    """The body of a background rebuild: run it, record how it ended, release its lock (if it took one)."""
    try:
        # A stuck publish is finished, not built again: its new index and files are ready.
        record = read_record(_project_dir(project_id))
        if record is not None:
            finish_publication(project_id, record, resumed=True)
        else:
            rebuild_index_with_alias(project_id)
    except PublicationError as exc:
        logger.error("%s", exc)
        _set_job(project_id, "failed", _unreadable_record(project_id), error="unreadable-record")
    except RebuildCancelled:
        logger.info("Index rebuild of %s stopped by a shutdown", project_id)
        _set_job(
            project_id,
            "failed",
            "The rebuild was stopped by a shutdown. Nothing changed. Press Rebuild to run it again.",
            error="cancelled",
        )
    except RebuildStopped as exc:
        logger.warning("Index rebuild of %s stopped: %s", project_id, exc)
        _set_job(project_id, "failed", f"Rebuild stopped: {exc} The previous index is still serving.", error=str(exc))
    except RebuildPartlyPublished as exc:
        logger.exception("Index rebuild of %s did not finish publishing", project_id)
        _set_job(project_id, "failed", str(exc), error=str(exc), traceback=traceback.format_exc()[-2000:])
    except Exception as exc:
        logger.exception("Index rebuild of %s failed", project_id)
        _set_job(
            project_id,
            "failed",
            f"Rebuild failed: {exc}. The previous index is still serving.",
            error=str(exc),
            traceback=traceback.format_exc()[-2000:],
        )
    finally:
        _release_lock(lock_token)


def start_rebuild(project_id: str) -> dict[str, Any]:
    """Kick off a rebuild of one dataset in the background. Returns immediately.

    Refuses while any rebuild is running (one at a time per instance: they share the
    embedding model) and says which dataset's; the caller surfaces that rather than queueing.
    """
    lock_token = _acquire_lock(project_id)
    if not lock_token:
        running = running_dataset()
        return {
            "started": False,
            "reason": "already-running",
            "dataset": running,
            "job": read_job(running) if running else {},
        }

    _set_job(project_id, "running", "Starting rebuild…")
    global _worker
    _worker = threading.Thread(target=run_rebuild_job, args=(project_id, lock_token), name=f"reindex-{project_id}", daemon=True)
    _worker.start()
    return {"started": True, "job": read_job(project_id)}
