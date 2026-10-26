"""The MCP tools, served per dataset: ``/mcp/<id>``, and bare ``/mcp`` for the first dataset.

Every tool of an app works on the dataset the app was created for: its index, entity
names, prefixes, schema, endpoint and method. No tool argument can change the dataset.
The MCP server has no login, so an unpublished dataset's address answers only requests
carrying the in-memory ``INTERNAL_TOKEN``: the app's own tools agent, serving a curator.
"""

import argparse
import contextlib
import json
import secrets
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import anyio.abc
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from qdrant_client.models import FieldCondition, Filter, MatchValue, ScoredPoint
from starlette.responses import JSONResponse

from sparql_llm.config import settings
from sparql_llm.entity_resolver import describe_for_tool, get_resolver
from sparql_llm.indexing.index_resources import embedding_model, endpoints_metadata, qdrant_client
from sparql_llm.indexing.rebuild import collection_for
from sparql_llm.project import Project, allowed_endpoint, get_project, get_projects, post_for
from sparql_llm.utils import compress_list, get_prefix_converter, logger, query_sparql
from sparql_llm.validate_sparql import validate_sparql

# What are the rat orthologs of the human TP53?
# TODO: MCP integrated https://github.com/modelcontextprotocol/python-sdk/pull/1007

PROMPT_TOOL_SPARQL = """Formulate a precise SPARQL query to answer the user's question from the knowledge graph.

## SPARQL Query Guidelines
- **Always include the endpoint URL** as a comment at the start: `#+ endpoint: http://example.org/sparql`
- **Use only ONE endpoint** per query
- **Base your query on the provided context**, never create generic or unsupported queries
- **Use appropriate prefixes** and class names from the schema documentation

## Knowledge Base
The following {docs_count} documents contain relevant query examples, and classes schemas to help you construct an accurate response:

{formatted_docs}
"""

FIX_QUERY_PROMPT = """Please fix the query, and try again.
We suggest you to make the query less restricted, e.g. use a broader regex for string matching instead of exact match,
ignore case, make sure you are not overriding an existing variable with BIND, or break down your query in smaller parts
and check them one by one."""

# An empty result is not always a mistake: "no children recorded" is a real answer.
# Telling the model to always fix and retry sent it round the loop again for
# queries that were already correct.
EMPTY_RESULT_PROMPT = """SPARQL query returned no results.
An empty result can be the real answer: if the entity is already confirmed and the pattern comes from the examples,
report that nothing is recorded and stop. If you were looking something up by name, or wrote the pattern yourself,
the query is probably too restrictive: use a broader, case-insensitive string match, check the class, predicates and
prefixes, or break the query into smaller parts."""


#: Lets the app's own tools agent use an unpublished dataset's MCP address. Held in memory
#: only, new at every start: nothing outside this process can know it.
INTERNAL_TOKEN = secrets.token_urlsafe(32)
INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def _first_project() -> Project:
    return get_projects()[0]


