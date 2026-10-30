import json
import logging
import re
import time
from typing import Any

import curies
import httpx
import rdflib

from sparql_llm.outbound import guarded_client
from sparql_llm.project import Project

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """Remove <think>…</think> reasoning blocks emitted by some LLMs."""
    return THINK_BLOCK_RE.sub("", text)


_THINK_CONTENT_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def extract_think_blocks(text: str) -> str:
    """Return the concatenated contents of any <think>…</think> blocks.

    The inverse of :func:`strip_think_blocks`: it returns the reasoning instead
    of the answer. Used to surface a model's chain-of-thought as a separate
    "Thought process" step. Returns "" when there is no reasoning block.
    """
    parts = [m.strip() for m in _THINK_CONTENT_RE.findall(text) if m.strip()]
    return "\n\n".join(parts)


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def strip_think_stream(buffer: str) -> str:
    """Return the portion of a *streamed* buffer that is safe to display.

    Reasoning models (e.g. minimax-m2.7 on GPUStack) interleave their
    chain-of-thought as ``<think>…</think>`` inside the streamed ``content``, and a
    tag can be split across chunks. So this works on the *whole accumulated buffer*
    and returns only the text that is safe to show:

    - complete ``<think>…</think>`` blocks are removed,
    - everything after an unclosed ``<think>`` is held back (still streaming),
    - a trailing partial opening tag (``"<"``, ``"<th"``, ``"<thin"``…) is held
      back so we never emit characters that may turn out to start a block.

    Call it on the growing buffer each chunk and emit only the newly revealed
    suffix (track an "already emitted length" cursor). The returned visible
    prefix is monotonic, so the cursor never has to walk backwards.
    """
    # Drop all complete reasoning blocks.
    s = THINK_BLOCK_RE.sub("", buffer)
    # Hold back an unclosed reasoning block.
    lower = s.lower()
    open_idx = lower.rfind(_THINK_OPEN)
    if open_idx != -1 and _THINK_CLOSE not in lower[open_idx:]:
        s = s[:open_idx]
    # Hold back a trailing partial opening tag like "<", "<th", "<thin"…
    for k in range(min(len(s), len(_THINK_OPEN) - 1), 0, -1):
        if s[-k:].lower() == _THINK_OPEN[:k]:
            return s[:-k]
    return s


_FENCE = "```"
_SPARQL_FENCE = "```sparql"


def strip_sparql_stream(buffer: str) -> str:
    """Return the portion of a *streamed* buffer with SPARQL codeblocks removed.

    In "natural language only" mode the visible answer contains no SPARQL; the
    query still reaches the user through the "Thought process" step and the
    "open in editor" link built from the agent's structured output.

    Like :func:`strip_think_stream` this operates on the *whole accumulated
    buffer* and returns only the text that is safe to show:

    - fenced blocks whose info string starts with ``sparql`` (any case) are
      dropped whole, leaving no blank line behind,
    - fences with any other info string (```` ``` ````, ```` ```json ````…) and
      inline code spans pass through untouched,
    - a still-open SPARQL fence holds back everything from the fence onward,
    - a trailing partial fence marker (`` ` ``, ``` `` ```, ```` ``` ````,
      ```` ```spa ````) is held back until the info string is known, so an
      opening fence is never emitted before we can classify it.

    Call it on the growing buffer each chunk and emit only the newly revealed
    suffix. The returned visible prefix is monotonic, so the cursor in
    ``stream_response`` never has to walk backwards.
    """
    lines = buffer.split("\n")
    # The final element is the line still being streamed ("" when the buffer
    # ends on a newline); it is the only one whose classification can change.
    partial = lines.pop()

    visible: list[str] = []
    in_fence = False
    fence_is_sparql = False
    for line in lines:
        marker = line.lstrip()
        if marker.startswith(_FENCE):
            if in_fence:
                closing_sparql = fence_is_sparql
                in_fence = False
                fence_is_sparql = False
                if closing_sparql:
                    continue
            else:
                in_fence = True
                fence_is_sparql = marker[len(_FENCE) :].strip().lower().startswith("sparql")
                if fence_is_sparql:
                    continue
        elif in_fence and fence_is_sparql:
            continue
        visible.append(line + "\n")

    if in_fence:
        # Inside a SPARQL block the partial line is reasoning-in-progress; inside
        # any other block it is ordinary content and streams as usual.
        if not fence_is_sparql:
            visible.append(partial)
    elif not _may_open_sparql_fence(partial):
        visible.append(partial)
    return "".join(visible)


