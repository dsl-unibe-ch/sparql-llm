"""Write every nameable entity in the endpoint to data/entity_names.json.

The entity resolver matches the names a user types against these labels. Persons carry
birth and death years so homonyms (2,729 persons share a label with someone else) can be
told apart. Built by the admin rebuild; a failure keeps the previous file.

Usage:
    uv run python -m sparql_llm.indexing.entity_names
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sparql_llm.config import settings
from sparql_llm.utils import logger, query_sparql

ENTITY_NAMES_FILE = Path("data") / "entity_names.json"
GRAPH = "https://swiss-elites.lod4hss.cloud/resource/"
LABEL = "https://sdhss.org/ontology/shortcuts/P9"
BIRTH = "https://sdhss.org/ontology/shortcuts/P2"
DEATH = "https://sdhss.org/ontology/shortcuts/P13"
PAGE = 10000

ENTITY_TYPES: dict[str, str] = {
    "Person": "http://www.cidoc-crm.org/cidoc-crm/E21",
    "Group": "http://www.cidoc-crm.org/cidoc-crm/E74",
    "Place": "https://sdhss.org/ontology/core/C13",
    "Discipline": "https://sdhss.org/ontology/social-life-specific/C9",
    "Study title": "https://sdhss.org/ontology/social-life-specific/C8",
}


def _query(class_iri: str, offset: int) -> str:
    years = ""
    if class_iri == ENTITY_TYPES["Person"]:
        years = f"OPTIONAL {{ ?s <{BIRTH}> ?birth }} OPTIONAL {{ ?s <{DEATH}> ?death }}"
    return (
        f"SELECT ?s ?l ?birth ?death WHERE {{ GRAPH <{GRAPH}> {{ ?s a <{class_iri}> ; <{LABEL}> ?l . {years} }} }}\n"
        f"ORDER BY ?s LIMIT {PAGE} OFFSET {offset}"
    )


def fetch_entities(endpoint: str, fetch: Callable[..., Any] = query_sparql) -> list[dict[str, str]]:
    """Page through every entity type. Raises on an endpoint error."""
    rows: list[dict[str, str]] = []
    for type_name, class_iri in ENTITY_TYPES.items():
        offset = 0
        while True:
            bindings = fetch(_query(class_iri, offset), endpoint, post=False, timeout=180)["results"]["bindings"]
            for b in bindings:
                row = {"uri": b["s"]["value"], "label": b["l"]["value"], "type": type_name}
                for key in ("birth", "death"):
                    if key in b:
                        row[key] = b[key]["value"]
                rows.append(row)
            if len(bindings) < PAGE:
                break
            offset += PAGE
    return rows


def build_entity_names(
    path: Path = ENTITY_NAMES_FILE,
    endpoint: str | None = None,
    fetch: Callable[..., Any] = query_sparql,
) -> dict[str, Any]:
    """Fetch and write the entity file atomically. Never raises; keeps the old file on failure."""
    endpoint = endpoint or settings.endpoints[0]["endpoint_url"]
    report: dict[str, Any] = {"entities": 0, "path": str(path), "error": ""}
    try:
        rows = fetch_entities(endpoint, fetch)
        if not rows:
            raise ValueError("the endpoint returned no entities")
        payload = {"built_at": datetime.now(timezone.utc).isoformat(), "endpoint": endpoint, "entities": rows}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
        report["entities"] = len(rows)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("Entity names not rebuilt, keeping the previous file: %s", report["error"])
    return report


if __name__ == "__main__":
    print(build_entity_names())