def agent_connection(project_id: str) -> dict[str, Any]:
    """How the app's own tools agent reaches a dataset's MCP tools: listing them and running them alike.

    Always that dataset's address, with the token that opens it before it is published.
    """
    return {
        "url": f"{settings.server_url}/mcp/{project_id}",
        "transport": "streamable_http",
        "headers": {INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
    }


def resolve_endpoint_url(endpoint_url: str, project: Project | None = None) -> tuple[str, str]:
    """The endpoint a tool call runs on: always its dataset's, whatever URL the model passed.

    gpt-oss sometimes damages the endpoint_url argument (e.g. repeating part of
    the host); another dataset's URL must not move the query there either.
    """
    return allowed_endpoint(endpoint_url, project or _first_project())


def get_mcp_app(project_id: str = "", enable_resources_info_tool: bool = True) -> FastMCP:
    """The MCP server of one dataset (the first one when ``project_id`` is empty).

    Every tool reads the dataset when it runs, so a rebuild's reloaded project is used at
    once; the server's name, instructions and tool descriptions are fixed when it is made.
    """

    def _project() -> Project:
        return get_project(project_id) if project_id else _first_project()

    project = _project()

    transport_security = (
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost:*", "127.0.0.1:*", settings.app_public_host],
            allowed_origins=["http://localhost:*", f"https://{settings.app_public_host}"],
        )
        if settings.app_public_host
        else None
    )

    # Create MCP server https://github.com/modelcontextprotocol/python-sdk
    topics = f" ({project.topics})" if project.topics else ""
    mcp = FastMCP(
        name=f"{project.org or project.label} MCP",
        debug=True,
        dependencies=["mcp", "qdrant_client", "fastembed", "sparql-llm"],
        instructions=f"Provide tools to query the {project.label} knowledge graph{topics} via SPARQL.",
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        # https://github.com/modelcontextprotocol/python-sdk/issues/1798
        transport_security=transport_security,
    )

    # A dataset's index is only ever built by its Rebuild (admin page), from checked files.
    # Building one here would create a plain collection under the name its alias needs.
    try:
        collection = collection_for(project)
        if not qdrant_client.collection_exists(collection):
            logger.warning(f"⚠️ {project.label} has no search index '{collection}' yet: run its Rebuild on the admin page.")
    except Exception as e:
        logger.error(f"⚠️ Error checking the search index of {project.label}: {e}")

    # potential_entities: Potential entities and instances of classes

    search_sparql_docs_tool_desc = f"""Retrieve relevant SPARQL query examples and classes schema to write queries to answer user questions related to {project.topics or project.label}

Args:
    question: The question to be answered with a SPARQL query
    potential_classes: High level concepts and potential classes that could be found in the SPARQL endpoints
    steps: Split the question in standalone smaller parts if relevant (if the question is already 1 step, leave empty)

Returns:
    Relevant documents (examples, classes schemas)
"""

    @mcp.tool(description=search_sparql_docs_tool_desc)
    async def search_sparql_docs(question: str, potential_classes: list[str], steps: list[str]) -> str:
        relevant_docs: list[ScoredPoint] = []
        for search_embeddings in embedding_model.embed_query([question, *steps, *potential_classes]):
            # Get SPARQL example queries
            relevant_docs.extend(
                doc
                for doc in qdrant_client.query_points(
                    query=search_embeddings,
                    collection_name=collection_for(_project()),
                    limit=settings.default_number_of_retrieved_docs,
                    query_filter=Filter(
                        must=[
                            FieldCondition(
                                key="doc_type",
                                match=MatchValue(value="SPARQL endpoints query examples"),
                            )
                        ]
                    ),
                ).points
                # Make sure we don't add duplicate docs
                if doc.payload
                and doc.payload.get("answer")
                not in {
                    existing_doc.payload.get("answer") if existing_doc.payload else None
                    for existing_doc in relevant_docs
                }
            )
            # Get other relevant documentation (classes schemas, general information)
            relevant_docs.extend(
                doc
                for doc in qdrant_client.query_points(
                    query=search_embeddings,
                    collection_name=collection_for(_project()),
                    limit=settings.default_number_of_retrieved_docs,
                    query_filter=Filter(
                        must_not=[
                            FieldCondition(
                                key="doc_type",
                                match=MatchValue(value="SPARQL endpoints query examples"),
                            )
                        ]
                    ),
                ).points
                if doc.payload
                and doc.payload.get("answer")
                not in {
                    existing_doc.payload.get("answer") if existing_doc.payload else None
                    for existing_doc in relevant_docs
                }
            )
        return PROMPT_TOOL_SPARQL.format(docs_count=str(len(relevant_docs)), formatted_docs=format_docs(relevant_docs))

    @mcp.tool()
    async def get_classes_schema(classes: list[str]) -> str:
        """Search for specific classes and their schema in the SPARQL endpoints.

        Args:
            classes: High level concepts and potential classes that could be found in the SPARQL endpoints

        Returns:
            Relevant classes schemas in ShEx format
        """
        relevant_docs: list[ScoredPoint] = []
        for search_embeddings in embedding_model.embed_query(classes):
            # Get other relevant documentation (classes schemas, general information)
            relevant_docs.extend(
                doc
                for doc in qdrant_client.query_points(
                    query=search_embeddings,
                    collection_name=collection_for(_project()),
                    limit=settings.default_number_of_retrieved_docs,
                    query_filter=Filter(
                        must_not=[
                            FieldCondition(
                                key="doc_type",
                                match=MatchValue(value="SPARQL endpoints query examples"),
                            )
                        ]
                    ),
                ).points
                if doc.payload
                and doc.payload.get("answer")
                not in {
                    existing_doc.payload.get("answer") if existing_doc.payload else None
                    for existing_doc in relevant_docs
                }
            )
        return f"""Here is a list of {len(relevant_docs)} classes schema relevant to the request:
    {format_docs(relevant_docs)}"""

    if enable_resources_info_tool:

        @mcp.tool()
        def get_resources_info(question: str) -> str:
            """Get information about the SPARQL endpoints indexed by this MCP server.

            Only call this tool when the user explicitly asks for information about the resources themselves.

            Args:
                question: The user question.

            Returns:
                str: Information about the resources.
            """
            search_embeddings = next(iter(embedding_model.embed_query([question])))
            relevant_docs = qdrant_client.query_points(
                collection_name=collection_for(_project()),
                query=search_embeddings,
                limit=settings.default_number_of_retrieved_docs,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="doc_type",
                            match=MatchValue(value="General information"),
                        )
                    ]
                ),
            ).points
            return f"""Here is a list of {len(relevant_docs)} documents relevant to the question that will help answer it accurately:
{format_docs(relevant_docs)}"""

    @mcp.tool()
    def execute_sparql_query(sparql_query: str, endpoint_url: str = "") -> str:
        """Execute a SPARQL query against a SPARQL endpoint.

        Args:
            sparql_query: A valid SPARQL query string
            endpoint_url: The SPARQL endpoint URL to execute the query against (defaults to the configured endpoint)

        Returns:
            The query results in JSON format
        """
        project = _project()
        endpoint_url, _ = resolve_endpoint_url(endpoint_url, project)
        resp_msg = ""
        # First check if query valid based on classes schema and known prefixes
        validation_output = validate_sparql(
            sparql_query, endpoint_url, endpoints_metadata.prefixes_for(project), endpoints_metadata.schema_for(project)
        )
        if validation_output["fixed_query"]:
            # Pass the fixed query to the client
            resp_msg += f"Fixed the prefixes of the generated SPARQL query automatically:\n```sparql\n{validation_output['fixed_query']}\n```\n"
            sparql_query = validation_output["fixed_query"]
        if validation_output["errors"]:
            # Recall the LLM to try to fix the errors
            error_str = "- " + "\n- ".join(validation_output["errors"])
            resp_msg += (
                "The query generated in the original response is not valid according to the endpoints schema.\n"
                f"### Validation results\n{error_str}\n"
                f"### Erroneous SPARQL query\n```sparql\n{validation_output['original_query']}\n```\n"
                "Fix the SPARQL query helping yourself with the error message and context from previous messages."
            )
            return resp_msg
        # Execute the SPARQL query
        try:
            res = query_sparql(sparql_query, endpoint_url, timeout=10, post=post_for(project))
            bindings = res.get("results", {}).get("bindings")
            if not bindings:
                # If no results, return a message to ask fix the query
                resp_msg += f"{EMPTY_RESULT_PROMPT}\n```sparql\n{sparql_query}\n```"
            else:
                # If results, return them (limit to first 50 rows if too many)
                resp_msg += f"Results of SPARQL query execution on {endpoint_url}"
                if len(bindings) > 50:
                    res["results"]["bindings"] = bindings[:50]
                    resp_msg += f" (showing first 50 of {len(bindings)} results)"
                resp_msg += f":\n```\n{json.dumps(res, indent=2)}\n```"
        except Exception as e:
            resp_msg += f"SPARQL query returned error: {e}. {FIX_QUERY_PROMPT}\n```sparql\n{sparql_query}\n```"
        return resp_msg

    @mcp.tool()
    def validate_sparql_query(sparql_query: str, endpoint_url: str = "") -> str:
        """Validate a SPARQL query without executing it (dry run).

        Use this tool to check a query for problems **before** calling
        ``execute_sparql_query``.  It performs two levels of checking:

        1. **Syntax check** — parses the query and auto-fixes missing
           prefixes when possible.
        2. **Schema check** — verifies that the classes and predicates used
           in the query actually exist in the endpoint's VoID description.

        This is much faster and cheaper than executing a broken query.
        Call this first when building complex or multi-part queries.

        Args:
            sparql_query: The SPARQL query string to validate.
            endpoint_url: The SPARQL endpoint URL the query is intended for.
                If omitted or empty, defaults to the first configured
                endpoint.

        Returns:
            A validation report.  If the query is valid, confirms it is
            ready to execute.  If there are issues, lists each error with
            hints on how to fix it, and includes the auto-fixed query when
            prefix corrections were applied.
        """
        project = _project()
        endpoint_url, _ = resolve_endpoint_url(endpoint_url, project)
        validation_output = validate_sparql(
            sparql_query,
            endpoint_url,
            endpoints_metadata.prefixes_for(project),
            endpoints_metadata.schema_for(project),
        )

        parts: list[str] = []

        if validation_output["fixed_query"]:
            parts.append(
                "**Auto-fixed prefixes.** The corrected query is:\n"
                f"```sparql\n{validation_output['fixed_query']}\n```"
            )

        if validation_output["errors"]:
            error_list = "\n".join(
                f"- {err}" for err in validation_output["errors"]
            )
            parts.append(
                f"**Validation found {len(validation_output['errors'])} "
                f"issue(s):**\n{error_list}\n\n"
                "Fix these issues before executing the query."
            )
        else:
            parts.append(
                "✅ **Query is valid.** Syntax and schema checks passed — "
                "the query is ready to execute."
            )

        return "\n\n".join(parts)

    @mcp.tool()
    def list_available_endpoints() -> str:
        """List the SPARQL endpoint this MCP server answers from.

        Call this tool when the user asks what databases or data sources are
        available, or when you need to discover which endpoint to target.
        No arguments are required.

        Returns:
            The endpoint with its label, URL, description, and homepage link
            (when available).
        """
        # This address serves one dataset: never name another, published or not.
        endpoints = [_project().as_endpoint_links()]
        parts: list[str] = [
            f"There are {len(endpoints)} SPARQL endpoint(s) available:\n",
        ]
        for idx, ep in enumerate(endpoints, start=1):
            label = ep.get("label", "Unnamed")
            url = ep["endpoint_url"]
            desc = ep.get("description", "No description provided.")
            homepage = ep.get("homepage_url")
            entry = f"{idx}. **{label}**\n   - Endpoint: `{url}`\n   - Description: {desc}"
            if homepage:
                entry += f"\n   - Homepage: {homepage}"
            parts.append(entry)
        return "\n\n".join(parts)

    @mcp.tool()
    def list_classes_for_endpoint(endpoint_url: str = "") -> str:
        """List every class available in a SPARQL endpoint's schema.

        Unlike ``get_classes_schema`` (which uses semantic search and may miss
        classes whose names differ from the user's wording), this tool returns
        the **complete catalogue** of classes derived from the endpoint's VoID
        description.  Use it to browse the full data model before writing a
        query.

        Args:
            endpoint_url: The SPARQL endpoint URL to inspect.  If omitted or
                empty, defaults to the first configured endpoint.

        Returns:
            A numbered list of all class names (in compact prefix:LocalName
            form) together with the predicates each class supports.
        """
        project = _project()
        endpoint_url, _ = resolve_endpoint_url(endpoint_url, project)
        void_dict = endpoints_metadata.schema_for(project).get(endpoint_url, {})

        if not void_dict:
            return (
                f"No schema information found for endpoint `{endpoint_url}`. "
                "The endpoint may not have a VoID description, or it has not "
                "been indexed yet."
            )

        converter = get_prefix_converter(endpoints_metadata.prefixes_for(project))
        parts: list[str] = [
            f"Classes available in `{endpoint_url}` ({len(void_dict)} classes):\n",
        ]
        for idx, (cls_uri, pred_dict) in enumerate(sorted(void_dict.items()), start=1):
            cls_short = converter.compress(cls_uri, passthrough=True)
            preds_short = compress_list(converter, list(pred_dict.keys()))
            parts.append(
                f"{idx}. **{cls_short}**\n"
                f"   Predicates: {', '.join(f'`{p}`' for p in preds_short)}"
            )
        return "\n".join(parts)

    @mcp.tool()
    def resolve_entity_uri(
        entity_name: str,
        entity_type: str = "",
        endpoint_url: str = "",
        limit: int = 5,
    ) -> str:
        """Find the URI of a real-world entity given its human-readable name.

        Users refer to entities by everyday names (e.g. "Albert Einstein",
        "University of Bern"), but SPARQL queries require their exact URIs.
        This tool matches the name against every label in the knowledge graph
        (tolerating word order, accents and typos) and returns the best
        candidates, with birth and death years for persons.

        Args:
            entity_name: The human-readable name or label to search for,
                e.g. a person's full name, an organisation or a place.
            entity_type: Optional type filter, one of the dataset's entity types
                (e.g. "Person").  Leave empty to search all types.
            endpoint_url: Ignored (there is a single endpoint); kept for
                compatibility.
            limit: Maximum number of candidate URIs to return (default 5).

        Returns:
            A list of matching entities with their URI, label, type, and
            source endpoint — or a message if no entity index is available.
        """
        # One endpoint, so endpoint_url is accepted for compatibility and ignored.
        project = _project()
        return describe_for_tool(get_resolver(project), entity_name, project, entity_type, limit)

    @mcp.tool()
    def explain_query_results(
        query_results_json: str,
        original_question: str,
        sparql_query: str = "",
    ) -> str:
        """Summarise raw SPARQL query results into a concise overview.

        After ``execute_sparql_query`` returns JSON results, call this tool
        to produce a structured, human-friendly summary.  It extracts key
        statistics (row count, distinct values per column, value ranges)
        and highlights notable patterns.

        Use this when:
        - Results contain many rows or columns of URIs that are hard to read.
        - You want to double-check result quality before presenting to user.
        - The user explicitly asks for a summary rather than raw data.

        Args:
            query_results_json: The raw JSON string returned by
                ``execute_sparql_query`` (the ``results.bindings`` part or
                the full response).
            original_question: The natural-language question the user asked,
                so the summary can be framed in context.
            sparql_query: Optional.  The SPARQL query that produced the
                results (included for reference in the summary).

        Returns:
            A structured text summary with row count, column overview,
            sample values, and potential data-quality observations.
        """
        try:
            data = json.loads(query_results_json)
        except (json.JSONDecodeError, TypeError):
            return (
                "Could not parse the provided JSON.  Make sure you pass "
                "the raw JSON string from execute_sparql_query."
            )

        # Normalise: accept either the full response or just the bindings list.
        bindings: list[dict] = []
        if isinstance(data, dict):
            bindings = data.get("results", {}).get("bindings", [])
        elif isinstance(data, list):
            bindings = data

        if not bindings:
            return (
                f"The query returned **0 results** for the question: "
                f"*{original_question}*\n\n"
                "This may mean that no matching data exists, or the query "
                "was too restrictive."
            )

        # Gather column names from the first binding.
        columns = list(bindings[0].keys())
        total_rows = len(bindings)

        # Per-column analysis.
        col_summaries: list[str] = []
        for col in columns:
            values = [
                row[col]["value"]
                for row in bindings
                if col in row and "value" in row[col]
            ]
            distinct = set(values)
            n_distinct = len(distinct)

            # Show a few sample values (max 5).
            samples = sorted(distinct)[:5]
            samples_str = ", ".join(f"`{s}`" for s in samples)
            if n_distinct > 5:
                samples_str += f", … ({n_distinct} distinct values)"

            col_summaries.append(
                f"- **{col}**: {n_distinct} distinct value(s) — {samples_str}"
            )

        # Assemble the summary.
        parts: list[str] = [
            f"### Results Summary\n",
            f"**Question:** {original_question}\n",
            f"**Total rows:** {total_rows}\n",
        ]
        if sparql_query:
            parts.append(f"**Query:**\n```sparql\n{sparql_query}\n```\n")
        parts.append("**Columns:**\n" + "\n".join(col_summaries) + "\n")

        # Data-quality observations.
        observations: list[str] = []
        if total_rows == 1:
            observations.append(
                "Only a single row was returned — this is likely a "
                "count or a unique match."
            )
        for col in columns:
            values = [
                row[col]["value"]
                for row in bindings
                if col in row and "value" in row[col]
            ]
            if len(set(values)) == 1 and total_rows > 1:
                observations.append(
                    f"Column `{col}` has the same value (`{values[0]}`) "
                    f"in all {total_rows} rows — this may indicate a "
                    "redundant variable or a data-quality issue."
                )

        if observations:
            parts.append(
                "**Observations:**\n" + "\n".join(f"- {o}" for o in observations)
            )

        return "\n".join(parts)

    # https://modelcontextprotocol.io/docs/concepts/resources
    @mcp.resource("examples://{question}")
    def get_examples(question: str) -> str:
        """Get relevant SPARQL query examples and other documents to help the user write a SPARQL query."""
        return search_sparql_docs(question, [], [])

    return mcp


