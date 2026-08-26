"""Check whether the curators' OWL profiles have actually been loaded into the triplestore.

The assistant never reads the profile files. It reads the triplestore, which serves the
same OWL as ordinary triples — but only once somebody exports the profile from OntoME,
commits it, *and* loads it into the store. That last step is manual, and a profile that
stops short of it is invisible to the assistant while looking perfectly up to date in git.

Crucially, this failure produces **no data change at all**, so the drift check on the admin
page cannot see it: that one compares instance counts, and an unloaded profile has none.
This script covers the other half — it reads the class and property IRIs declared in the
profiles on disk and asks the endpoint whether it knows each one.

Usage:
    uv run python scripts/check_profile_sync.py
    uv run python scripts/check_profile_sync.py --profiles /path/to/llm_ontology

Exit codes:
    0  every declared term is in the triplestore
    1  some terms are missing (they were committed but never loaded)
    2  the profiles could not be found, or the endpoint could not be reached
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter"

#: The curator repo is normally checked out beside this one.
DEFAULT_PROFILE_DIR = Path(__file__).parent.parent.parent / "elites-suisses" / "llm_ontology"

#: Terms are declared as rdf:about="..." on owl:Class / owl:ObjectProperty elements.
#: Only SDHSS and CIDOC CRM terms ending in C<n>/P<n> are ours to check.
_TERM_RE = re.compile(
    r'rdf:about="(https://sdhss\.org/ontology/[^"]+|http://www\.cidoc-crm\.org/cidoc-crm/[^"]+)"'
)
_IS_TERM = re.compile(r"/(?:C|P)\d+$")


def declared_terms(profile_dir: Path) -> dict[str, set[str]]:
    """Map each profile file to the set of term IRIs it declares."""
    out: dict[str, set[str]] = {}
    for path in sorted(profile_dir.glob("*.rdf")):
        text = path.read_text(encoding="utf-8", errors="replace")
        terms = {m for m in _TERM_RE.findall(text) if _IS_TERM.search(m)}
        if terms:
            out[path.name] = terms
    return out


def labelled_in_store(iris: set[str], endpoint: str) -> set[str]:
    """Return the subset of ``iris`` the endpoint has an rdfs:label for.

    Asked as a single VALUES query rather than one request per term — a few hundred IRIs
    would otherwise mean a few hundred round trips.
    """
    if not iris:
        return set()
    values = " ".join(f"<{i}>" for i in sorted(iris))
    query = (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
        f"SELECT ?t WHERE {{ VALUES ?t {{ {values} }} ?t rdfs:label ?l }}"
    )
    url = endpoint + "?query=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        rows = json.load(resp)["results"]["bindings"]
    return {r["t"]["value"] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILE_DIR,
                        help=f"directory of OWL profile .rdf files (default: {DEFAULT_PROFILE_DIR})")
    parser.add_argument("--endpoint", default=ENDPOINT, help="SPARQL endpoint to check against")
    args = parser.parse_args()

    if not args.profiles.is_dir():
        print(f"Profile directory not found: {args.profiles}", file=sys.stderr)
        print("Pass --profiles with the path to the elites-suisses repo's llm_ontology folder.", file=sys.stderr)
        return 2

    by_file = declared_terms(args.profiles)
    if not by_file:
        print(f"No OWL profiles with declared terms found in {args.profiles}", file=sys.stderr)
        return 2

    every_term = set().union(*by_file.values())
    try:
        present = labelled_in_store(every_term, args.endpoint)
    except Exception as exc:
        print(f"Could not reach the endpoint: {exc}", file=sys.stderr)
        return 2

    missing_total = every_term - present

    print(f"Profiles: {args.profiles}")
    print(f"Endpoint: {args.endpoint}")
    print(f"{len(by_file)} profile files declaring {len(every_term)} distinct terms\n")

    for name, terms in by_file.items():
        gone = sorted(terms - present)
        status = "OK" if not gone else f"{len(gone)} MISSING"
        print(f"  [{status:>10}]  {name}  ({len(terms)} terms)")
        for iri in gone:
            print(f"                 not in store: {iri}")

    print()
    if missing_total:
        print(f"{len(missing_total)} term(s) are declared in the profiles but absent from the triplestore.")
        print("They have been committed but not loaded, so the assistant cannot see them.")
        print("Ask the curators to load the current profiles into the store.")
        return 1

    print("In sync — every term declared in the profiles is present in the triplestore.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
