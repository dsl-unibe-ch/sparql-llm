"""A project's three curator files, uploaded and downloaded from the admin page.

An upload is validated, then kept in ``<id>/uploaded/`` until a Rebuild has checked it
and promotes it to the live folder (keeping the replaced file as ``<file>.prev``). Nothing
uploaded is served before that, not even after a restart.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sparql_llm.indexing.sync_examples import parse_examples
from sparql_llm.project import UPLOADED_DIR, Project

FILE_NAMES = ("project.json", "dataset.md", "examples.md")
MAX_UPLOAD_BYTES = 1_000_000
UPLOAD_LOG = "uploads.json"


class ProjectFileError(Exception):
    """A request about a project file that cannot be served. Plain message."""


def _project_dir(project_id: str) -> Path:
    from sparql_llm.config import settings

    return Path(settings.data_dir) / project_id


def _served(project_id: str) -> bool:
    from sparql_llm.datasets import list_dataset_ids

    return project_id in list_dataset_ids()


def endpoint_owner(project_id: str, endpoint_url: str) -> str | None:
    """Another dataset whose live or uploaded project.json uses this endpoint, if any."""
    from sparql_llm.datasets import list_dataset_ids

    wanted = endpoint_url.rstrip("/")
    for other in list_dataset_ids():
        if other == project_id:
            continue
        for path in (_project_dir(other) / "project.json", _project_dir(other) / UPLOADED_DIR / "project.json"):
            try:
                url = json.loads(path.read_text(encoding="utf-8")).get("endpoint_url", "")
            except Exception:
                continue
            if isinstance(url, str) and url.rstrip("/") == wanted:
                return other
    return None


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
    if kind == "value_error":
        return error["msg"].removeprefix("Value error, ")
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
    owner = endpoint_owner(project_id, project.endpoint_url)
    if owner:
        return [f"endpoint_url: is already used by dataset '{owner}'. Each dataset needs its own endpoint."]
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


def _log(project_id: str, name: str, user_email: str, action: str) -> None:
    directory = _project_dir(project_id)
    log = _read_log(project_id)
    log.append(
        {"file": name, "by": user_email, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action}
    )
    (directory / UPLOAD_LOG).write_text(json.dumps(log[-200:], indent=2, ensure_ascii=False), encoding="utf-8")


def save_upload(project_id: str, name: str, content: bytes, user_email: str) -> None:
    """Keep the upload pending in ``uploaded/``; the next successful Rebuild makes it live."""
    staged = _project_dir(project_id) / UPLOADED_DIR
    staged.mkdir(parents=True, exist_ok=True)
    tmp = staged / f"{name}.tmp{os.getpid()}"
    tmp.write_bytes(content)
    os.replace(tmp, staged / name)
    _log(project_id, name, user_email, "upload")


def restore_previous(project_id: str, name: str, user_email: str = "") -> None:
    """Undo: drop a pending upload, or else stage the previous live version for the next Rebuild."""
    if name not in FILE_NAMES or not _served(project_id):
        raise ProjectFileError(f"'{name}' of project '{project_id}' is not a project file.")
    directory = _project_dir(project_id)
    pending = directory / UPLOADED_DIR / name
    if pending.is_file():
        pending.unlink()
        _log(project_id, name, user_email, "discard upload")
        return
    previous = directory / f"{name}.prev"
    if not previous.exists():
        raise ProjectFileError(f"There is no previous version of {name} to restore.")
    # The previous version is checked like an upload: it may name an endpoint that
    # another dataset has used since.
    errors = validate_upload(project_id, name, previous.read_bytes())
    if errors:
        raise ProjectFileError(" ".join(errors))
    save_upload(project_id, name, previous.read_bytes(), user_email)


def file_path(project_id: str, name: str) -> Path:
    """The file to download: the pending upload if there is one, else the live file."""
    if name not in FILE_NAMES or not _served(project_id):
        raise ProjectFileError(f"'{name}' of project '{project_id}' is not a project file.")
    for path in (_project_dir(project_id) / UPLOADED_DIR / name, _project_dir(project_id) / name):
        if path.is_file():
            return path
    raise ProjectFileError(f"{name} has not been uploaded yet.")


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
                # Uploaded and not live yet: the next successful Rebuild makes it live.
                "pending": (directory / UPLOADED_DIR / name).is_file(),
            }
        )
    return files
