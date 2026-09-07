"""Pull the curators' example queries into our corpus, testing them on the way in.

The curators own the examples: they know the data, and asking them to work in this
repository would put the material behind a fork-and-pull-request loop they have no
reason to learn. So their file stays in ``lod4hss-projects/elites-suisses`` and a
rebuild fetches it.

The one thing that must not be automated away is the check. Example queries are the
highest-leverage part of the retrieval corpus — the assistant copies their shape — and
a broken one is indexed in silence: nothing executes examples, so a query that returns
nothing still teaches a pattern that cannot work. ``sdh-so:`` survived months that way.
Every query fetched here is therefore executed against the endpoint first, and only the
ones that come back with data are written into the corpus. The rest are reported.

Their content is spliced between markers in our examples file, so the examples we curate
ourselves survive, and every sync is a readable diff.

Usage:
    uv run python -m sparql_llm.indexing.sync_examples
    uv run python -m sparql_llm.indexing.sync_examples --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from sparql_llm.config import settings
from sparql_llm.loaders.sparql_examples_md_loader import PREFIX_TYPO_FIXES
from sparql_llm.utils import logger, query_sparql

#: Raw URLs of the curators' example files. Their repository is public, so no token is
#: needed; ``main`` rather than a pinned sha because the point is to pick up their edits.
CURATOR_SOURCES: tuple[str, ...] = (
    "https://raw.githubusercontent.com/lod4hss-projects/elites-suisses/main/"
    "llm_documentation/SPARQL_queries_examples/query_examples.md",
    "https://raw.githubusercontent.com/lod4hss-projects/elites-suisses/main/"
    "llm_documentation/SPARQL_queries_examples/query_obtaining_study_title.md",
)

#: The synced material is written here rather than into the curated file. That file is
#: git-tracked, and rewriting it during a rebuild would leave the server's working tree
#: dirty for the next `git pull` to conflict with. Nothing is lost by keeping it out of
#: our history: the curators' own repository is the audit trail. `data/*` is gitignored
#: with a short whitelist, so this path is ignored automatically.
SYNCED_SUFFIX = "-synced"

_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_SPARQL_BLOCK = re.compile(r"```\s*sparql\s*\n(.*?)```", re.DOTALL)
_QUESTION = re.compile(r"^(?:Question|Alternative question):\s*(.+?)\s*$", re.MULTILINE)
#: Their layout: a bare "Question:" line followed by a fenced block, no heading.
_FLAT_ENTRY = re.compile(r"^Question:.*?```\s*sparql\s*\n.*?```", re.DOTALL | re.MULTILINE)


@dataclass
class Example:
    """One question/query pair on its way from their repository into our corpus."""

    title: str
    questions: list[str]
    query: str
    source: str = ""
    #: Set by validation: row count, or None when the query could not be executed.
    rows: int | None = None
    error: str = ""
    notes: list[str] = field(default_factory=list)


def _title_from_question(question: str) -> str:
    """Turn 'What are the disciplines of the study titles?' into a short title."""
    text = question.strip().rstrip("?").strip()
    for lead in ("What are the ", "What is the ", "Who are the ", "Who is the ", "Which "):
        if text.lower().startswith(lead.lower()):
            text = text[len(lead) :]
            break
    text = text[:1].upper() + text[1:]
    return text[:70].strip()


def parse_examples(text: str, source: str = "") -> list[Example]:
    """Read either layout: our '## Example N: title' sections, or their flat list.

    Their file has no headings at all, so splitting on sections would collapse it into
    one blob carrying only the first query. Fall back to splitting on each
    'Question: … ```sparql …```' run.
    """
    examples: list[Example] = []
    bounds = [(m.start(), m.group(1)) for m in _SECTION.finditer(text)]

    if bounds:
        for i, (start, heading) in enumerate(bounds):
            end = bounds[i + 1][0] if i + 1 < len(bounds) else len(text)
            chunk = text[start:end]
            block = _SPARQL_BLOCK.search(chunk)
            if not block:
                continue
            title = re.sub(r"^Example\s+\d+\s*(\*\(.*?\)\*)?\s*:\s*", "", heading).strip()
            examples.append(
                Example(
                    title=title or _title_from_question(" ".join(_QUESTION.findall(chunk))),
                    questions=_QUESTION.findall(chunk),
                    query=block.group(1).strip(),
                    source=source,
                )
            )
        return examples

    for match in _FLAT_ENTRY.finditer(text):
        chunk = match.group(0)
        block = _SPARQL_BLOCK.search(chunk)
        questions = _QUESTION.findall(chunk)
        if not block or not questions:
            continue
        examples.append(
            Example(
                title=_title_from_question(questions[0]),
                questions=questions,
                query=block.group(1).strip(),
                source=source,
            )
        )
    return examples


def normalise_query(query: str) -> str:
    """Repair the prefix mistakes the curators' file has carried for months.

    Reuses the same table the Markdown loader applies at index time, so a query that
    survives this is the query that gets indexed.
    """
    for wrong, right in PREFIX_TYPO_FIXES.items():
        query = query.replace(wrong, right)
    return query


def validate(examples: list[Example], endpoint: str) -> tuple[list[Example], list[Example]]:
    """Execute each query. Returns (accepted, rejected).

    A query that errors is rejected — it would poison the corpus. A query that runs but
    matches nothing is *also* rejected: it is indistinguishable to the model from one
    that works, and teaches a shape the data cannot answer.
    """
    accepted: list[Example] = []
    rejected: list[Example] = []
    for example in examples:
        try:
            rows = query_sparql(example.query, endpoint, timeout=120)["results"]["bindings"]
        except Exception as exc:
            example.error = f"{type(exc).__name__}: {exc}"[:300]
            rejected.append(example)
            continue
        example.rows = len(rows)
        if rows:
            accepted.append(example)
        else:
            example.error = "runs, but returns no rows against the current data"
            rejected.append(example)
    return accepted, rejected


def render_examples(examples: list[Example]) -> str:
    """Write the accepted examples in the layout our Markdown loader parses."""
    parts: list[str] = []
    for i, example in enumerate(examples, start=1):
        lines = [f"## Example S{i}: {example.title}", ""]
        for j, question in enumerate(example.questions):
            label = "Question" if j == 0 else "Alternative question"
            lines.append(f"{label}: {question}")
        if example.source:
            note = f"Comment: Synced from the curators' `{Path(example.source).name}`"
            if example.rows is not None:
                note += f"; verified against the endpoint ({example.rows} row(s) returned)"
            note += "."
            lines.append(note)
        lines += ["", "```sparql", example.query, "```", ""]
        parts.append("\n".join(lines))
    return "\n".join(parts)


def synced_examples_path(curated_file: str | Path) -> Path:
    """Where the curators' synced examples are written, beside the curated file.

    A distinct file, never the curated one: a rebuild must not modify anything git
    tracks on the server.
    """
    curated = Path(curated_file)
    return curated.with_name(f"{curated.stem}{SYNCED_SUFFIX}{curated.suffix}")


def _question_key(question: str) -> str:
    """Comparable form of a question: case, spacing and trailing punctuation ignored."""
    return re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()


def drop_already_curated(
    examples: list[Example], curated: str
) -> tuple[list[Example], list[Example]]:
    """Skip curator examples we already answer with a hand-written one.

    Their study-title queries are the source our Examples 13-18 were copied from, so a
    straight sync would index each question twice and let the duplicates crowd better
    matches out of the retrieved set. Ours win because they carry the endpoint-verified
    comments; when the curators improve one, delete ours and the sync picks theirs up.

    Only the hand-curated file is consulted. The synced file is separate, so a re-sync
    cannot mistake its own previous output for something we curate.
    """
    ours = {_question_key(q) for q in _QUESTION.findall(curated)}
    kept: list[Example] = []
    skipped: list[Example] = []
    for example in examples:
        if any(_question_key(q) in ours for q in example.questions):
            example.notes.append("already curated in our examples file")
            skipped.append(example)
        else:
            kept.append(example)
    return kept, skipped


def fetch(url: str, client: httpx.Client | None = None) -> str:
    owned = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=30)
    try:
        response = client.get(url)
        response.raise_for_status()
        return response.text
    finally:
        if owned:
            client.close()


def sync_curator_examples(
    sources: tuple[str, ...] = CURATOR_SOURCES,
    target: str | Path | None = None,
    endpoint: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch, validate and splice the curators' examples. Never raises.

    A rebuild calls this, so a failure here must degrade rather than abort: if their
    repository or the endpoint is unreachable, the corpus keeps whatever was synced last
    time and the report says so.
    """
    endpoint = endpoint or settings.endpoints[0]["endpoint_url"]
    curated_path = Path(target) if target else Path(settings.endpoints[0]["examples_file"])
    target_path = synced_examples_path(curated_path)
    report: dict[str, Any] = {
        "accepted": 0,
        "rejected": [],
        "skipped_as_curated": [],
        "sources": [],
        "written": False,
        "error": "",
    }

    parsed: list[Example] = []
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        for url in sources:
            try:
                text = fetch(url, client)
            except Exception as exc:
                report["error"] = f"could not fetch {url.rsplit('/', 1)[-1]}: {exc}"
                logger.warning("Example sync: %s", report["error"])
                return report
            found = parse_examples(text, source=url)
            report["sources"].append({"url": url, "found": len(found)})
            parsed.extend(found)

    for example in parsed:
        example.query = normalise_query(example.query)

    curated = curated_path.read_text(encoding="utf-8") if curated_path.exists() else ""
    parsed, already_ours = drop_already_curated(parsed, curated)
    report["skipped_as_curated"] = [e.title for e in already_ours]

    accepted, rejected = validate(parsed, endpoint)
    report["accepted"] = len(accepted)
    report["rejected"] = [
        {"title": e.title, "source": Path(e.source).name, "reason": e.error} for e in rejected
    ]

    # An empty accepted set is only alarming if their whole offering failed. Once we
    # already curate everything good they publish — today's steady state — zero accepted
    # alongside a rejected query is a warning about that one query, not a failed sync.
    if not accepted and rejected and not already_ours:
        report["error"] = "no curator example passed validation"
        logger.warning("Example sync: %s", report["error"])

    if dry_run:
        return report

    header = (
        "<!-- Generated by sparql_llm.indexing.sync_examples — do not edit.\n"
        "     Source of truth: lod4hss-projects/elites-suisses, "
        "llm_documentation/SPARQL_queries_examples/\n"
        "     Every query below was executed against the endpoint before being written. -->\n\n"
        "# Elites Suisses — example queries synced from the curators\n\n"
    )
    # Written even when empty: the file mirrors what they currently publish, so an
    # example they delete or break disappears from our corpus on the next rebuild
    # instead of lingering because nothing overwrote it.
    try:
        target_path.write_text(header + render_examples(accepted), encoding="utf-8")
        report["written"] = True
        report["path"] = str(target_path)
    except Exception as exc:
        report["error"] = f"could not write {target_path}: {exc}"
        logger.warning("Example sync: %s", report["error"])
    return report


