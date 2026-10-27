"""Public MCP work runs off the event loop, and a query's answer is bounded in bytes and time.

The MCP server has no login and the app runs one worker: a tool that blocked the loop
would stall login, chat and admin for everyone while it waited on the endpoint.
"""

import time

import anyio
import httpx
import pytest

from sparql_llm import mcp_server
from sparql_llm.utils import ResponseTooLarge, query_sparql

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}


def _slow_query(calls: list[dict]):
    def query(q, url, **kw):
        calls.append(kw)
        time.sleep(0.3)
        return {"results": {"bindings": [{"x": {"value": "1"}}]}}

    return query


def test_execute_tool_does_not_block_the_event_loop(use_projects, monkeypatch: pytest.MonkeyPatch):
    use_projects(A)
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server, "validate_sparql", lambda *a: {"fixed_query": None, "errors": []})
    monkeypatch.setattr(mcp_server, "query_sparql", _slow_query(calls))
    mcp = mcp_server.get_mcp_app("a")
    lag: list[float] = []

    start = time.monotonic()

    async def heartbeat():
        # Due 50 ms in: a blocked loop wakes it only when the tool's 300 ms are over.
        await anyio.sleep(0.05)
        lag.append(time.monotonic() - start - 0.05)

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp.call_tool, "execute_sparql_query", {"sparql_query": "SELECT * WHERE { ?s ?p ?o }"})
            tg.start_soon(heartbeat)

    anyio.run(main)
    assert lag and lag[0] < 0.15, f"event loop stalled {lag[0]:.2f}s behind a blocking tool"
    # The public tool asks for a bounded answer, not only a bounded wait.
    assert calls[0].get("max_bytes") and calls[0].get("deadline")


def test_search_tool_does_not_block_the_event_loop(use_projects, monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    use_projects(A)

    def embed_query(texts):
        time.sleep(0.3)
        return [[0.0] for _ in texts]

    monkeypatch.setattr(mcp_server, "embedding_model", SimpleNamespace(embed_query=embed_query))
    monkeypatch.setattr(mcp_server, "qdrant_client", SimpleNamespace(query_points=lambda **kw: SimpleNamespace(points=[]), collection_exists=lambda c: True))
    mcp = mcp_server.get_mcp_app("a")
    lag: list[float] = []

    start = time.monotonic()

    async def heartbeat():
        # Due 50 ms in: a blocked loop wakes it only when the tool's 300 ms are over.
        await anyio.sleep(0.05)
        lag.append(time.monotonic() - start - 0.05)

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp.call_tool, "search_sparql_docs", {"question": "q", "potential_classes": [], "steps": []})
            tg.start_soon(heartbeat)

    anyio.run(main)
    assert lag and lag[0] < 0.15, f"event loop stalled {lag[0]:.2f}s behind a blocking search"


def test_query_sparql_stops_reading_past_the_byte_cap():
    body = b'{"results": {"bindings": [' + b'{"x": {"value": "aaaaaaaaaa"}},' * 1000 + b"{}]}}"
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    with pytest.raises(ResponseTooLarge):
        query_sparql("SELECT * WHERE { ?s ?p ?o }", "https://a.example/sparql", client=client, max_bytes=1000)


def test_query_sparql_gives_up_at_the_deadline_on_a_trickling_answer():
    def trickle():
        for _ in range(50):
            time.sleep(0.02)
            yield b" "

    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=trickle())))
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        query_sparql("SELECT * WHERE { ?s ?p ?o }", "https://a.example/sparql", client=client, deadline=0.2)
    assert time.monotonic() - start < 0.5


def test_query_sparql_still_reads_a_normal_answer_within_limits():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": {"bindings": [{"x": {"value": "1"}}]}}))
    )
    res = query_sparql("SELECT * WHERE { ?s ?p ?o }", "https://a.example/sparql", client=client, max_bytes=10_000, deadline=5)
    assert res["results"]["bindings"] == [{"x": {"value": "1"}}]


def test_pipeline_execution_does_not_block_the_event_loop(use_projects, monkeypatch: pytest.MonkeyPatch):
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableConfig

    from sparql_llm.agent.nodes import validation
    from sparql_llm.agent.state import State

    use_projects(A)
    calls: list[dict] = []
    monkeypatch.setattr(validation, "query_sparql", _slow_query(calls))
    monkeypatch.setattr(validation, "validate_sparql_in_msg", lambda *a, **kw: [
        {"original_query": "SELECT * WHERE { ?s ?p ?o }", "fixed_query": None, "errors": [], "endpoint_url": A["endpoint_url"]}
    ])
    message = AIMessage(content="```sparql\nSELECT * WHERE { ?s ?p ?o }\n```")
    lag: list[float] = []
    start = time.monotonic()

    async def heartbeat():
        await anyio.sleep(0.05)
        lag.append(time.monotonic() - start - 0.05)

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(validation.validate_output, State(messages=[message]), RunnableConfig(configurable={}))
            tg.start_soon(heartbeat)

    anyio.run(main)
    assert lag and lag[0] < 0.15, f"event loop stalled {lag[0]:.2f}s behind the pipeline's query"
    assert calls[0].get("max_bytes") and calls[0].get("deadline")
