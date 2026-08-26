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
from pathlib import Path
from typing import Any

from sparql_llm.config import settings
from sparql_llm.utils import logger, query_sparql

#: Where the fingerprint taken at index time is kept. Lives beside the vector store so a
#: wiped index and a wiped fingerprint stay in step.
FINGERPRINT_FILE = Path("data") / "index_fingerprint.json"

#: The graph holding the project data. Note this is still the ``swiss-elites.lod4hss.cloud``
#: name even though entity URIs are now minted under ``elites-suisses.lod4hss.org`` — the
#: graph name did not move with the URIs.
DATA_GRAPH = "https://swiss-elites.lod4hss.cloud/resource/"

_TRIPLES_QUERY = """
SELECT (COUNT(*) AS ?n) WHERE { GRAPH <%(g)s> { ?s ?p ?o } }
"""

_CLASSES_QUERY = """
SELECT ?class (COUNT(?s) AS ?n) WHERE { GRAPH <%(g)s> { ?s a ?class } }
GROUP BY ?class ORDER BY ?class
"""

_PREDICATES_QUERY = """
SELECT DISTINCT ?p WHERE { GRAPH <%(g)s> { ?s ?p ?o } } ORDER BY ?p
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


def take_fingerprint(endpoint_url: str | None = None) -> Fingerprint:
    """Query the endpoint for its current shape. Three aggregate queries, a few seconds."""
    endpoint_url = endpoint_url or settings.endpoints[0]["endpoint_url"]
    subs = {"g": DATA_GRAPH}

    triples = 0
    rows = query_sparql(_TRIPLES_QUERY % subs, endpoint_url)["results"]["bindings"]
    if rows:
        triples = int(rows[0]["n"]["value"])

    classes: dict[str, int] = {}
    for row in query_sparql(_CLASSES_QUERY % subs, endpoint_url)["results"]["bindings"]:
        classes[row["class"]["value"]] = int(row["n"]["value"])

    predicates = [
        row["p"]["value"]
        for row in query_sparql(_PREDICATES_QUERY % subs, endpoint_url)["results"]["bindings"]
    ]

    return Fingerprint(
        triples=triples,
        classes=classes,
        predicates=predicates,
        taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def save_fingerprint(fp: Fingerprint, path: Path = FINGERPRINT_FILE) -> None:
    """Record the fingerprint that a freshly built index corresponds to."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fp.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved index fingerprint to %s (%s triples)", path, fp.triples)


def load_fingerprint(path: Path = FINGERPRINT_FILE) -> Fingerprint | None:
    """The fingerprint the current index was built from, or None if never recorded."""
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


def check_drift(endpoint_url: str | None = None) -> dict[str, Any]:
    """Full check: take a live fingerprint and compare it to the indexed one.

    Returns the same shape as :func:`compare`, with ``error`` set instead of ``stale``
    when the endpoint could not be reached.
    """
    try:
        live = take_fingerprint(endpoint_url)
    except Exception as exc:
        logger.warning("Drift check could not reach the endpoint: %s", exc)
        return {
            "stale": False,
            "reason": "endpoint-unreachable",
            "error": str(exc),
            "changes": [],
            "indexed": (load_fingerprint() or Fingerprint()).to_dict(),
            "live": None,
        }
    return compare(load_fingerprint(), live)