def _may_open_sparql_fence(partial: str) -> bool:
    """True when a partial line could still turn out to open a SPARQL block.

    Holds back ``` ``` ``` and its prefixes until enough of the info string has
    arrived to classify the fence. ``` ```json ``` is released immediately, and a
    line that merely *contains* backticks (an inline ``sdh-slc:C3`` CURIE) is
    never held, since only a line starting with the fence can open a block.
    """
    marker = partial.lstrip().lower()
    if not marker:
        return False
    return marker.startswith(_SPARQL_FENCE) or _SPARQL_FENCE.startswith(marker)


# Disable logger in your code with logging.getLogger("sparql_llm").setLevel(logging.WARNING)
logger = logging.getLogger("sparql_llm")
logger.setLevel(logging.INFO)

logging.getLogger("httpx").setLevel(logging.WARNING)


# Prefixes utilities

GET_PREFIXES_QUERY = """PREFIX sh: <http://www.w3.org/ns/shacl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?prefix ?namespace
WHERE {
    [] sh:namespace ?namespace ;
        sh:prefix ?prefix .
} ORDER BY ?prefix"""

def get_prefixes_for_endpoint(
    endpoint_url: str, examples_file: str | None = None, prefixes_map: dict[str, str] | None = None
) -> dict[str, str]:
    """Return a dictionary of prefixes for the given endpoint."""
    if prefixes_map is None:
        prefixes_map = {}
    try:
        for row in query_sparql(
            GET_PREFIXES_QUERY, endpoint_url, use_file=examples_file, check_service_desc=True, timeout=10
        )["results"]["bindings"]:
            if row["namespace"]["value"] not in prefixes_map.values():
                prefixes_map[row["prefix"]["value"]] = row["namespace"]["value"]
    except Exception as e:
        logger.warning(f"Error retrieving prefixes for {endpoint_url}: {e}")
    return prefixes_map


def merge_prefixes(discovered: dict[str, str], declared: dict[str, str]) -> dict[str, str]:
    """Merge discovered prefixes into a project's declared ones; the declared entries win.

    A discovered name for a declared namespace is dropped (no ``schema1``), and so is a
    discovered namespace under a declared name (no ``schema`` -> https://schema.org/ from
    rdflib's defaults when the project says http).
    """
    merged = dict(declared)
    for prefix, namespace in discovered.items():
        if prefix in merged or namespace in merged.values():
            continue
        merged[prefix] = namespace
    return merged


def _merge_prefixes_from_turtle(turtle_file: str, prefixes_map: dict[str, str]) -> None:
    """Extract @prefix declarations from a Turtle file and merge into prefixes_map."""
    try:
        g = rdflib.Graph()
        g.parse(turtle_file, format="turtle")
        for prefix, ns in g.namespaces():
            if prefix and str(ns) not in prefixes_map.values():
                prefixes_map[str(prefix)] = str(ns)
    except Exception as e:
        logger.debug(f"Could not extract prefixes from {turtle_file}: {e}")


def get_prefix_converter(prefix_dict: dict[str, str]) -> curies.Converter:
    """Return a prefix converter."""
    return curies.load_prefix_map(prefix_dict)


def compress_list(converter: curies.Converter, uris: list[str]) -> list[str]:
    """Helper function to compress a list of URIs using a curies.Converter."""
    return [converter.compress(uri, passthrough=True) for uri in uris]


# VoID description utilities

GET_VOID_DESC = """PREFIX up: <http://purl.uniprot.org/core/>
PREFIX void: <http://rdfs.org/ns/void#>
PREFIX void-ext: <http://ldf.fi/void-ext#>
SELECT DISTINCT ?subjectClass ?prop ?objectClass ?objectDatatype
WHERE {
    {
        ?cp void:class ?subjectClass ;
            void:propertyPartition ?pp .
        ?pp void:property ?prop .
        OPTIONAL {
            {
                ?pp  void:classPartition [ void:class ?objectClass ] .
            } UNION {
                ?pp void-ext:datatypePartition [ void-ext:datatype ?objectDatatype ] .
            }
        }
    } UNION {
        ?linkset void:subjectsTarget [ void:class ?subjectClass ] ;
            void:linkPredicate ?prop ;
            void:objectsTarget [ void:class ?objectClass ] .
    }
}"""

