"""A guard around Qdrant's local ``meta.json``.

In path mode Qdrant lists every collection and alias in ``<vectordb>/meta.json`` and
rewrites it in place on each change, so a kill in that window leaves it truncated and
every index disappears, or the client cannot even open. A copy taken at known-good
moments, restored before the client opens, closes that window.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from sparql_llm.indexing.publication import fsync_file, fsync_folder

logger = logging.getLogger("sparql_llm")

META_FILE = "meta.json"
BACKUP_FILE = "meta.json.bak"
DAMAGED_FILE = "meta.json.damaged"


def _is_valid(path: Path) -> bool:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(meta, dict) and isinstance(meta.get("collections"), dict) and isinstance(meta.get("aliases"), dict)


def _copy(source: Path, target: Path) -> None:
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
    shutil.copyfile(source, tmp)
    fsync_file(tmp)
    os.replace(tmp, target)
    fsync_folder(target.parent)


def snapshot_meta(vectordb_path: Path) -> bool:
    """Copy a valid ``meta.json`` to the backup; a damaged one never replaces a good backup."""
    meta = Path(vectordb_path) / META_FILE
    if not _is_valid(meta):
        return False
    _copy(meta, Path(vectordb_path) / BACKUP_FILE)
    return True


def repair_meta(vectordb_path: Path) -> str:
    """Make ``meta.json`` safe to open: "ok" when it was, "restored" from the backup, or "moved-aside".

    With no usable backup the damaged file is kept beside it and Qdrant starts with no
    indexes, which an admin can rebuild: better than an app that cannot start.
    """
    folder = Path(vectordb_path)
    meta = folder / META_FILE
    backup = folder / BACKUP_FILE
    if not folder.is_dir() or _is_valid(meta):
        return "ok"
    if _is_valid(backup):
        logger.warning("Qdrant's meta.json in %s is damaged or missing: restoring it from the backup.", folder)
        _copy(backup, meta)
        return "restored"
    if not meta.exists():
        return "ok"
    logger.error("Qdrant's meta.json in %s is damaged and has no backup: moving it aside, all indexes need a rebuild.", folder)
    os.replace(meta, folder / DAMAGED_FILE)
    return "moved-aside"
