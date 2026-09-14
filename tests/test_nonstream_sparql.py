"""Natural-language mode must hide SPARQL on the non-streaming path too.

``stream_response`` strips SPARQL codeblocks from the answer as it streams, but a
request with ``stream=False`` gets the finished graph state back as JSON, so the
answer still carried the query. Since the tools prompt no longer asks the model
to keep its SPARQL inside ``<think>``, the codeblock is in almost every answer.

Requires the optional ``agent`` extra (langgraph, langfuse…), which CI does not
install, so this skips there and runs in a dev environment set up with
``uv sync --extra agent``.
"""

import pytest

pytest.importorskip("langgraph", reason="needs the optional 'agent' extra")
pytest.importorskip("langfuse", reason="needs the optional 'agent' extra")

from sparql_llm.agent.main import hide_sparql_in_answers  # noqa: E402

ANSWER = "Ernst Brenner's spouse was Lina Sturzenegger.\n```sparql\nASK {}\n```\nMore?"


def _response() -> dict:
    return {
        "messages": [
            {"type": "human", "content": "Show me the query: ```sparql\nASK {}\n```"},
            {
                "type": "ai",
                "content": "",
                "tool_calls": [{"name": "execute_sparql_query", "args": {"sparql_query": "ASK {}"}}],
            },
            {"type": "tool", "content": "```sparql\nASK {}\n```"},
            {"type": "ai", "content": ANSWER},
        ]
    }


def test_sparql_is_removed_from_the_answer():
    messages = hide_sparql_in_answers(_response())["messages"]
    assert messages[-1]["content"] == "Ernst Brenner's spouse was Lina Sturzenegger.\nMore?"


def test_only_assistant_text_is_touched():
    # The user's own message, the tool results and the executed query in the
    # tool call are data, not the answer; API clients may still need them.
    messages = hide_sparql_in_answers(_response())["messages"]
    original = _response()["messages"]
    assert messages[0] == original[0]
    assert messages[1]["tool_calls"] == original[1]["tool_calls"]
    assert messages[2] == original[2]


def test_answer_without_sparql_is_unchanged():
    response = {"messages": [{"type": "ai", "content": "Results:\n```\nLina\n```"}]}
    assert hide_sparql_in_answers(response)["messages"][0]["content"] == "Results:\n```\nLina\n```"
