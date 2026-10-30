"""The datasets this instance serves, kept in ``<data_dir>/datasets.json`` and changed from the admin page.

Each entry is ``{"id", "published"}``, in the order the pickers show them. A dataset is:
- *new* until a rebuild has promoted its files (no live ``project.json`` and ``fingerprint.json``);
- *ready* after that: admins and curators can try it in the chat;
- *published* once an admin publishes it: every user sees it. A rebuild never publishes.

On upgrade the list is seeded from the install settings' ``projects``, all published.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
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
    from sparql_llm.indexing.publication import write_durable_json

    write_durable_json(data_dir / DATASETS_FILE, entries)


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
    # Datasets share the data folder with the index, logs and archive: an id must not name
    # one of those, or any folder already there, which removing the dataset would archive.
    if dataset_id in RESERVED_IDS or (_data_dir() / dataset_id).exists():
        raise DatasetError(f"The id '{dataset_id}' is taken by a folder of this installation: choose another.")
    # Saved chats name their dataset by id: a new dataset under a removed one's id would
    # show, and resume, the removed dataset's chats as its own.
    archived = re.compile(re.escape(dataset_id) + r"-\d{8}-\d{6}")
    removed = _data_dir() / REMOVED_DIR
    if removed.is_dir() and any(archived.fullmatch(p.name) for p in removed.iterdir()):
        raise DatasetError(f"The id '{dataset_id}' belonged to a removed dataset, whose saved chats still use it: choose another.")
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


def dataset_label(dataset_id: str) -> str:
    """The name to show: the live project's, else the one staged for a new dataset, else the id."""
    for path in (_data_dir() / dataset_id / PROJECT_FILE, _data_dir() / dataset_id / UPLOADED_DIR / PROJECT_FILE):
        try:
            label = json.loads(path.read_text(encoding="utf-8")).get("label")
        except Exception:
            continue
        if isinstance(label, str) and label.strip():
            return label
    return dataset_id


def set_published(dataset_id: str, published: bool) -> None:
    if published and dataset_state(dataset_id) == "new":
        raise DatasetError(f"The dataset '{dataset_id}' has not been rebuilt successfully yet.")
    entries = _entries()
    for entry in entries:
        if entry["id"] == dataset_id:
            entry["published"] = published
    _write(_data_dir(), entries)


#: Removed datasets' folders are moved here, not deleted: a developer can bring one back.
REMOVED_DIR = "removed"
REMOVAL_PREFIX = "removing-"

#: Folders of the installation itself in the data folder, whether or not they exist yet.
RESERVED_IDS = frozenset({REMOVED_DIR, "vectordb", "logs", "ontology", "fastembed_cache"})


def _own_folder(dataset_id: str) -> bool:
    """Whether data/<id> is this dataset's folder: its live or staged project.json names it."""
    folder = _data_dir() / dataset_id
    for path in (folder / PROJECT_FILE, folder / UPLOADED_DIR / PROJECT_FILE):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("id") == dataset_id:
                return True
        except Exception:
            continue
    return False


def check_removable(dataset_id: str) -> None:
    """Raise DatasetError unless the dataset may be removed: unpublished, not the last one, and
    its folder (if any) its own, never a shared one that happens to carry its name."""
    entries = _entries()
    if not any(entry["id"] == dataset_id for entry in entries):
        raise DatasetError(f"There is no dataset '{dataset_id}'.")
    if dataset_state(dataset_id) == "published":
        raise DatasetError(f"Unpublish '{dataset_label(dataset_id)}' before removing it.")
    if len(entries) == 1:
        raise DatasetError("The last dataset cannot be removed.")
    if (_data_dir() / dataset_id).exists() and not _own_folder(dataset_id):
        raise DatasetError(
            f"The folder '{dataset_id}' in the data folder is not this dataset's own (no project.json naming it): "
            "nothing was removed. Ask a developer to remove the dataset by hand."
        )


def removal_path(dataset_id: str) -> Path:
    """The intent to remove a dataset: once written, the removal finishes, at the next start if need be."""
    return _data_dir() / f"{REMOVAL_PREFIX}{dataset_id}.json"


def begin_removal(dataset_id: str) -> None:
    """Check the dataset may be removed and record the intent, keeping the archive name of an earlier try."""
    from sparql_llm.indexing.publication import write_durable_json

    check_removable(dataset_id)
    if removal_path(dataset_id).exists():
        return
    archive = f"{REMOVED_DIR}/{dataset_id}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    write_durable_json(removal_path(dataset_id), {"dataset": dataset_id, "archive": archive})


def finish_removal(dataset_id: str) -> Path | None:
    """Do what is left of a removal; each step is skipped when already done. Returns the archive, if any.

    It does not check ``check_removable`` again: the dataset passed it, and its folder may be gone by now.
    """
    intent = removal_path(dataset_id)
    archived = _data_dir() / json.loads(intent.read_text(encoding="utf-8"))["archive"]
    folder = _data_dir() / dataset_id
    if folder.exists():
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(archived))
    entries = _entries()
    if any(entry["id"] == dataset_id for entry in entries):
        _write(_data_dir(), [entry for entry in entries if entry["id"] != dataset_id])
    intent.unlink(missing_ok=True)
    return archived if archived.exists() else None


def pending_removals() -> list[str]:
    """The datasets whose removal began and did not finish."""
    return sorted(p.name[len(REMOVAL_PREFIX) : -len(".json")] for p in _data_dir().glob(f"{REMOVAL_PREFIX}*.json"))


def remove_dataset(dataset_id: str) -> Path | None:
    """Take a dataset out of the list and move its folder to ``removed/<id>-<time>``.

    Its search index is dropped separately (``rebuild.drop_index``), between the two steps
    when both happen. Saved chats about it stay in the database, hidden: no page lists a
    dataset that is not in the list.
    """
    begin_removal(dataset_id)
    return finish_removal(dataset_id)


def datasets_for(user: Any) -> list[Project]:
    """The served datasets ``user`` may see and ask: published ones, plus ready ones for managers."""
    from sparql_llm.agent.roles import can_manage
    from sparql_llm.project import get_projects

    allowed = ("ready", "published") if can_manage(user) else ("published",)
    served = {p.id: p for p in get_projects()}
    return [served[pid] for pid in list_dataset_ids() if pid in served and dataset_state(pid) in allowed]
