import argparse
import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from qdrant_client.models import FieldCondition, Filter, MatchValue, ScoredPoint

from sparql_llm.config import settings
from sparql_llm.indexing.index_resources import embedding_model, endpoints_metadata, init_vectordb, qdrant_client
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


def resolve_endpoint_url(endpoint_url: str) -> str:
    """Return the endpoint a tool call meant, repairing a corrupted URL.

    gpt-oss sometimes damages the endpoint_url argument (e.g. repeating part of
    the host), which gets a 404 and costs the model a round. With a single
    endpoint configured, an unknown URL can only mean that one.
    """
    known = [endpoint["endpoint_url"] for endpoint in settings.endpoints]
    if endpoint_url in known or len(known) != 1:
        return endpoint_url
    return known[0]


def get_mcp_app(enable_resources_info_tool: bool = True) -> FastMCP:
    """Get the MCP server instance."""

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
    mcp = FastMCP(
        name=f"{settings.app_org} MCP",
        debug=True,
        dependencies=["mcp", "qdrant_client", "fastembed", "sparql-llm"],
        instructions=f"Provide tools to query the {settings.app_name} knowledge graph ({settings.app_topics}) via SPARQL.",
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        # https://github.com/modelcontextprotocol/python-sdk/issues/1798
        transport_security=transport_security,
    )

    # Check if the docs collection exists and has data, initialize if not
    # In prod with multiple workers, auto_init should be set to False to avoid race conditions
    try:
        collection_needs_init = (
            settings.force_index
            or not qdrant_client.collection_exists(settings.docs_collection_name)
            or not qdrant_client.get_collection(settings.docs_collection_name).points_count
        )
        if settings.auto_init and collection_needs_init:
            logger.info("📊 Initializing vectordb...")
            init_vectordb()
        elif not settings.auto_init and collection_needs_init:
            logger.warning(
                f"⚠️ Collection '{settings.docs_collection_name}' does not exist or is empty. Run the following command to initialize it:\n"
                "docker compose -f compose.prod.yml exec api uv run src/sparql_llm/indexing/index_resources.py"
            )
        else:
            logger.info(
                f"✅ Collection '{settings.docs_collection_name}' exists with {qdrant_client.get_collection(settings.docs_collection_name).points_count} points. Skipping initialization."
            )
    except Exception as e:
        logger.error(f"⚠️ Error checking or initializing vectordb: {e}")
        # Continue without initialization to avoid blocking the app startup

    # potential_entities: Potential entities and instances of classes

    search_sparql_docs_tool_desc = f"""Retrieve relevant SPARQL query examples and classes schema to write queries to answer user questions related to {settings.app_topics}

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
        for search_embeddings in embedding_model.embed([question, *steps, *potential_classes]):
            # Get SPARQL example queries
            relevant_docs.extend(
                doc
                for doc in qdrant_client.query_points(
                    query=search_embeddings,
                    collection_name=settings.docs_collection_name,
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
                    collection_name=settings.docs_collection_name,
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
        for search_embeddings in embedding_model.embed(classes):
            # Get other relevant documentation (classes schemas, general information)
            relevant_docs.extend(
                doc
                for doc in qdrant_client.query_points(
                    query=search_embeddings,
                    collection_name=settings.docs_collection_name,
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
            search_embeddings = next(iter(embedding_model.embed([question])))
            relevant_docs = qdrant_client.query_points(
                collection_name=settings.docs_collection_name,
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
        endpoint_url = resolve_endpoint_url(endpoint_url)
        resp_msg = ""
        # First check if query valid based on classes schema and known prefixes
        validation_output = validate_sparql(
            sparql_query, endpoint_url, endpoints_metadata.prefixes_map, endpoints_metadata.void_dict
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
            res = query_sparql(sparql_query, endpoint_url, timeout=10, post=True)
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
        endpoint_url = resolve_endpoint_url(endpoint_url)
        validation_output = validate_sparql(
            sparql_query,
            endpoint_url,
            endpoints_metadata.prefixes_map,
            endpoints_metadata.void_dict,
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
        """List all SPARQL endpoints indexed by this MCP server.

        Call this tool when the user asks what databases or data sources are
        available, or when you need to discover which endpoint to target.
        No arguments are required.

        Returns:
            A formatted list of endpoints, each with its label, URL,
            description, and homepage link (when available).
        """
        if not settings.endpoints:
            return "No SPARQL endpoints are currently configured."

        parts: list[str] = [
            f"There are {len(settings.endpoints)} SPARQL endpoint(s) available:\n",
        ]
        for idx, ep in enumerate(settings.endpoints, start=1):
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
        endpoint_url = resolve_endpoint_url(endpoint_url)
        void_dict = endpoints_metadata.void_dict.get(endpoint_url, {})

        if not void_dict:
            return (
                f"No schema information found for endpoint `{endpoint_url}`. "
                "The endpoint may not have a VoID description, or it has not "
                "been indexed yet."
            )

        converter = get_prefix_converter(endpoints_metadata.prefixes_map)
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
        This tool searches the pre-built entity index and returns the best
        matching candidates.

        Args:
            entity_name: The human-readable name or label to search for
                (e.g. "Max Frisch", "ETH Zürich").
            entity_type: Optional type/class filter to narrow results
                (e.g. "Person", "Organisation").  Leave empty to search
                across all types.
            endpoint_url: Optional endpoint URL to restrict the search to a
                specific data source.  Leave empty to search all endpoints.
            limit: Maximum number of candidate URIs to return (default 5).

        Returns:
            A list of matching entities with their URI, label, type, and
            source endpoint — or a message if no entity index is available.
        """
        collection_name = settings.entities_collection_name
        if not qdrant_client.collection_exists(collection_name):
            return (
                "The entity index has not been built yet. "
                "Entity URI resolution is unavailable.  Try constructing "
                "the query with a string-matching filter (e.g. FILTER "
                "CONTAINS or REGEX) on the entity's label instead."
            )

        collection_info = qdrant_client.get_collection(collection_name)
        if not collection_info.points_count:
            return (
                "The entity index exists but is empty. "
                "Entity URI resolution is unavailable.  Use a string-"
                "matching FILTER on the label as a fallback."
            )

        # Build optional filters
        filter_conditions: list[FieldCondition] = []
        if entity_type:
            filter_conditions.append(
                FieldCondition(
                    key="entity_type",
                    match=MatchValue(value=entity_type),
                )
            )
        if endpoint_url:
            filter_conditions.append(
                FieldCondition(
                    key="endpoint_url",
                    match=MatchValue(value=endpoint_url),
                )
            )
        query_filter = Filter(must=filter_conditions) if filter_conditions else None

        search_embedding = next(iter(embedding_model.embed([entity_name])))
        results = qdrant_client.query_points(
            query=search_embedding,
            collection_name=collection_name,
            limit=limit,
            query_filter=query_filter,
        ).points

        if not results:
            return (
                f"No entities matching '{entity_name}' were found in the "
                "index.  Try using a FILTER with REGEX or CONTAINS in "
                "your SPARQL query to match by label instead."
            )

        parts: list[str] = [
            f"Found {len(results)} candidate(s) for '{entity_name}':\n",
        ]
        for idx, point in enumerate(results, start=1):
            payload = point.payload or {}
            parts.append(
                f"{idx}. **{payload.get('label', 'N/A')}**\n"
                f"   - URI: `{payload.get('iri', 'N/A')}`\n"
                f"   - Type: `{payload.get('entity_type', 'N/A')}`\n"
                f"   - Endpoint: `{payload.get('endpoint_url', 'N/A')}`\n"
                f"   - Score: {point.score:.4f}"
            )
        return "\n".join(parts)

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


# if __name__ == "__main__":
#     cli()
