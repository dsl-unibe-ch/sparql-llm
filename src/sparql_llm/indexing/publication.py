"""One durable record per publish, and file moves that can be replayed.

``<data_dir>/<id>/publish.json`` is the commit point of a rebuild: before it is written
nothing live has changed, after it the publish rolls forward to the end, at the next
start if need be. The record lists the files itself, because ``uploaded/project.json``
may already have moved when it is replayed.

``generation.json`` names the collection the live files were published with, so a lost
alias can be linked again and a mismatch reported.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sparql_llm.project import BUILD_DIR, GENERATED_FILES, UPLOADED_DIR, Project

logger = logging.getLogger("sparql_llm")

RECORD_FILE = "publish.json"
GENERATION_FILE = "generation.json"
#: finish_publication cannot run without these.
_RECORD_KEYS = {"alias", "collection", "files", "job", "job_message"}


class PublicationError(Exception):
    """A publish record cannot be read. Plain message."""


def write_durable_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace ``path`` so that it holds either the old content or all of the new, even after a power loss."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}-{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    # On Windows the replace is refused while a reader has the file open.
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)
    _fsync_folder(path.parent)


def _fsync_folder(folder: Path) -> None:
    """Make a rename durable. Best effort: Windows refuses to open a folder."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def files_for(project: Project) -> dict[str, list[str]]:
    """What publishing this candidate moves: built files, uploaded files, and generated files it no longer configures."""
    remove = []
    if project.entities is None:
        remove.append("entity_names.json")
    if project.curator_examples is None:
        remove.append("examples-synced.md")
    uploaded = project.uploaded_dir
    return {
        "generated": [name for name in GENERATED_FILES if (project.build_dir / name).is_file()],
        "uploaded": sorted(p.name for p in uploaded.iterdir() if p.is_file()) if uploaded.is_dir() else [],
        "remove": remove,
    }


def apply_files(project_dir: Path, files: dict[str, list[str]]) -> None:
    """Make a built candidate live. Each step is skipped when already done, so this can run again after any failure.

    A replaced uploaded file is kept as ``<name>.prev`` for Undo; a replay never overwrites
    that copy with the new file, because the upload is gone by then.
    """
    build = project_dir / BUILD_DIR
    uploaded = project_dir / UPLOADED_DIR
    for name in files["generated"]:
        if (build / name).is_file():
            os.replace(build / name, project_dir / name)
    for name in files["remove"]:
        (project_dir / name).unlink(missing_ok=True)
    for name in files["uploaded"]:
        upload = uploaded / name
        if not upload.is_file():
            continue
        target = project_dir / name
        if target.exists():
            os.replace(target, project_dir / f"{name}.prev")
        os.replace(upload, target)
    for folder in (uploaded, build):
        try:
            if folder.exists():
                shutil.rmtree(folder)
        except OSError as exc:
            logger.warning("Could not remove %s after publishing: %s", folder, exc)


def record_path(project_dir: Path) -> Path:
    return project_dir / RECORD_FILE


def read_record(project_dir: Path) -> dict[str, Any] | None:
    """The unfinished publish of this dataset, or None. Raises PublicationError when it cannot be used."""
    path = record_path(project_dir)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PublicationError(f"The publish record {path} cannot be read: {exc}") from exc
    if not isinstance(record, dict) or not _RECORD_KEYS <= record.keys():
        raise PublicationError(f"The publish record {path} is incomplete.")
    return record


def write_record(project_dir: Path, record: dict[str, Any]) -> None:
    write_durable_json(record_path(project_dir), record)


def clear_record(project_dir: Path) -> None:
    record_path(project_dir).unlink(missing_ok=True)
    _fsync_folder(project_dir)


def read_generation(project_dir: Path) -> dict[str, Any] | None:
    """Which collection the live files were published with; None when absent or unreadable."""
    try:
        generation = json.loads((project_dir / GENERATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return generation if isinstance(generation, dict) else None


def write_generation(project_dir: Path, collection: str) -> None:
    committed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_durable_json(project_dir / GENERATION_FILE, {"collection": collection, "committed_at": committed_at})
