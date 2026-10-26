"""One SPARQL endpoint per project: its settings live in ``<data_dir>/<id>/project.json``.

The install settings list the project ids this instance serves (``settings.projects``).
Every module asks this registry for the endpoint entries instead of hard-coding a dataset.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

if TYPE_CHECKING:
    from sparql_llm.config import SparqlEndpointLinks

PROJECT_FILE = "project.json"


class ProjectError(Exception):
    """A project file is missing or invalid. The message names the file and the field to fix."""


class EntityType(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    class_iri: str = Field(alias="class")
    birth_property: str | None = None
    death_property: str | None = None


class EntityConfig(BaseModel):
    label_property: str
    types: dict[str, EntityType]


class SchemaClasses(BaseModel):
    min_instances: int = 1
    exclude: list[str] = []


class CuratorExamples(BaseModel):
    github_repo: str
    folder: str
    branch: str = "main"


class Project(BaseModel):
    """One endpoint entry, as written in project.json."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    label: str
    endpoint_url: str
    method: Literal["GET", "POST"] = "GET"
    description: str = ""
    homepage_url: str | None = None
    graphs: list[str] = []
    prefixes: dict[str, str] = {}
    dataset_description_file: str = "dataset.md"
    examples_file: str = "examples.md"
    entities: EntityConfig | None = None
    schema_classes: SchemaClasses = SchemaClasses()
    curator_examples: CuratorExamples | None = None
    prefix_repairs: dict[str, str] = {}
    ontology_from_endpoint: bool = False

    _data_dir: Path = PrivateAttr(default=Path("data"))

    @property
    def dir(self) -> Path:
        return self._data_dir / self.id

    def _in_dir(self, name: str) -> Path:
        # Relative names resolve under the project folder; absolute paths are used as given.
        path = Path(name)
        return path if path.is_absolute() else self.dir / path

    @property
    def void_path(self) -> Path:
        return self.dir / "void.ttl"

    @property
    def prefixes_path(self) -> Path:
        return self.dir / "prefixes.json"

    @property
    def entity_names_path(self) -> Path:
        return self.dir / "entity_names.json"

    @property
    def fingerprint_path(self) -> Path:
        return self.dir / "fingerprint.json"

    @property
    def synced_examples_path(self) -> Path:
        return self.dir / "examples-synced.md"

    @property
    def dataset_path(self) -> Path:
        return self._in_dir(self.dataset_description_file)

    @property
    def examples_path(self) -> Path:
        return self._in_dir(self.examples_file)

    @property
    def upload_log_path(self) -> Path:
        return self.dir / "uploads.json"

    def as_endpoint_links(self) -> SparqlEndpointLinks:
        return {
            "endpoint_url": self.endpoint_url,
            "label": self.label,
            "description": self.description,
            "homepage_url": self.homepage_url,
            "void_file": str(self.void_path),
            "examples_file": str(self.examples_path),
        }


#: A Turtle local name we can write safely: no slash, no trailing dot.
_LOCAL_NAME = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.%-]*[A-Za-z0-9_%-])?$")


def shorten(iri: str, prefixes: dict[str, str]) -> str:
    """A prefixed name when the longest matching namespace leaves a valid local name, else <iri>."""
    best = ""
    best_ns = ""
    for prefix, ns in prefixes.items():
        if iri.startswith(ns) and len(ns) > len(best_ns):
            best, best_ns = prefix, ns
    local = iri[len(best_ns) :]
    if best_ns and _LOCAL_NAME.match(local):
        return f"{best}:{local}"
    return f"<{iri}>"


def label_property_names(projects: list[Project] | None = None) -> str:
    """The configured name/label properties as prefixed names, for prompts and fallback hints."""
    names = []
    for project in get_projects() if projects is None else projects:
        if project.entities:
            name = shorten(project.entities.label_property, project.prefixes)
            if name not in names:
                names.append(name)
    return " or ".join(names) or "the label property (rdfs:label)"


def graph_pattern(graphs: list[str], pattern: str) -> str:
    """Wrap a triple pattern for the project's graphs: none = default graph, one, or several."""
    if not graphs:
        return pattern
    if len(graphs) == 1:
        return f"GRAPH <{graphs[0]}> {{ {pattern} }}"
    values = " ".join(f"<{g}>" for g in graphs)
    return f"GRAPH ?g {{ {pattern} }} VALUES ?g {{ {values} }}"


def describe_validation_error(err: ValidationError) -> list[str]:
    """Pydantic errors as plain ``"<field>: <reason>"`` lines."""
    lines = []
    for e in err.errors():
        field = ".".join(str(part) for part in e["loc"]) or "file"
        lines.append(f"{field}: {e['msg']}")
    return lines


def load_project(data_dir: Path, project_id: str) -> Project:
    path = Path(data_dir) / project_id / PROJECT_FILE
    if not path.is_file():
        raise ProjectError(f"The project file {path} for project '{project_id}' is missing.")
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProjectError(f"The project file {path} is not valid JSON: {e}") from e
    try:
        project = Project.model_validate(content)
    except ValidationError as e:
        raise ProjectError(f"The project file {path} has errors: " + "; ".join(describe_validation_error(e))) from e
    if project.id != project_id:
        raise ProjectError(
            f"The project file {path} says id '{project.id}', but it is in the folder of project '{project_id}'. "
            "The id must match the folder name."
        )
    project._data_dir = Path(data_dir)
    return project


_projects: list[Project] | None = None


def reload_projects() -> list[Project]:
    """Read every project listed in the install settings from disk again."""
    global _projects
    from sparql_llm.config import settings

    _projects = [load_project(Path(settings.data_dir), pid) for pid in settings.projects]
    return _projects


def get_projects() -> list[Project]:
    if _projects is None:
        return reload_projects()
    return _projects


def get_project(project_id: str) -> Project:
    for project in get_projects():
        if project.id == project_id:
            return project
    raise ProjectError(f"Project '{project_id}' is not served by this instance.")
