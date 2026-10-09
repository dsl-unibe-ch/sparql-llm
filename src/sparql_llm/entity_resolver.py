"""Match entity names from a question against the labels in data/entity_names.json.

Lexical fuzzy matching (rapidfuzz), not embeddings: names need typo, accent and word-order
tolerance, which BM25 and dense vectors handle poorly. Calibrated on the 65,800 live labels:
token_set finds subsets ("Ogi" → "Ogi, Adolf"; "Ludwig Forrer" → "Forrer-Dändliker, Ludwig")
and token_sort ranks the exact form first.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from sparql_llm.indexing.entity_names import ENTITY_NAMES_FILE
from sparql_llm.utils import logger

SCORE_CUTOFF = 86
_NOT_A_NAME = re.compile(r"^[\d\s./\-:]*$")


def normalise(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())


@dataclass
class Candidate:
    uri: str
    label: str
    type: str
    score: float
    birth: str = ""
    death: str = ""

    @property
    def years(self) -> str:
        if self.birth and self.death:
            return f"{self.birth}–{self.death}"
        if self.birth:
            return f"b. {self.birth}"
        return f"d. {self.death}" if self.death else ""


@dataclass
class Resolution:
    name: str
    candidates: list[Candidate]
    total: int


class EntityResolver:
    """Loads the entity file lazily and reloads it when it changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._rows: list[dict[str, str]] = []
        self._keys: list[str] = []
        self._warned = False

    def _load(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._rows, self._keys, self._mtime = [], [], None
            if not self._warned:
                logger.info("Entity resolution unavailable: %s not built yet", self.path)
                self._warned = True
            return False
        if mtime == self._mtime:
            return bool(self._rows)
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))["entities"]
        except Exception as exc:
            logger.warning("Entity resolution unavailable: cannot read %s (%s)", self.path, exc)
            self._rows, self._keys = [], []
        else:
            self._rows = rows
            self._keys = [normalise(r["label"]) for r in rows]
        self._mtime = mtime
        return bool(self._rows)

    @property
    def available(self) -> bool:
        return self._load()

    def resolve(self, name: str, limit: int = 8) -> Resolution | None:
        """Candidates for one name; None if the name is not a name or no entity file exists."""
        query = normalise(name)
        if not query or _NOT_A_NAME.match(name.strip()) or not self._load():
            return None
        hits = process.extract(query, self._keys, scorer=fuzz.token_set_ratio, limit=None, score_cutoff=SCORE_CUTOFF)
        if any(score == 100 for _, score, _ in hits):
            hits = [h for h in hits if h[1] == 100]
        ranked = sorted(hits, key=lambda h: (-fuzz.token_sort_ratio(query, h[0]), -h[1]))
        candidates = []
        for _, score, idx in ranked[:limit]:
            row = self._rows[idx]
            candidates.append(
                Candidate(row["uri"], row["label"], row["type"], round(score), row.get("birth", ""), row.get("death", ""))
            )
        return Resolution(name, candidates, len(hits))


_resolver: EntityResolver | None = None


def get_resolver() -> EntityResolver:
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver(ENTITY_NAMES_FILE)
    return _resolver
