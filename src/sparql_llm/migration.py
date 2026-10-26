"""One-off move of the generated files that used to sit flat in data/ into data/<project id>/.

Runs at app startup and at the start of every rebuild. It is idempotent and never
overwrites a file that already exists in the project folder.
"""

from __future__ import annotations

import shutil
from pathlib import Path

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


def migrate_configured() -> list[str]:
    """Seed missing projects, then move the flat legacy files into the first project."""
    from sparql_llm.config import settings

    if not settings.projects:
        return []
    seed_projects(Path(settings.data_dir), settings.projects)
    return migrate_legacy_data(Path(settings.data_dir), settings.projects[0])
