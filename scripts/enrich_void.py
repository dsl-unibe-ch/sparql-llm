"""Generate a project's VoID description from its endpoint (developer wrapper).

The admin rebuild does this for every project; this script runs the same step on its own,
writing data/<id>/void.ttl. See sparql_llm.indexing.void_gen.

Run from the sparql-llm/ directory:
    uv run python scripts/enrich_void.py [--project <id>]
"""

from __future__ import annotations

import argparse

from sparql_llm.indexing.void_gen import generate_void
from sparql_llm.project import get_project, get_projects


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=None, help="Project id (defaults to the first project)")
    args = parser.parse_args()

    project = get_project(args.project) if args.project else get_projects()[0]
    print(f"Querying {project.endpoint_url} …")
    report = generate_void(project)
    print(f"  Wrote {project.void_path}: {report}")


if __name__ == "__main__":
    main()
