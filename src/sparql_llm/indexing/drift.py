"""Detect when the indexed schema snapshot no longer matches the live endpoint.

The retrieval index is built from a snapshot of the endpoint: class/property statistics
in the VoID file, ontology labels and SHACL shapes fetched at index time. None of that
updates itself, so when the curators reload the triplestore the assistant keeps answering
from a stale picture — silently, and for as long as nobody happens to notice. That is
exactly what happened between 2026-06 and 2026-08: the database grew 61% and gained the
entire geographic model while the index still described nine classes.

This module takes a cheap fingerprint of the endpoint (three aggregate queries), stores
the one that was current at index time next to the index, and compares them on demand.
It answers one question — "is a rebuild needed, and what changed?" — and never rebuilds
anything itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sparql_llm.project import Project, get_projects, graph_pattern
from sparql_llm.utils import logger, query_sparql

#: Bounds on each fingerprint query, in seconds: per wait, and for the whole answer.
FINGERPRINT_TIMEOUT = 30
FINGERPRINT_DEADLINE = 90

# The fingerprint taken at index time is kept in each project's folder (project.fingerprint_path).
# The graph clause follows the project's graphs: none, one, or several.
_TRIPLES_QUERY = """
SELECT (COUNT(*) AS ?n) WHERE { %(triples)s }
"""

_CLASSES_QUERY = """
SELECT ?class (COUNT(?s) AS ?n) WHERE { %(classes)s }
GROUP BY ?class ORDER BY ?class
"""

_PREDICATES_QUERY = """
SELECT DISTINCT ?p WHERE { %(triples)s } ORDER BY ?p
"""


@dataclass
class Fingerprint:
    """A cheap, comparable summary of what the endpoint currently holds."""

    triples: int = 0
    classes: dict[str, int] = field(default_factory=dict)
    predicates: list[str] = field(default_factory=list)
    taken_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "triples": self.triples,
            "classes": self.classes,
            "predicates": self.predicates,
            "taken_at": self.taken_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Fingerprint":
        return cls(
            triples=int(raw.get("triples", 0)),
            classes={str(k): int(v) for k, v in (raw.get("classes") or {}).items()},
            predicates=list(raw.get("predicates") or []),
            taken_at=str(raw.get("taken_at", "")),
        )


def take_fingerprint(project: Project, fetch=query_sparql) -> Fingerprint:
    """Query the project's endpoint for its current shape. Three aggregate queries, a few seconds."""
    endpoint_url = project.endpoint_url
    post = project.method == "POST"
    subs = {
        "triples": graph_pattern(project.graphs, "?s ?p ?o"),
        "classes": graph_pattern(project.graphs, "?s a ?class"),
    }

    # Each query gives up in bounded time: a status check or a rebuild must never wait forever.
    bounds = {"post": post, "timeout": FINGERPRINT_TIMEOUT, "deadline": FINGERPRINT_DEADLINE}

    triples = 0
    rows = fetch(_TRIPLES_QUERY % subs, endpoint_url, **bounds)["results"]["bindings"]
    if rows:
        triples = int(rows[0]["n"]["value"])

    classes: dict[str, int] = {}
    for row in fetch(_CLASSES_QUERY % subs, endpoint_url, **bounds)["results"]["bindings"]:
        classes[row["class"]["value"]] = int(row["n"]["value"])

    predicates = [
        row["p"]["value"] for row in fetch(_PREDICATES_QUERY % subs, endpoint_url, **bounds)["results"]["bindings"]
    ]

    return Fingerprint(
        triples=triples,
        classes=classes,
        predicates=predicates,
        taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def save_fingerprint(fp: Fingerprint, project: Project) -> None:
    """Record the fingerprint that a freshly built index corresponds to."""
    path = project.fingerprint_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fp.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved index fingerprint to %s (%s triples)", path, fp.triples)


def load_fingerprint(project: Project) -> Fingerprint | None:
    """The fingerprint the current index was built from, or None if never recorded."""
    path = project.fingerprint_path
    if not path.exists():
        return None
    try:
        return Fingerprint.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # a corrupt file must not take the app down
        logger.warning("Could not read index fingerprint at %s: %s", path, exc)
        return None


def _short(iri: str) -> str:
    """Trim an IRI to something readable in a UI without a full prefix converter."""
    for sep in ("#", "/"):
        if sep in iri:
            tail = iri.rsplit(sep, 1)[-1]
            if tail:
                # Keep one path segment of context: .../social-life-core/C5 -> social-life-core:C5
                head = iri[: -(len(tail) + 1)].rstrip("/#").rsplit("/", 1)[-1]
                return f"{head}:{tail}" if head else tail
    return iri


def compare(indexed: Fingerprint | None, live: Fingerprint) -> dict[str, Any]:
    """Describe the drift between the indexed snapshot and the endpoint right now.

    Returns a dict shaped for direct rendering: ``stale`` plus a list of human-readable
    ``changes``. Never raises — a drift check must not be able to break the admin page.
    """
    if indexed is None:
        return {
            "stale": True,
            "reason": "no-fingerprint",
            "changes": ["No fingerprint recorded — the index predates drift tracking, or was never built."],
            "indexed": None,
            "live": live.to_dict(),
        }

    changes: list[str] = []

    if live.triples != indexed.triples:
        delta = live.triples - indexed.triples
        pct = (delta / indexed.triples * 100) if indexed.triples else 0.0
        changes.append(
            f"Triples {indexed.triples:,} → {live.triples:,} ({delta:+,}, {pct:+.1f}%)"
        )

    added = sorted(set(live.classes) - set(indexed.classes))
    removed = sorted(set(indexed.classes) - set(live.classes))
    if added:
        changes.append("New classes: " + ", ".join(f"{_short(c)} ({live.classes[c]:,})" for c in added))
    if removed:
        changes.append("Classes no longer present: " + ", ".join(_short(c) for c in removed))

    # Only report per-class count moves for classes present on both sides, and only when
    # the move is big enough to matter — small edits are normal curation noise.
    shifted = []
    for c in sorted(set(live.classes) & set(indexed.classes)):
        before, after = indexed.classes[c], live.classes[c]
        if before and abs(after - before) / before >= 0.05:
            shifted.append(f"{_short(c)} {before:,} → {after:,}")
    if shifted:
        changes.append("Counts changed: " + ", ".join(shifted))

    new_preds = sorted(set(live.predicates) - set(indexed.predicates))
    gone_preds = sorted(set(indexed.predicates) - set(live.predicates))
    if new_preds:
        changes.append("New predicates: " + ", ".join(_short(p) for p in new_preds))
    if gone_preds:
        changes.append("Predicates no longer present: " + ", ".join(_short(p) for p in gone_preds))

    return {
        "stale": bool(changes),
        "reason": "drift" if changes else "current",
        "changes": changes,
        "indexed": indexed.to_dict(),
        "live": live.to_dict(),
    }


def check_drift(project_id: str = "") -> dict[str, Any]:
    """Full check of one dataset: take a live fingerprint and compare it to the indexed one.

    Returns the same shape as :func:`compare`, with ``error`` set instead of ``stale``
    when the endpoint could not be reached. Defaults to the first project.
    """
    projects = [p for p in get_projects() if not project_id or p.id == project_id]
    if not projects:
        return {
            "stale": False,
            "reason": "no-project",
            "error": "This dataset has not been rebuilt successfully yet." if project_id
            else "No project is configured for this instance.",
            "changes": [],
            "indexed": None,
            "live": None,
        }
    project = projects[0]
    try:
        live = take_fingerprint(project)
    except Exception as exc:
        logger.warning("Drift check could not reach the endpoint: %s", exc)
        return {
            "stale": False,
            "reason": "endpoint-unreachable",
            "error": str(exc),
            "changes": [],
            "indexed": (load_fingerprint(project) or Fingerprint()).to_dict(),
            "live": None,
        }
    return compare(load_fingerprint(project), live)
