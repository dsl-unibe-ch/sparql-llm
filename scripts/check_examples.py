"""Run every example query in the RAG corpus against the live endpoint.

The example queries are the highest-leverage part of the retrieval corpus: the
assistant copies their shape. An example that does not run — or that runs but
matches nothing — teaches the model a pattern that cannot work, and nothing in
the pipeline notices, because the drift check only compares endpoint statistics
and never executes the examples.

This script closes that gap. It parses the Markdown examples file, executes each
query, and reports rows returned. Run it before committing new examples and
before pressing "Rebuild index" on the admin page.

Usage:
    uv run python scripts/check_examples.py
    uv run python scripts/check_examples.py --file data/elites-suisses-examples.md
    uv run python scripts/check_examples.py --verbose

Exit codes:
    0  every example ran and returned at least one row
    1  some examples failed, or ran but returned no rows
    2  the examples file could not be found, or the endpoint could not be reached
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sparql_llm.config import settings
from sparql_llm.utils import query_sparql

# Same conventions the Markdown loader expects, so what this validates is what
# actually gets indexed.
_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_SPARQL_BLOCK = re.compile(r"```\s*sparql\s*\n(.*?)```", re.DOTALL)
_QUESTION = re.compile(r"^(?:Question|Alternative question):\s*(.+?)\s*$", re.MULTILINE)

#: Mistakes seen repeatedly in hand-written examples. Each maps a bad fragment to
#: the reason it breaks, so the report explains rather than just failing.
#: Kept ASCII-only: this prints to a Windows console that defaults to cp1252.
KNOWN_TRAPS: dict[str, str] = {
    "sdh-so:": "undefined prefix - the endpoint rejects the whole query; use sdh-slc: (membership) or sdh-short:",
    "https://sdhss.org/ontology/social-life/": "wrong namespace - must be .../social-life-core/",
    "crm:P1_is_identified_by": "dead since the remodelling - names are a plain literal on sdh-short:P9",
    "crm:P190_has_symbolic_content": "dead since the remodelling - names are a plain literal on sdh-short:P9",
    "sdh-slc:P20": "replaced by sdh-slc:P15",
    "sdh-slc:C9": "no instances - marriages are sdh-slc:C3",
    "xsd:date(CONCAT": "membership years are plain xsd:integer - compare them as numbers",
}

#: Traps that only apply to membership queries. sdh-short:P3 and P8 are perfectly
#: valid elsewhere (study titles use them), so flagging them everywhere would cry
#: wolf; they are only wrong on an sdh-slc:C5 membership.
MEMBERSHIP_TRAPS: dict[str, str] = {
    "sdh-short:P1 ": "membership -> person is sdh-slc:P1, not sdh-short:P1",
    "sdh-short:P2 ": "membership -> group is sdh-slc:P2, not sdh-short:P2",
    "sdh-short:P3": "membership start year is sdh-short:P4, not P3",
    "sdh-short:P8": "membership end year is sdh-short:P7, not P8",
    "sdh-slc:C11": "sdh-slc:C11 is Gender - a group is crm:E74",
}


def parse_examples(path: Path) -> list[tuple[str, str, str]]:
    """Return (title, first question, query) for each example section."""
    text = path.read_text(encoding="utf-8")
    bounds = [(m.start(), m.group(1)) for m in _SECTION.finditer(text)]
    examples: list[tuple[str, str, str]] = []
    for i, (start, title) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else len(text)
        chunk = text[start:end]
        block = _SPARQL_BLOCK.search(chunk)
        if not block:
            continue
        question = _QUESTION.search(chunk)
        examples.append((title, question.group(1) if question else "", block.group(1).strip()))
    return examples


def traps_in(query: str) -> list[str]:
    traps = dict(KNOWN_TRAPS)
    if "sdh-slc:C5" in query or "sdh-slc:P2" in query:
        traps.update(MEMBERSHIP_TRAPS)
    return [f"{bad!r}: {why}" for bad, why in traps.items() if bad in query]


def main() -> int:
    # Example titles and questions carry French accents; a Windows console
    # defaults to cp1252 and would abort the run partway through.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=None, help="Markdown examples file to check")
    parser.add_argument("--endpoint", default=None, help="SPARQL endpoint URL")
    parser.add_argument("--verbose", action="store_true", help="Show the first row of each result")
    args = parser.parse_args()

    endpoint = args.endpoint or settings.endpoints[0]["endpoint_url"]
    path = Path(args.file) if args.file else Path(settings.endpoints[0]["examples_file"])
    if not path.exists():
        print(f"Examples file not found: {path}", file=sys.stderr)
        return 2

    examples = parse_examples(path)
    print(f"Examples: {path}")
    print(f"Endpoint: {endpoint}")
    print(f"{len(examples)} example queries\n")

    failed: list[str] = []
    empty: list[str] = []
    for title, question, query in examples:
        for trap in traps_in(query):
            print(f"  [ !TRAP  ]  {title}\n              {trap}")
        try:
            rows = query_sparql(query, endpoint, timeout=120)["results"]["bindings"]
        except Exception as exc:
            print(f"  [ FAILED  ]  {title}\n              {str(exc)[:160]}")
            failed.append(title)
            continue
        if rows:
            print(f"  [ OK {len(rows):>4} ]  {title}")
            if args.verbose:
                first = rows[0]
                print("              " + " | ".join(f"{k}={v['value'][:60]}" for k, v in first.items()))
        else:
            print(f"  [ 0 ROWS  ]  {title}")
            print(f"              runs, but matches nothing — question: {question!r}")
            empty.append(title)

    print()
    if failed:
        print(f"{len(failed)} query/queries could not be executed: {', '.join(failed)}")
    if empty:
        print(f"{len(empty)} query/queries returned no rows: {', '.join(empty)}")
    if not failed and not empty:
        print("All examples run and return data.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
