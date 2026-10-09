"""Resolve the entity names in a question to URIs, for the model to use instead of label filters."""

from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from sparql_llm.agent.state import State, StepOutput
from sparql_llm.config import Configuration
from sparql_llm.entity_resolver import Resolution, get_resolver

ENTITIES_PREAMBLE = """--- ENTITIES FOUND ---
Names from the question, matched against the labels in the knowledge graph (score 100 = exact match).
- Use the URI directly, e.g. VALUES ?person { <uri> }, instead of filtering on the label.
- If several candidates fit and the question does not say which one, do not guess: tell the user there are several and list them with their years.
- If candidates share the same label and nothing tells them apart, use all of them in VALUES.
- Ignore a candidate whose label does not denote the name in the question (a different person or place that merely looks similar).
- If a name has no match, fall back to a label filter on sdh-short:P9 and tell the user the name was not found exactly.
"""


def format_entities_message(resolutions: list[Resolution]) -> str:
    parts = [ENTITIES_PREAMBLE]
    for res in resolutions:
        if not res.candidates:
            parts.append(f'\n"{res.name}": no match.')
            continue
        shown = f"{len(res.candidates)} of {res.total}" if res.total > len(res.candidates) else str(res.total)
        parts.append(f'\n"{res.name}": {shown} candidate(s)')
        for c in res.candidates:
            years = f", {c.years}" if c.years else ""
            parts.append(f"- <{c.uri}> {c.label} ({c.type}{years}) score {c.score:.0f}")
    return "\n".join(parts)


async def resolve_entities(state: State, config: RunnableConfig) -> dict[str, Any]:
    """Look up each extracted entity name; add the candidates as a message ahead of retrieval."""
    configuration = Configuration.from_runnable_config(config)
    names = state.structured_question.extracted_entities
    if not configuration.enable_entities_resolution or not names:
        return {}
    resolver = get_resolver()
    if not resolver.available:
        return {"steps": [StepOutput(label="🖇️ Entity index not built yet: names matched by label")]}
    resolutions = [r for r in (resolver.resolve(n) for n in names) if r is not None]
    if not resolutions:
        return {}
    content = format_entities_message(resolutions)
    found = sum(1 for r in resolutions if r.candidates)
    return {
        "messages": [HumanMessage(content=content, name="resolve_entities")],
        "steps": [StepOutput(label=f"🖇️ Linked {found} of {len(resolutions)} entities", details=content)],
    }