def format_docs(docs: list[ScoredPoint]) -> str:
    """Format a list of documents."""
    return "\n".join(_format_doc(doc) for doc in docs)


def _format_doc(doc: ScoredPoint) -> str:
    """Format a single document, with special formatting based on doc type (sparql, schema)."""
    if not doc.payload:
        return ""
    if doc.payload.get("answer"):
        doc_lang = ""
        doc_type = str(doc.payload.get("doc_type", "")).lower()
        if "query" in doc_type:
            doc_lang = f"sparql\n#+ endpoint: {doc.payload.get('endpoint_url', 'undefined')}"
        elif "schema" in doc_type:
            doc_lang = "shex"
        return f"{doc.payload['question']}:\n\n```{doc_lang}\n{doc.payload.get('answer')}\n```"
    return "".join(f" {k}={v!r}" for k, v in doc.payload.items())


def cli() -> None:
    """Run the MCP server with appropriate transport."""
    parser = argparse.ArgumentParser(
        description="A Model Context Protocol (MCP) server for BioData resources at the SIB."
    )
    parser.add_argument("--http", action="store_true", help="Use Streamable HTTP transport")
    parser.add_argument("--port", type=int, default=8888, help="Port to run the server on")
    # parser.add_argument("settings_filepath", type=str, nargs="?", default="sparql-mcp.json", help="Path to settings file")
    args = parser.parse_args()
    mcp = get_mcp_app()
    # settings = Settings.from_file(args.settings_filepath)
    if args.http:
        mcp.run()
        mcp.settings.port = args.port
        mcp.settings.log_level = "INFO"
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