def format_report(report: dict[str, Any]) -> str:
    """One-line summary suitable for the rebuild status shown on the admin page."""
    bits = [f"{report['accepted']} curator example(s) accepted"]
    if report.get("skipped_as_curated"):
        bits.append(f"{len(report['skipped_as_curated'])} already curated here")
    if report["rejected"]:
        titles = ", ".join(r["title"] for r in report["rejected"])
        bits.append(f"⚠ {len(report['rejected'])} rejected and NOT indexed: {titles}")
    if report["error"]:
        bits.append(f"⚠ {report['error']}")
    return "; ".join(bits)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Sync example queries from the curators' repo")
    parser.add_argument("--dry-run", action="store_true", help="Validate but do not write")
    args = parser.parse_args()

    report = sync_curator_examples(dry_run=args.dry_run)
    for source in report["sources"]:
        print(f"  fetched {source['found']:>2} example(s) from {source['url'].rsplit('/', 1)[-1]}")
    print(f"\n  accepted: {report['accepted']}")
    for title in report.get("skipped_as_curated", []):
        print(f"  skipped   {title}  (we already curate this question)")
    for rejected in report["rejected"]:
        print(f"  REJECTED  {rejected['title']}  ({rejected['source']})")
        print(f"            {rejected['reason']}")
    if report["error"]:
        print(f"\n  ERROR: {report['error']}")
        return 2
    if args.dry_run:
        print("\n  (dry run — nothing written)")
    elif report["written"]:
        print("\n  Examples file updated. Rebuild the index for it to take effect.")
    return 1 if report["rejected"] else 0


if __name__ == "__main__":
    sys.exit(main())
