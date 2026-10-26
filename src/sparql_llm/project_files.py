"""A project's three curator files, uploaded and downloaded from the admin page.

An upload is validated before it replaces anything, and the previous version is kept as
``<file>.prev`` for a one-step undo. Saving does not reload the project: the next
Rebuild does, after checking the configuration against the endpoint.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sparql_llm.indexing.sync_examples import parse_examples
from sparql_llm.project import Project

FILE_NAMES = ("project.json", "dataset.md", "examples.md")
MAX_UPLOAD_BYTES = 1_000_000
UPLOAD_LOG = "uploads.json"


class ProjectFileError(Exception):
    """A request about a project file that cannot be served. Plain message."""


def _project_dir(project_id: str) -> Path:
    from sparql_llm.config import settings

    return Path(settings.data_dir) / project_id


def _served(project_id: str) -> bool:
    from sparql_llm.config import settings

    return project_id in settings.projects


def _field_reason(error: dict[str, Any]) -> str:
    """A pydantic error in words a curator can act on."""
    kind = error.get("type", "")
    if kind == "missing":
        return "is required"
    if kind == "literal_error":
        expected = str(error.get("ctx", {}).get("expected", "")).replace("'", "")
        return f"must be {expected}"
    if kind == "string_pattern_mismatch":
        return "must use lowercase letters, digits and dashes only, starting with a letter or digit"
    if kind.endswith("_type"):
        return error["msg"].lower().replace("input should be", "must be")
    return error["msg"]


def _project_errors(project_id: str, text: str) -> list[str]:
    try:
        content = json.loads(text)
    except json.JSONDecodeError as e:
        return [f"project.json is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}."]
    try:
        project = Project.model_validate(content)
    except ValidationError as e:
        return [
            f"{'.'.join(str(part) for part in err['loc']) or 'file'}: {_field_reason(err)}" for err in e.errors()
        ]
    if project.id != project_id:
        return [f"id: is '{project.id}', but this is the file of project '{project_id}'. The id must stay '{project_id}'."]
    return []


def validate_upload(project_id: str, name: str, content: bytes) -> list[str]:
    """Plain error lines for an upload; empty means it may replace the current file."""
    if name not in FILE_NAMES:
        return [f"'{name}' is not a project file. Upload one of: {', '.join(FILE_NAMES)}."]
    if not _served(project_id):
        return [f"Project '{project_id}' is not served by this instance."]
    if len(content) > MAX_UPLOAD_BYTES:
        return [f"{name} is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB."]
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{name} is not UTF-8 text. Save it as UTF-8 and upload it again."]
    if name == "project.json":
        return _project_errors(project_id, text)
    if name == "examples.md" and not parse_examples(text):
        return [
            "examples.md has no example: each needs a '## ' heading, a 'Question:' line and a ```sparql block."
        ]
    if name == "dataset.md" and not text.strip():
        return ["dataset.md is empty."]
    return []


def _read_log(project_id: str) -> list[dict[str, str]]:
    try:
        return json.loads((_project_dir(project_id) / UPLOAD_LOG).read_text(encoding="utf-8"))
    except Exception:
        return []


def save_upload(project_id: str, name: str, content: bytes, user_email: str) -> None:
    """Replace the file atomically, keeping the previous version as ``<name>.prev``."""
    directory = _project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    tmp = directory / f"{name}.tmp{os.getpid()}"
    tmp.write_bytes(content)
    if target.exists():
        os.replace(target, directory / f"{name}.prev")
    os.replace(tmp, target)
    log = _read_log(project_id)
    log.append({"file": name, "by": user_email, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    (directory / UPLOAD_LOG).write_text(json.dumps(log[-200:], indent=2, ensure_ascii=False), encoding="utf-8")


def restore_previous(project_id: str, name: str) -> None:
    """Swap the file with its previous version, so an undo can itself be undone."""
    if name not in FILE_NAMES or not _served(project_id):
        raise ProjectFileError(f"'{name}' of project '{project_id}' is not a project file.")
    directory = _project_dir(project_id)
    previous = directory / f"{name}.prev"
    if not previous.exists():
        raise ProjectFileError(f"There is no previous version of {name} to restore.")
    current = directory / name
    swap = directory / f"{name}.swap{os.getpid()}"
    if current.exists():
        os.replace(current, swap)
    os.replace(previous, current)
    if swap.exists():
        os.replace(swap, previous)


def file_path(project_id: str, name: str) -> Path:
    """The path of a project file, for download. Raises for anything that is not one."""
    if name not in FILE_NAMES or not _served(project_id):
        raise ProjectFileError(f"'{name}' of project '{project_id}' is not a project file.")
    path = _project_dir(project_id) / name
    if not path.is_file():
        raise ProjectFileError(f"{name} has not been uploaded yet.")
    return path


def list_files(project_id: str) -> list[dict[str, Any]]:
    """Name, size, last change and last uploader of each project file."""
    directory = _project_dir(project_id)
    last_upload: dict[str, dict[str, str]] = {}
    for entry in _read_log(project_id):
        last_upload[entry.get("file", "")] = entry
    files = []
    for name in FILE_NAMES:
        path = directory / name
        exists = path.is_file()
        stat = path.stat() if exists else None
        files.append(
            {
                "name": name,
                "exists": exists,
                "size": stat.st_size if stat else 0,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
                if stat
                else None,
                "uploaded_by": last_upload.get(name, {}).get("by"),
                "uploaded_at": last_upload.get(name, {}).get("at"),
                "has_previous": (directory / f"{name}.prev").exists(),
            }
        )
    return files
