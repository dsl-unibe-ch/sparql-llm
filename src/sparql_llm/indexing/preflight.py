"""Check a project's configuration against its endpoint before anything is rebuilt.

Each check is a named step (stable names, for the onboarding wizard to run one at a time)
and reports one plain-language line that says which settings field to fix. An error stops
a rebuild before the live index is touched; a warning is shown but does not stop it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import httpx

from sparql_llm.indexing.sync_examples import parse_examples
from sparql_llm.project import Project, graph_pattern, read_projects
from sparql_llm.utils import query_sparql

STEPS = ("endpoint", "graphs", "entities", "files", "examples")


@dataclass
class CheckResult:
    step: str
    status: Literal["ok", "warning", "error"]
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _short_reason(exc: Exception) -> str:
    """Why an endpoint call failed, in words a curator can act on."""
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return "it did not answer in time"
    if isinstance(exc, json.JSONDecodeError):
        return "it answered with something other than SPARQL results, e.g. a web page"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"it answered with HTTP error {exc.response.status_code}"
    if isinstance(exc, httpx.ConnectError):
        return "it could not be reached"
    first_line = str(exc).splitlines()[0][:150] if str(exc) else ""
    return first_line or "the request failed"


def _ask(project: Project, query: str, fetch: Callable[..., Any]) -> bool:
    result = fetch(query, project.endpoint_url, post=project.method == "POST", timeout=60)
    bindings = result.get("results", {}).get("bindings", [])
    if bindings and "ask-variable" in bindings[0]:
        return bindings[0]["ask-variable"]["value"] == "true"
    return bool(bindings)


def _check_endpoint(project: Project, fetch: Callable[..., Any]) -> CheckResult:
    url = project.endpoint_url
    try:
        has_data = _ask(project, "ASK { ?s ?p ?o }", fetch)
    except Exception as exc:
        return CheckResult(
            "endpoint",
            "error",
            f"{project.label}: the endpoint {url} did not answer a SPARQL query ({_short_reason(exc)}). "
            f"Check endpoint_url and method ({project.method}) in project.json.",
        )
    if not has_data:
        return CheckResult("endpoint", "error", f"{project.label}: the endpoint {url} answers but holds no data.")
    return CheckResult("endpoint", "ok", f"{project.label}: the endpoint {url} answers ({project.method}).")


def _check_graphs(project: Project, fetch: Callable[..., Any]) -> CheckResult:
    if not project.graphs:
        return CheckResult("graphs", "ok", f"{project.label}: no graphs listed, the default graph is used.")
    empty = [g for g in project.graphs if not _ask(project, f"ASK {{ GRAPH <{g}> {{ ?s ?p ?o }} }}", fetch)]
    if empty:
        names = ", ".join(f"<{g}>" for g in empty)
        return CheckResult(
            "graphs",
            "error",
            f"{project.label}: the graph {names} listed in the graphs field is empty or does not exist. "
            "Fix the graphs field in project.json, or leave it empty to use the default graph.",
        )
    return CheckResult("graphs", "ok", f"{project.label}: {len(project.graphs)} graph(s) hold data.")


def _check_entities(project: Project, fetch: Callable[..., Any]) -> CheckResult:
    if project.entities is None:
        return CheckResult(
            "entities", "warning", f"{project.label}: no entities block, so names in questions will not be looked up."
        )
    missing = []
    for type_name, entity_type in project.entities.types.items():
        pattern = f"?s a <{entity_type.class_iri}> ; <{project.entities.label_property}> ?l ."
        if not _ask(project, f"ASK {{ {graph_pattern(project.graphs, pattern)} }}", fetch):
            missing.append(type_name)
    if missing:
        fields = ", ".join(f"entities.types.{name}" for name in missing)
        return CheckResult(
            "entities",
            "error",
            f"{project.label}: no instance of {fields} has a label <{project.entities.label_property}>. "
            "Check the class IRI and entities.label_property in project.json.",
        )
    return CheckResult(
        "entities", "ok", f"{project.label}: every entity type has labelled instances ({len(project.entities.types)})."
    )


def _check_files(project: Project) -> tuple[CheckResult, list]:
    problems = []
    if not project.dataset_path.is_file():
        problems.append(f"the dataset description {project.dataset_path.name} is missing")
    examples = []
    if not project.examples_path.is_file():
        problems.append(f"the examples file {project.examples_path.name} is missing")
    else:
        examples = parse_examples(project.examples_path.read_text(encoding="utf-8"))
        if not examples:
            problems.append(
                f"the examples file {project.examples_path.name} has no example "
                "(each needs a '## ' heading, a 'Question:' line and a ```sparql block)"
            )
    if problems:
        return CheckResult("files", "error", f"{project.label}: " + "; ".join(problems) + "."), examples
    return CheckResult("files", "ok", f"{project.label}: dataset.md and {len(examples)} example(s) found."), examples


def _check_examples(project: Project, examples: list, fetch: Callable[..., Any]) -> CheckResult:
    failing = []
    for example in examples:
        try:
            rows = fetch(example.query, project.endpoint_url, post=project.method == "POST", timeout=120)
            if not rows.get("results", {}).get("bindings"):
                failing.append(example.title)
        except Exception:
            failing.append(example.title)
    passed = len(examples) - len(failing)
    details = {"ok": passed, "total": len(examples)}
    if failing:
        return CheckResult(
            "examples",
            "warning",
            f"{project.label}: {passed}/{len(examples)} examples return rows. "
            f"These fail or return nothing: {'; '.join(failing)}.",
            details,
        )
    return CheckResult("examples", "ok", f"{project.label}: {passed}/{len(examples)} examples OK.", details)


def run_preflight(project: Project, fetch: Callable[..., Any] = query_sparql) -> list[CheckResult]:
    """Run every check for one project. Stops after the endpoint check if the endpoint is down."""
    endpoint = _check_endpoint(project, fetch)
    if endpoint.status == "error":
        return [endpoint]
    results = [endpoint]
    for check in (_check_graphs, _check_entities):
        try:
            results.append(check(project, fetch))
        except Exception as exc:
            step = "graphs" if check is _check_graphs else "entities"
            results.append(
                CheckResult(step, "error", f"{project.label}: the {step} check failed ({_short_reason(exc)}).")
            )
    files, examples = _check_files(project)
    results.append(files)
    if files.status != "error":
        results.append(_check_examples(project, examples, fetch))
    return results


def run_checks() -> dict[str, list[dict[str, Any]]]:
    """Preflight every project of this instance as its files are on disk now (Check button)."""
    projects, problems = read_projects(candidates=True)
    results = {pid: [asdict(CheckResult("project", "error", message))] for pid, message in problems.items()}
    for project in projects:
        results[project.id] = [asdict(r) for r in run_preflight(project)]
    return results