SchemaDict = dict[str, dict[str, list[str]]]
"""A dictionary to store the classes schema of an endpoint: dict[subject_cls][predicate] = list[object_cls/datatype]"""
EndpointsSchemaDict = dict[str, SchemaDict]
"""A dictionary to store the classes schema of multiple endpoints: dict[endpoint_url][subject_cls][predicate] = list[object_cls/datatype]"""


def get_schema_for_endpoint(endpoint_url: str, void_file: str | None = None) -> SchemaDict:
    """Get a dict of VoID description of a SPARQL endpoint directly from the endpoint or from a VoID description URL.

    Formatted as: dict[subject_cls][predicate] = list[object_cls/datatype]

    Raises ValueError when ``void_file`` is given but cannot be read: an unreadable schema
    must stop a rebuild rather than leave the assistant without one. An endpoint that
    simply has no VoID description only logs a warning.
    """
    void_dict: SchemaDict = {}
    if void_file:
        try:
            rdflib.Graph().parse(void_file, format="turtle")
        except Exception as e:
            reason = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            raise ValueError(f"The schema file {void_file} could not be read: {reason}") from e
    try:
        for void_triple in query_sparql(GET_VOID_DESC, endpoint_url, use_file=void_file, check_service_desc=True)[
            "results"
        ]["bindings"]:
            if void_triple["subjectClass"]["value"] not in void_dict:
                void_dict[void_triple["subjectClass"]["value"]] = {}
            if void_triple["prop"]["value"] not in void_dict[void_triple["subjectClass"]["value"]]:
                void_dict[void_triple["subjectClass"]["value"]][void_triple["prop"]["value"]] = []
            if "objectClass" in void_triple:
                void_dict[void_triple["subjectClass"]["value"]][void_triple["prop"]["value"]].append(
                    void_triple["objectClass"]["value"]
                )
            if "objectDatatype" in void_triple:
                void_dict[void_triple["subjectClass"]["value"]][void_triple["prop"]["value"]].append(
                    void_triple["objectDatatype"]["value"]
                )
        if len(void_dict) == 0:
            raise Exception("No VoID description found")
    except Exception as e:
        logger.warning(f"Could not retrieve VoID description from {void_file if void_file else endpoint_url}: {e}")
    return void_dict


#: Bounds on the answer to a query a user or the public MCP asked for: read no more than this
#: many bytes, within this many seconds.
QUERY_MAX_BYTES = 5_000_000
QUERY_DEADLINE = 30


class ResponseTooLarge(Exception):
    """An endpoint's answer passed the byte cap the caller set: the rest was not read."""


def _read_json_bounded(client: httpx.Client, request: httpx.Request, max_bytes: int | None, deadline: float | None) -> Any:
    """Send ``request`` and decode its JSON body, reading no more than ``max_bytes`` within ``deadline`` seconds."""
    end = time.monotonic() + deadline if deadline else None
    resp = client.send(request, stream=True)
    try:
        resp.raise_for_status()
        body = bytearray()
        for chunk in resp.iter_bytes():
            body += chunk
            if max_bytes and len(body) > max_bytes:
                raise ResponseTooLarge(f"the answer is larger than {max_bytes} bytes: make the query return fewer rows")
            if end and time.monotonic() > end:
                raise TimeoutError(f"the endpoint took more than {deadline} seconds to answer")
    finally:
        resp.close()
    return json.loads(bytes(body))


