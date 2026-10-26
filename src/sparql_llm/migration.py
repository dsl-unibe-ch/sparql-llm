"""One-off move of the generated files that used to sit flat in data/ into data/<project id>/.

Runs at app startup and at the start of every rebuild. It is idempotent and never
overwrites a file that already exists in the project folder.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from sparql_llm.utils import logger

#: Project files shipped with the code, copied into the data folder when a project has none
#: there yet. After that the server copy is the one curators edit (admin → Project files):
#: git never tracks live project files, so uploads cannot block a later `git pull`.
SEEDS_DIR = Path(__file__).resolve().parents[2] / "projects"
SEED_FILES = ("project.json", "dataset.md", "examples.md")

LEGACY_FILES = {
    "endpoints_metadata.json": "prefixes.json",
    "entity_names.json": "entity_names.json",
    "index_fingerprint.json": "fingerprint.json",
    "elites-suisses-examples-synced.md": "examples-synced.md",
}


def migrate_legacy_data(data_dir: Path, project_id: str) -> list[str]:
    """Move legacy files into the project folder; return the moves made."""
    data_dir = Path(data_dir)
    moves = []
    for old_name, new_name in LEGACY_FILES.items():
        old = data_dir / old_name
        new = data_dir / project_id / new_name
        if not old.is_file() or new.exists():
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)
        moves.append(f"{old} -> {new}")
        logger.info(f"Moved {old} to {new}")
    return moves


def seed_projects(data_dir: Path, project_ids: list[str], seeds_dir: Path = SEEDS_DIR) -> list[str]:
    """Copy the shipped files of each project that has no live project.json yet; return their ids."""
    seeded = []
    for pid in project_ids:
        live, seed = Path(data_dir) / pid, Path(seeds_dir) / pid
        if (live / "project.json").exists() or not (seed / "project.json").is_file():
            continue
        live.mkdir(parents=True, exist_ok=True)
        for name in SEED_FILES:
            if (seed / name).is_file() and not (live / name).exists():
                shutil.copyfile(seed / name, live / name)
        seeded.append(pid)
        logger.info(f"Seeded project {pid} from {seed}")
    return seeded


#: project.json field <- install setting it used to be, before each dataset had its own texts.
PROJECT_TEXTS = {"intro": "app_intro", "example_questions": "example_questions", "topics": "app_topics", "org": "app_org"}


def fill_project_texts(project_dir: Path, settings: Any) -> bool:
    """Copy the install's intro, examples, topics and organisation into a live project.json that has none.

    Only when all four are absent, so a curator's later edit is never overwritten.
    """
    path = Path(project_dir) / "project.json"
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(content, dict) or any(field in content for field in PROJECT_TEXTS):
        return False
    for field, setting in PROJECT_TEXTS.items():
        content[field] = getattr(settings, setting)
    tmp = path.with_name(f"project.json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    logger.info(f"Copied the install's intro, examples and topics into {path}")
    return True


def migrate_configured() -> list[str]:
    """Seed the dataset list and missing projects, then move the flat legacy files into the first project."""
    from sparql_llm.config import settings
    from sparql_llm.datasets import list_dataset_ids, seed_dataset_list

    seed_dataset_list(Path(settings.data_dir), settings.projects)
    project_ids = list_dataset_ids()
    if not project_ids:
        return []
    seed_projects(Path(settings.data_dir), project_ids)
    # The install's texts described the one dataset there was: the first.
    fill_project_texts(Path(settings.data_dir) / project_ids[0], settings)
    return migrate_legacy_data(Path(settings.data_dir), project_ids[0])
