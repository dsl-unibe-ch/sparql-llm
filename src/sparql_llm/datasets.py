"""The datasets this instance serves, kept in ``<data_dir>/datasets.json`` and changed from the admin page.

Each entry is ``{"id", "published"}``, in the order the pickers show them. A dataset is:
- *new* until a rebuild has promoted its files (no live ``project.json`` and ``fingerprint.json``);
- *ready* after that: admins and curators can try it in the chat;
- *published* once an admin publishes it: every user sees it. A rebuild never publishes.

On upgrade the list is seeded from the install settings' ``projects``, all published.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from sparql_llm.project import PROJECT_FILE, UPLOADED_DIR, Project

DATASETS_FILE = "datasets.json"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: Staged for a new dataset until its curator uploads the real project.json.
PLACEHOLDER_ENDPOINT = "https://example.org/sparql"


class DatasetError(Exception):
    """A dataset cannot be added or changed. Plain message."""


def _data_dir() -> Path:
    from sparql_llm.config import settings

    return Path(settings.data_dir)


def _write(data_dir: Path, entries: list[dict[str, Any]]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / f"{DATASETS_FILE}.tmp{os.getpid()}"
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, data_dir / DATASETS_FILE)


def _entries() -> list[dict[str, Any]]:
    path = _data_dir() / DATASETS_FILE
    if not path.is_file():
        # Before the upgrade seeded the list: the install settings still say what is served.
        from sparql_llm.config import settings

        return [{"id": pid, "published": True} for pid in settings.projects]
    return json.loads(path.read_text(encoding="utf-8"))


def seed_dataset_list(data_dir: Path, project_ids: list[str]) -> bool:
    """Write the list from the install settings when there is none yet; return whether it wrote."""
    if (Path(data_dir) / DATASETS_FILE).exists() or not project_ids:
        return False
    _write(Path(data_dir), [{"id": pid, "published": True} for pid in project_ids])
    return True


def list_dataset_ids() -> list[str]:
    return [entry["id"] for entry in _entries()]


def add_dataset(dataset_id: str, label: str) -> None:
    """An empty dataset, not published, with a minimal project.json staged for its first upload."""
    if not _ID.fullmatch(dataset_id or ""):
        raise DatasetError(
            f"'{dataset_id}' is not a valid id: use lowercase letters, digits and dashes, starting with a letter or digit."
        )
    if not label.strip():
        raise DatasetError("Give the dataset a name.")
    entries = _entries()
    if any(entry["id"] == dataset_id for entry in entries):
        raise DatasetError(f"A dataset with the id '{dataset_id}' already exists.")
    staged = _data_dir() / dataset_id / UPLOADED_DIR
    staged.mkdir(parents=True, exist_ok=True)
    (staged / PROJECT_FILE).write_text(
        json.dumps({"id": dataset_id, "label": label.strip(), "endpoint_url": PLACEHOLDER_ENDPOINT}, indent=2),
        encoding="utf-8",
    )
    _write(_data_dir(), [*entries, {"id": dataset_id, "published": False}])


def dataset_state(dataset_id: str) -> Literal["new", "ready", "published"]:
    for entry in _entries():
        if entry["id"] == dataset_id:
            if entry.get("published"):
                return "published"
            live = _data_dir() / dataset_id
            return "ready" if (live / PROJECT_FILE).is_file() and (live / "fingerprint.json").is_file() else "new"
    raise DatasetError(f"There is no dataset '{dataset_id}'.")


def set_published(dataset_id: str, published: bool) -> None:
    if published and dataset_state(dataset_id) == "new":
        raise DatasetError(f"The dataset '{dataset_id}' has not been rebuilt successfully yet.")
    entries = _entries()
    for entry in entries:
        if entry["id"] == dataset_id:
            entry["published"] = published
    _write(_data_dir(), entries)


def datasets_for(user: Any) -> list[Project]:
    """The served datasets ``user`` may see and ask: published ones, plus ready ones for managers."""
    from sparql_llm.agent.roles import can_manage
    from sparql_llm.project import get_projects

    allowed = ("ready", "published") if can_manage(user) else ("published",)
    served = {p.id: p for p in get_projects()}
    return [served[pid] for pid in list_dataset_ids() if pid in served and dataset_state(pid) in allowed]