# Use https://github.com/lu-pl/sparqlx ?
def query_sparql(
    query: str,
    endpoint_url: str,
    post: bool = False,
    timeout: float = 60,
    client: httpx.Client | None = None,
    use_file: str | None = None,
    check_service_desc: bool = False,
    max_bytes: int | None = None,
    deadline: float | None = None,
) -> Any:
    """Execute a SPARQL query on a SPARQL endpoint or its service description using httpx or a RDF turtle file using rdflib.

    ``timeout`` bounds each wait (connect, read): an endpoint that accepts and never answers
    must not hold a request, or the rebuild lock, forever. ``max_bytes`` stops reading an answer past that size (``ResponseTooLarge``), and ``deadline``
    bounds the whole request in seconds (``TimeoutError``): ``timeout`` alone only bounds each
    wait, so a slowly trickling answer could otherwise run on and fill memory.
    """
    query_resp: dict[str, Any] = {"results": {"bindings": []}}
    if use_file:
        g = rdflib.Graph()
        g.parse(use_file, format="turtle")
        results = g.query(query)
        query_resp = {
            "results": {"bindings": [{str(k): {"value": str(v)} for k, v in row.asdict().items()} for row in results]}  # type: ignore
        }
    else:
        should_close = False
        if client is None:
            client = guarded_client("sparql", follow_redirects=True, timeout=timeout)
            should_close = True
        try:
            request = (
                client.build_request(
                    "POST", endpoint_url, headers={"Accept": "application/sparql-results+json"}, data={"query": query}
                )
                if post
                else client.build_request(
                    "GET", endpoint_url, headers={"Accept": "application/sparql-results+json"}, params={"query": query}
                )
            )
            query_resp = _read_json_bounded(client, request, max_bytes, deadline)
            # query_resp = resp_json
            # Handle ASK queries
            if query_resp.get("boolean") is not None:
                query_resp = {
                    "results": {"bindings": [{"ask-variable": {"value": str(query_resp["boolean"]).lower()}}]}
                }
            elif check_service_desc and not query_resp.get("results", {}).get("bindings", []):
                # If no results found directly in the endpoint we check in its service description
                logger.debug(f"No results found, checking service description for {endpoint_url}...")
                try:
                    resp = client.get(
                        endpoint_url,
                        headers={"Accept": "text/turtle"},
                    )
                    resp.raise_for_status()
                    g = rdflib.Graph()
                    g.parse(data=resp.text, format="turtle")
                except Exception as e:
                    # Many endpoints have no Turtle service description (ldf.fi answers 404):
                    # that only means there is nothing more to find.
                    logger.debug(f"No service description at {endpoint_url}: {e}")
                    return query_resp
                results = g.query(query)
                bindings = []
                for row in results:
                    if hasattr(row, "asdict"):
                        bindings.append({str(k): {"value": str(v)} for k, v in row.asdict().items()})  # type: ignore
                    else:
                        # Handle tuple results
                        bindings.append(
                            {str(var): {"value": str(val)} for var, val in zip(results.vars, row, strict=False)}  # type: ignore
                        )
                query_resp = {"results": {"bindings": bindings}}
        finally:
            if should_close:
                client.close()
    return query_resp


