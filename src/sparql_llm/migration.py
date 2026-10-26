"""One-off move of the generated files that used to sit flat in data/ into data/<project id>/.

Runs at app startup and at the start of every rebuild. It is idempotent and never
overwrites a file that already exists in the project folder.
"""

from __future__ import annotations

from pathlib import Path

from sparql_llm.utils import logger

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


def migrate_configured() -> list[str]:
    """Migrate into the first configured project, the one the flat files belonged to."""
    from sparql_llm.config import settings

    if not settings.projects:
        return []
    return migrate_legacy_data(Path(settings.data_dir), settings.projects[0])
