"""BYOK and chat go through the outbound guard, and a refusal reads as a refusal."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from sparql_llm.agent import main
from sparql_llm.agent.crypto import encrypt_api_key
from sparql_llm.agent.providers import validate_and_list_models
from sparql_llm.agent.utils import chat_async_http_client, chat_http_client, load_chat_model
from sparql_llm.config import Configuration, settings
from sparql_llm.outbound import CHAT_REFUSED_MESSAGE, REFUSED_MESSAGE, DestinationRefused, find_refusal

USER = SimpleNamespace(id=uuid.uuid4(), email="user@example.org", is_superuser=False, is_curator=False, is_active=True)


class _Session:
    """Holds at most one saved row and remembers what was stored."""

    def __init__(self, saved=None):
        self.saved = saved
        self.added: list = []
        self.commits = 0

    async def get(self, *args, **kwargs):
        return self.saved

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def no_allow_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OUTBOUND_ALLOW", raising=False)


@pytest.fixture
def client_with(monkeypatch: pytest.MonkeyPatch):
    def build(session: _Session) -> TestClient:
        main.app.dependency_overrides[main.current_active_user] = lambda: USER
        main.app.dependency_overrides[main.get_async_session] = lambda: session
        return TestClient(main.app)

    yield build
    main.app.dependency_overrides.clear()


def test_validate_refuses_loopback():
    with pytest.raises(DestinationRefused):
        asyncio.run(validate_and_list_models("http://127.0.0.1:9/v1", "k", "custom"))


def test_saving_a_private_url_is_refused_and_saves_nothing(client_with):
    session = _Session()
    response = client_with(session).put(
        "/llm-config", json={"provider": "custom", "api_key": "k", "base_url": "http://169.254.169.254/v1"}
    )
    assert response.status_code == 422
    assert response.json() == {"error": REFUSED_MESSAGE}
    assert session.added == [] and session.commits == 0
    assert "169.254" not in response.text and "Traceback" not in response.text


def test_model_lists_report_a_saved_url_that_is_now_refused(client_with):
    saved = SimpleNamespace(
        provider="custom",
        encrypted_api_key=encrypt_api_key("k", settings.auth_secret),
        base_url="http://127.0.0.1:9/v1",
    )
    client = client_with(_Session(saved))
    for route in ("/llm-config/models", "/models"):
        response = client.get(route)
        assert response.status_code == 422, route
        assert response.json() == {"error": REFUSED_MESSAGE, "needs_setup": True}


@pytest.mark.parametrize("model", ["openai/m", "gpustack/m", "gpustack/gpt-oss-m", "custom/m", "openrouter/m"])
def test_every_chat_client_is_guarded(model, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    chat = load_chat_model(Configuration(model=model, llm_base_url="http://x.example/v1", llm_api_key="k"))
    assert chat.http_client is chat_http_client()
    assert chat.http_async_client is chat_async_http_client()


@pytest.mark.parametrize("model", ["openai/m", "gpustack/m", "gpustack/gpt-oss-m", "custom/m", "openrouter/m"])
def test_an_openai_proxy_setting_does_not_stop_chat_models(model, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENAI_PROXY", "http://proxy.example:3128")
    chat = load_chat_model(Configuration(model=model, llm_base_url="http://x.example/v1", llm_api_key="k"))
    assert chat.http_client is chat_http_client()


def test_the_sdk_keeps_the_refusal_reachable():
    chat = ChatOpenAI(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        api_key=SecretStr("k"),
        max_retries=0,
        http_client=chat_http_client(),
    )
    with pytest.raises(Exception) as caught:
        chat.invoke("hi")
    assert isinstance(find_refusal(caught.value), DestinationRefused)


def test_the_async_sdk_keeps_the_refusal_reachable():
    chat = ChatOpenAI(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        api_key=SecretStr("k"),
        max_retries=0,
        http_async_client=chat_async_http_client(),
    )
    with pytest.raises(Exception) as caught:
        asyncio.run(chat.ainvoke("hi"))
    assert isinstance(find_refusal(caught.value), DestinationRefused)


def _wrapped_refusal() -> openai.APIConnectionError:
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x/"))
    exc.__cause__ = DestinationRefused("127.0.0.1", 9)
    return exc


def test_chat_error_message_explains_a_refusal():
    assert main.chat_error_message(_wrapped_refusal()) == CHAT_REFUSED_MESSAGE


def test_chat_error_message_is_unchanged_for_a_real_timeout():
    exc = openai.APITimeoutError(request=httpx.Request("POST", "http://x/"))
    assert main.chat_error_message(exc) == (
        "The language model did not answer in time. Please try again, or pick another model."
    )


def test_a_refusal_in_tools_mode_does_not_blame_the_model():
    class Graph:
        async def astream(self, *args, **kwargs):
            raise _wrapped_refusal()
            yield

    async def collect() -> list[str]:
        config = {"configurable": {"use_tools": True, "model": "custom/m"}}
        return [chunk async for chunk in main.stream_response({}, config, Graph())]

    payload = json.loads(asyncio.run(collect())[0].removeprefix("data: "))
    assert payload["data"][0]["content"] == f"⚠️ {CHAT_REFUSED_MESSAGE}"
