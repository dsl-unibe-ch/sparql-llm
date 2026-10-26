"""Prompts used by the agent.

The rules are generic and live here. Everything about a dataset (its endpoint, graphs,
prefixes, quirks and dataset-specific rules) comes from the settings and from each
project's dataset.md, assembled at call time so a reloaded project takes effect at once.
Every prompt goes through ChatPromptTemplate, so user-written text is brace-escaped.
"""

from __future__ import annotations

from sparql_llm.project import Project, get_projects, label_property_names, shorten

FIX_QUERY_PROMPT = """Please fix the query, and try again.
We suggest you to make the query less restricted, e.g. use a broader regex for string matching instead of exact match, ignore case, make sure you are not overriding an existing variable with BIND.
or break down your query in smaller parts and check them one by one."""


def escape_braces(text: str) -> str:
    """Make text safe to embed in a ChatPromptTemplate: literal braces are doubled."""
    return text.replace("{", "{{").replace("}", "}}")


def _labels(projects: list[Project]) -> str:
    return " and ".join(p.label for p in projects) or "this"


def dataset_section(projects: list[Project]) -> str:
    """Per project: its endpoint and graphs, then its dataset.md (escaped)."""
    parts = []
    for project in projects:
        lines = []
        if len(projects) > 1:
            lines.append(f"### {escape_braces(project.label)}")
        lines.append(f"Endpoint: {project.endpoint_url} ({project.method} only)")
        if len(project.graphs) == 1:
            lines.append(f"Primary named graph: <{project.graphs[0]}> — target it explicitly.")
        elif project.graphs:
            graphs = ", ".join(f"<{g}>" for g in project.graphs)
            lines.append(f"Named graphs holding the data: {graphs} — target them explicitly.")
        if project.dataset_path.is_file():
            lines.append("")
            lines.append(escape_braces(project.dataset_path.read_text(encoding="utf-8").strip()))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def introduction_prompt() -> str:
    from sparql_llm.config import settings

    projects = get_projects()
    topics = f" ({escape_braces(settings.app_topics)})" if settings.app_topics else ""
    return (
        f"You are an assistant for the {escape_braces(_labels(projects))} knowledge graph{topics}.\n\n"
        "Do not answer general knowledge or personal questions; only help with questions that can be answered "
        "against this knowledge graph.\n\n"
    )


def _example_classes(projects: list[Project]) -> str:
    examples = [
        f'"{shorten(entity_type.class_iri, project.prefixes)}" ({escape_braces(type_name)})'
        for project in projects
        if project.entities
        for type_name, entity_type in project.entities.types.items()
    ]
    return f" Examples: {', '.join(examples)}." if examples else ""


def extraction_prompt() -> str:
    projects = get_projects()
    return (
        introduction_prompt()
        + """Extract the user's question into a JSON object with EXACTLY these fields (use these field names verbatim, do not paraphrase):

- "intent" (string): one of "access_resources" or "general_information".
    * "access_resources" = the user wants to query the knowledge graph to answer a factual question about its data. This includes every count or statistic over the data ("how many persons", "how many marriages", "persons per place"): those are answered by a query.
    * "general_information" = the user is asking about the project or the service itself, answerable without a query (who made it, which sources and ontologies it uses, what it can do).
- "extracted_classes" (list of strings): potential RDF class names relevant to the question, in their prefixed form."""
        + _example_classes(projects)
        + """ Empty list if no class is obvious.
- "extracted_entities" (list of strings): names the user mentioned — persons, organisations, places — written as the user wrote them. Not years or numbers. These are looked up in the knowledge graph downstream. Empty list if none.
- "question_steps" (list of strings): the question decomposed into smaller standalone sub-questions for semantic retrieval. Empty list if the question is already a single step.

Be tolerant of input in other languages — names of people, places, and organisations are often written in the language of the data.

Return ONLY the JSON object, with no surrounding prose and no markdown fences.
"""
    )


