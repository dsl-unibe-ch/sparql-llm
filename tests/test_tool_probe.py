"""The model list never waits long on tool-calling probes (curators' report 2026-11-17).

A hanging gpt-oss made every /models call wait for the 20 s probe timeout, and the
chat's model selector only appears once /models answers.
"""

import asyncio
import time

import httpx
import pytest

from sparql_llm.agent import providers

BASE = "https://llm.example/v1"


@pytest.fixture(autouse=True)
def fresh_probe_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(providers, "_TOOL_SUPPORT_CACHE", {})
    monkeypatch.setattr(providers, "_PROBES_IN_FLIGHT", {})
    monkeypatch.setattr(providers, "_TOOL_PROBE_WAIT", 0.1)


def fake_probe(monkeypatch: pytest.MonkeyPatch, answers: dict[str, bool], delay: dict[str, float]) -> list[str]:
    """Replace the probe request; returns the list of models probed, in call order."""
    calls: list[str] = []

    async def probe(client, base_url, api_key, model_id):
        calls.append(model_id)
        await asyncio.sleep(delay.get(model_id, 0))
        return providers._remember_tool_support(base_url, model_id, answers[model_id], providers._TOOL_SUPPORT_TTL)

    monkeypatch.setattr(providers, "_supports_tool_calling", probe)
    return calls


def test_a_hanging_model_is_listed_without_waiting_for_its_probe(monkeypatch):
    fake_probe(monkeypatch, {"fast": True, "slow": True}, {"slow": 5})

    async def run():
        start = time.monotonic()
        verdicts = await providers._tool_support(BASE, "k", ["fast", "slow"])
        return verdicts, time.monotonic() - start

    verdicts, took = asyncio.run(run())
    assert verdicts == {"fast": True, "slow": True}
    assert took < 1


def test_a_model_that_refuses_tools_is_hidden(monkeypatch):
    fake_probe(monkeypatch, {"tools": True, "no-tools": False}, {})
    assert asyncio.run(providers._tool_support(BASE, "k", ["tools", "no-tools"])) == {"tools": True, "no-tools": False}


def test_page_loads_close_together_share_one_probe(monkeypatch):
    calls = fake_probe(monkeypatch, {"slow": True}, {"slow": 0.3})

    async def run():
        await asyncio.gather(*(providers._tool_support(BASE, "k", ["slow"]) for _ in range(12)))

    asyncio.run(run())
    assert calls == ["slow"]


def test_an_answered_probe_is_not_sent_again(monkeypatch):
    calls = fake_probe(monkeypatch, {"m": True}, {})

    async def run():
        await providers._tool_support(BASE, "k", ["m"])
        await providers._tool_support(BASE, "k", ["m"])

    asyncio.run(run())
    assert calls == ["m"]


def test_a_probe_timeout_counts_as_supported_and_is_not_retried_at_once():
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        raise httpx.ReadTimeout("", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await providers._supports_tool_calling(client, BASE, "k", "gpt-oss")
            second = await providers._supports_tool_calling(client, BASE, "k", "gpt-oss")
        return first, second

    assert asyncio.run(run()) == (True, True)
    assert len(posts) == 1