class McpDispatcher:
    """The ASGI app mounted at ``/mcp``: ``/mcp/<id>/…`` to that dataset's app, bare ``/mcp/…`` to the first.

    Each dataset's app is made on first use and kept. Its session manager runs in a task of
    its own inside the task group ``running()`` holds for the app's lifetime: a task group
    must be left by the task that entered it, never by a request. An unknown id is a 404;
    an unpublished dataset answers only requests carrying ``INTERNAL_TOKEN``.
    """

    def __init__(self, app_factory: Callable[[str], tuple[Any, Any]] | None = None) -> None:
        self._factory = app_factory or self._fastmcp_app
        self._apps: dict[str, Any] = {}
        self._tasks: anyio.abc.TaskGroup | None = None
        self._lock = anyio.Lock()

    @staticmethod
    def _fastmcp_app(project_id: str) -> tuple[Any, Any]:
        mcp = get_mcp_app(project_id)
        return mcp.streamable_http_app(), mcp.session_manager.run()

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        async with anyio.create_task_group() as tasks:
            self._tasks = tasks
            try:
                yield
            finally:
                self._tasks = None
                self._apps.clear()
                tasks.cancel_scope.cancel()

    async def _app(self, project_id: str) -> Any:
        async with self._lock:
            if project_id not in self._apps:
                if self._tasks is None:
                    raise RuntimeError("The MCP apps are not running (McpDispatcher.running() was not entered).")
                app, session = self._factory(project_id)

                async def serve(*, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
                    async with session:
                        task_status.started()
                        await anyio.sleep_forever()

                await self._tasks.start(serve)
                self._apps[project_id] = app
            return self._apps[project_id]

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        from sparql_llm.datasets import dataset_state, list_dataset_ids

        if scope["type"] != "http":
            return
        # A Mount keeps the full path and extends root_path; route on what is left.
        root_path = scope.get("root_path", "")
        path = scope.get("path", "/")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        first, _, rest = path.lstrip("/").partition("/")
        ids = list_dataset_ids()
        served = {p.id for p in get_projects()}
        if not first:
            project_id, subpath = (ids[0] if ids else ""), path
        elif first in ids:
            project_id, subpath = first, "/" + rest
        else:
            await JSONResponse({"error": f"There is no dataset '{first}'."}, status_code=404)(scope, receive, send)
            return
        if project_id not in served:
            await JSONResponse({"error": f"The dataset '{project_id}' is not available."}, status_code=404)(
                scope, receive, send
            )
            return
        if dataset_state(project_id) != "published":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            token = headers.get(INTERNAL_TOKEN_HEADER.lower(), "")
            if not secrets.compare_digest(token, INTERNAL_TOKEN):
                await JSONResponse({"error": "This dataset is not published."}, status_code=403)(scope, receive, send)
                return
        if subpath != path:
            scope = {**scope, "root_path": f"{root_path}/{project_id}"}
        app = await self._app(project_id)
        await app(scope, receive, send)


# if __name__ == "__main__":
#     cli()
