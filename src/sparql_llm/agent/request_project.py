"""The one dataset a chat request is answered from: the user's selection, checked server-side.

The model never chooses it. ``chat`` resolves it once and puts its id into the run's
configuration; every node then reads it back with ``project_of``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sparql_llm.config import Configuration
from sparql_llm.datasets import datasets_for
from sparql_llm.project import Project, get_project, get_projects

#: With auth off there is no user: whoever runs the instance manages it.
_AUTH_OFF = SimpleNamespace(id=None, is_superuser=False, is_curator=True)


class ProjectRefused(Exception):
    """The request names a dataset this user may not ask. Plain message."""


def resolve_request_project(requested: str, user: Any) -> Project:
    """The dataset to answer from; an empty request means the first one this user may ask."""
    allowed = datasets_for(_AUTH_OFF if user is None else user)
    if not requested:
        if not allowed:
            raise ProjectRefused("No dataset is available yet.")
        return allowed[0]
    for project in allowed:
        if project.id == requested:
            return project
    raise ProjectRefused(f"The dataset '{requested}' is not available.")


def project_of(configuration: Configuration) -> Project:
    """The dataset this run answers from, as ``chat`` put it into the configuration."""
    if configuration.project:
        return get_project(configuration.project)
    return get_projects()[0]
