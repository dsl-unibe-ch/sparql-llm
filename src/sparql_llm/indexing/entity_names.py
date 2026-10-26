"""Write every nameable entity of a project's endpoint to data/<id>/entity_names.json.

The entity resolver matches the names a user types against these labels. Which classes
count as entities, and their label property, come from the project's ``entities`` block.
Types with birth and death properties carry those years so homonyms can be told apart.
Built by the admin rebuild; a failure keeps the previous file.

Usage:
    uv run python -m sparql_llm.indexing.entity_names [project id]
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sparql_llm.project import Project, get_project, get_projects, graph_pattern
from sparql_llm.utils import logger, query_sparql

PAGE = 10000
MIN_KEPT_FRACTION = 0.9


def entity_query(project: Project, type_name: str, offset: int) -> str:
    """One page of the entities of one type, with birth/death years when the type has them."""
    assert project.entities is not None
    entity_type = project.entities.types[type_name]
    years = ""
    if entity_type.birth_property:
        years += f" OPTIONAL {{ ?s <{entity_type.birth_property}> ?birth }}"
    if entity_type.death_property:
        years += f" OPTIONAL {{ ?s <{entity_type.death_property}> ?death }}"
    pattern = f"?s a <{entity_type.class_iri}> ; <{project.entities.label_property}> ?l .{years}"
    return (
        f"SELECT ?s ?l ?birth ?death WHERE {{ {graph_pattern(project.graphs, pattern)} }}\n"
        f"ORDER BY ?s LIMIT {PAGE} OFFSET {offset}"
    )


def fetch_entities(project: Project, fetch: Callable[..., Any] = query_sparql) -> list[dict[str, str]]:
    """Page through every entity type of the project. Raises on an endpoint error."""
    rows: list[dict[str, str]] = []
    if project.entities is None:
        return rows
    post = project.method == "POST"
    for type_name in project.entities.types:
        offset = 0
        while True:
            query = entity_query(project, type_name, offset)
            bindings = fetch(query, project.endpoint_url, post=post, timeout=180)["results"]["bindings"]
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


def _source(project: Project) -> dict[str, Any]:
    """What a list of names was built from: a list from another source is no baseline."""
    assert project.entities is not None
    return {"endpoint": project.endpoint_url, "graphs": project.graphs, "entities": project.entities.model_dump()}


def _previous_count(path: Path, source: dict[str, Any]) -> int:
    """Entities in the current file if it was built from the same source; else 0."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if "source" in payload:
        same = payload["source"] == source
    else:
        # Older files record only the endpoint, or nothing: keep the guard unless it differs.
        same = payload.get("endpoint", source["endpoint"]) == source["endpoint"]
    if not same:
        return 0
    return len(payload.get("entities", []))


def build_entity_names(project: Project, fetch: Callable[..., Any] = query_sparql) -> dict[str, Any]:
    """Fetch and write the project's entity file atomically. Never raises; keeps the old file on failure."""
    path = project.entity_names_path
    endpoint = project.endpoint_url
    report: dict[str, Any] = {"entities": 0, "path": str(path), "error": ""}
    if project.entities is None:
        report["skipped"] = True
        return report
    try:
        rows = fetch_entities(project, fetch)
        if not rows:
            raise ValueError("the endpoint returned no entities")
        # The baseline is the list in service, not what an earlier attempt left in next/.
        previous = _previous_count(project.live_entity_names_path, _source(project))
        if previous and len(rows) < MIN_KEPT_FRACTION * previous:
            # A short page is read as "done", so an endpoint row cap would truncate the list
            # without an error. A large drop is far likelier to be that than real deletions.
            raise ValueError(f"only {len(rows)} entities against {previous} in the previous list")
        payload = {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "endpoint": endpoint,
            "source": _source(project),
            "entities": rows,
        }
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
    print(build_entity_names(get_project(sys.argv[1]) if len(sys.argv) > 1 else get_projects()[0]))