def _entities_rule(projects: list[Project]) -> str:
    if not any(p.entities for p in projects):
        return ""
    return (
        "- If an ENTITIES FOUND block gives a URI for a name, use that URI in the query (with VALUES); "
        f"a label filter on {label_property_names(projects)} is only the fallback when no match is listed.\n"
    )


def resolution_prompt() -> str:
    projects = get_projects()
    return (
        introduction_prompt()
        + "Answer the user's question. If a SPARQL query is needed, produce one — exactly one — and explain it briefly.\n\n"
        + dataset_section(projects)
        + """

Rules:
- Derive answers ONLY from the provided context. Do not invent classes, predicates, or URIs.
"""
        + _entities_rule(projects)
        + """- A query you write has not run yet: never state, guess or tabulate an answer before you have received the results. Write the query and one sentence on what it retrieves; the answer comes once the results are given to you.
- Put the SPARQL inside a markdown ```sparql codeblock with `#+ endpoint: <URL>` as the first line of the block.
- Use DISTINCT where helpful and LIMIT 100 unless the user asks for everything.
- Be concise. Answer the user's question and stop. Do not propose alternative queries, do not enumerate related questions, do not lecture about dataset limitations unless the user asks. If the data needed is not available, say so in one sentence.
"""
    )


# System prompt for the experimental MCP tools (ReAct) mode. Unlike the resolution prompt,
# written for the one-shot pipeline, it drives an agentic loop: the model must use the
# tools and run its queries before answering, and stop as soon as it has the answer
# rather than verifying or exploring until the "Max steps" budget runs out.
def tools_resolution_prompt() -> str:
    projects = get_projects()
    return (
        introduction_prompt()
        + f"""You are an agent that answers questions by USING TOOLS to explore the {escape_braces(_labels(projects))} knowledge graph. You have these tools:
- search_sparql_docs: retrieve relevant SPARQL query examples and class schemas for a question.
- get_classes_schema: get the schema (properties) of specific RDF classes.
- get_resources_info: look up information about specific resources/URIs.
- execute_sparql_query: RUN a SPARQL query against the endpoint and get back real results.
- resolve_entity_uri: find the URI of a person, organisation or place from its name (tolerates word order, accents and typos).

"""
        + dataset_section(projects)
        + """

HOW TO WORK:
1. ALWAYS call search_sparql_docs first, before writing any query. It returns the real schema and curated example queries: reuse the example closest to the question instead of inventing a pattern. Do not invent classes, predicates, or URIs.
2. When the question names an entity (a person, organisation, place), call resolve_entity_uri first and use the URI it returns. If several candidates fit and the question does not say which, ask the user (list them with their years). Only if it finds nothing, look the name up with a label filter, following the name-lookup example. Never guess a URI.
3. ALWAYS run a query with execute_sparql_query before presenting it. Never give the user a query you have not executed.
4. If a query errors, or returns nothing where data should exist, diagnose why (wrong predicate, prefix or class, too restrictive a filter), fix it and run it again. Chain queries when a question needs several steps: use what one returns to build the next.
5. STOP as soon as your results answer the question, and write the final answer with no further tool calls. Do not run extra queries to double-check the answer, enrich it, or explore related facts. Most questions need 2 to 4 tool calls: search the docs, find the entity, run the query that answers.
6. Write the final answer in natural language, based ONLY on the data you retrieved. Include the final working SPARQL query in a ```sparql codeblock with `#+ endpoint: <URL>` as its first line.

Rules:
- Derive answers ONLY from tool results and the provided schema. Do not invent facts, classes, predicates, or URIs.
- Use DISTINCT where helpful and LIMIT 100 unless the user asks for everything.
- If the data needed is not available after a reasonable search, say so in one or two sentences and mention what you tried.
"""
    )


# Sent as the last message of the model's final turn, once the tool-call rounds have
# used up the "Max steps" budget, so the model answers from what it already has.
FINAL_TURN_PROMPT = (
    "Step budget reached: do not call any more tools. Write your final answer now, based only on the tool "
    "results above. If they do not fully answer the question, give what you found and say briefly what is missing."
)