class EndpointsMetadataManager:
    """Lazy-loading manager for the endpoints' prefixes and classes schema.

    Each project caches its own metadata in ``project.prefixes_path``; the manager merges
    all projects in memory. Without an explicit list it follows the project registry, so
    a project reloaded after a rebuild is picked up.
    """

    def __init__(self, projects: list[Project] | None = None, auto_init: bool = True) -> None:
        self._fixed_projects = projects
        self._prefixes_map: dict[str, str] = {}
        self._void_dict: EndpointsSchemaDict = {}
        self._per_project: dict[str, tuple[tuple[str, float], dict[str, Any]]] = {}
        self._initialized = False
        if auto_init:
            logger.info("Auto-initializing endpoints metadata...")
            self._ensure_loaded()

    @property
    def _projects(self) -> list[Project]:
        if self._fixed_projects is not None:
            return self._fixed_projects
        from sparql_llm.project import get_projects

        return get_projects()

    def _ensure_loaded(self) -> None:
        """Load each project's cache file. Never fetches: only ``refresh()`` hits the endpoints.

        Projects without a readable cache contribute nothing and are named in one warning,
        so a fresh install boots offline with empty maps until the index is built.
        """
        if self._initialized:
            return
        prefixes_map: dict[str, str] = {}
        void_dict: EndpointsSchemaDict = {}
        missing: list[str] = []
        for project in self._projects:
            try:
                data = json.loads(project.prefixes_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.debug(f"Could not load cached endpoints metadata for {project.id}: {e}")
                missing.append(project.id)
                continue
            for prefix, namespace in data.get("prefixes_map", {}).items():
                prefixes_map.setdefault(prefix, namespace)
            void_dict.update(data.get("classes_schema", {}))
        if missing:
            logger.warning(f"No endpoints metadata cache for project(s) {', '.join(missing)}: rebuild the index to create it")
        if void_dict:
            logger.info(f"💾 Loaded endpoints metadata for {len(void_dict)} endpoints")
        self._prefixes_map, self._void_dict = prefixes_map, void_dict
        self._initialized = True

    def reload(self) -> None:
        """Read the cached metadata files again on next use, e.g. after a rebuild promoted them."""
        self._initialized = False
        self._per_project.clear()

    def refresh(self) -> None:
        """Rebuild the metadata from its sources, replacing the cached file.

        The cache file used to be written only when missing, so a schema change
        never reached the query validator: vm7 kept validating against a
        months-old snapshot and rejected every correct marriage query. The admin
        index rebuild calls this. Only this process sees the new schema at once;
        other workers pick it up from the file when they restart.
        """
        prefixes, schemas = self._fetch()
        if not any(schema for project_schemas in schemas.values() for schema in project_schemas.values()):
            raise RuntimeError("no classes schema found for any endpoint; the previous schema is kept")
        self._cache(prefixes, schemas)

    def _fetch(self) -> tuple[dict[str, dict[str, str]], dict[str, "EndpointsSchemaDict"]]:
        """Build each project's prefixes map and classes schema from its endpoint and VoID file."""
        prefixes: dict[str, dict[str, str]] = {}
        schemas: dict[str, EndpointsSchemaDict] = {}
        logger.info(f"Fetching metadata for {len(self._projects)} endpoints...")
        for project in self._projects:
            logger.info(f"Fetching {project.endpoint_url} metadata...")
            void_file = str(project.void_path) if project.void_path.is_file() else None
            schemas[project.id] = {project.endpoint_url: get_schema_for_endpoint(project.endpoint_url, void_file)}
            prefixes_map = get_prefixes_for_endpoint(project.endpoint_url, None, {})
            # Endpoints that don't support SHACL prefix queries (e.g. Wisski):
            # extract prefixes from the VoID Turtle file's @prefix declarations.
            if void_file:
                _merge_prefixes_from_turtle(void_file, prefixes_map)
            prefixes[project.id] = merge_prefixes(prefixes_map, project.prefixes)
        return prefixes, schemas

    def _cache(self, prefixes: dict[str, dict[str, str]], schemas: dict[str, "EndpointsSchemaDict"]) -> None:
        """Write each project's metadata to its cache file, then swap the merged view in."""
        prefixes_map: dict[str, str] = {}
        void_dict: EndpointsSchemaDict = {}
        for project in self._projects:
            project_prefixes, project_schema = prefixes.get(project.id, {}), schemas.get(project.id, {})
            project.prefixes_path.parent.mkdir(parents=True, exist_ok=True)
            project.prefixes_path.write_text(
                json.dumps({"prefixes_map": project_prefixes, "classes_schema": project_schema}, indent=2),
                encoding="utf-8",
            )
            logger.info(f"💾 Cached endpoints metadata to {project.prefixes_path.resolve()}")
            for prefix, namespace in project_prefixes.items():
                prefixes_map.setdefault(prefix, namespace)
            void_dict.update(project_schema)
        self._prefixes_map, self._void_dict = prefixes_map, void_dict
        self._initialized = True

    def _project_metadata(self, project: Project) -> dict[str, Any]:
        """One project's cached metadata, re-read when its file changes; never fetched at request time."""
        path = project.prefixes_path
        try:
            key = (str(path), path.stat().st_mtime)
        except OSError:
            # Not rebuilt yet: the prefixes its project file declares, and no classes schema.
            return {"prefixes_map": dict(project.prefixes), "classes_schema": {}}
        cached = self._per_project.get(project.id)
        if cached is None or cached[0] != key:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Cannot read {path}: {e}")
                data = {}
            data = {
                "prefixes_map": data.get("prefixes_map") or dict(project.prefixes),
                "classes_schema": data.get("classes_schema") or {},
            }
            cached = self._per_project[project.id] = (key, data)
        return cached[1]

    def prefixes_for(self, project: Project) -> dict[str, str]:
        """This project's prefixes only: two datasets may bind the same prefix to different namespaces."""
        return self._project_metadata(project)["prefixes_map"]

    def schema_for(self, project: Project) -> "EndpointsSchemaDict":
        """This project's classes schema only, keyed by its endpoint URL."""
        return self._project_metadata(project)["classes_schema"]

    @property
    def prefixes_map(self) -> dict[str, str]:
        """Get prefixes map, loading lazily if needed."""
        self._ensure_loaded()
        return self._prefixes_map or {}

    @property
    def void_dict(self) -> "EndpointsSchemaDict":
        """Get endpoints VoID schema dict, loading lazily if needed."""
        self._ensure_loaded()
        return self._void_dict or {}
