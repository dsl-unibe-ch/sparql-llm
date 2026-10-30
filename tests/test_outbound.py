"""The outbound guard: a user-chosen URL never reaches a private address, however it gets there."""

import asyncio
import logging
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from sparql_llm import outbound
from sparql_llm.outbound import (
    DestinationRefused,
    allowed_destinations,
    checked_addresses,
    find_refusal,
    guarded_async_client,
    guarded_client,
    is_public_address,
    remote_json_ld_contexts,
)


@pytest.fixture(autouse=True)
def no_allow_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OUTBOUND_ALLOW", raising=False)


@pytest.fixture
def local_server() -> Iterator[SimpleNamespace]:
    """A plain HTTP server on loopback that counts requests; /redirect answers a 302 to the metadata address."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield SimpleNamespace(port=port, url=f"http://127.0.0.1:{port}", seen=seen)
    server.shutdown()
    server.server_close()
    thread.join()


def _resolves_to(monkeypatch: pytest.MonkeyPatch, *answers: list[str]) -> None:
    """Stub DNS: each lookup of a name takes the next list of addresses. The sockets' own lookup of an IP passes."""
    remaining = list(answers)
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host[0].isdigit():
            return real_getaddrinfo(host, port, *args, **kwargs)
        addresses = remaining.pop(0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fec0::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::7f00:1",
        "64:ff9b::a00:1",
        "2002:a00:1::",
    ],
)
def test_not_public(address):
    assert not is_public_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "130.92.1.86", "157.180.63.171", "2a01:4f9:3100:4a60::2"])
def test_public(address):
    assert is_public_address(address)


def test_allow_list_parses_host_and_port(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_ALLOW", " Host.Docker.Internal:11434 , [::1]:8080")
    assert allowed_destinations() == {("host.docker.internal", 11434), ("::1", 8080)}


def test_malformed_allow_entries_are_ignored(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setenv("OUTBOUND_ALLOW", "nohost,example.org:notaport")
    with caplog.at_level(logging.WARNING, logger="sparql_llm.outbound"):
        assert allowed_destinations() == set()
    assert len(caplog.records) == 2


def test_all_addresses_must_be_public(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _resolves_to(monkeypatch, ["8.8.8.8", "10.0.0.1"])
    with caplog.at_level(logging.WARNING, logger="sparql_llm.outbound"), pytest.raises(DestinationRefused) as refused:
        checked_addresses("mixed.example", 443, "test-purpose")
    assert (refused.value.host, refused.value.port) == ("mixed.example", 443)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "mixed.example" in message and "443" in message and "test-purpose" in message


def test_a_name_that_changes_to_private_is_refused(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, ["8.8.8.8"], ["10.0.0.1"])
    assert checked_addresses("rebinding.example", 80, "test") == ["8.8.8.8"]
    with pytest.raises(DestinationRefused):
        checked_addresses("rebinding.example", 80, "test")


def test_repeated_addresses_are_tried_once(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, ["8.8.8.8", "8.8.4.4", "8.8.8.8"])
    assert checked_addresses("dup.example", 80, "test") == ["8.8.8.8", "8.8.4.4"]


def test_a_name_that_does_not_resolve_is_a_connect_error(monkeypatch: pytest.MonkeyPatch):
    def no_such_name(*args, **kwargs):
        raise socket.gaierror("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", no_such_name)
    with pytest.raises(httpcore.ConnectError):
        checked_addresses("nothing.example", 80, "test")


def test_guarded_client_refuses_loopback_before_connecting(local_server):
    with guarded_client("test") as client, pytest.raises(DestinationRefused):
        client.get(local_server.url)
    assert local_server.seen == []


def test_guarded_async_client_refuses_loopback(local_server):
    async def fetch():
        async with guarded_async_client("test") as client:
            await client.get(local_server.url)

    with pytest.raises(DestinationRefused):
        asyncio.run(fetch())
    assert local_server.seen == []


def test_allow_list_opens_exactly_that_port(local_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_ALLOW", f"127.0.0.1:{local_server.port}")
    with guarded_client("test") as client:
        assert client.get(local_server.url).status_code == 200
        with pytest.raises(DestinationRefused):
            client.get(f"http://127.0.0.1:{local_server.port + 1}/")


def test_guarded_async_client_reaches_an_allowed_destination(local_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_ALLOW", f"127.0.0.1:{local_server.port}")

    async def fetch() -> int:
        async with guarded_async_client("test") as client:
            return (await client.get(local_server.url)).status_code

    assert asyncio.run(fetch()) == 200


def test_redirect_to_metadata_address_is_refused(local_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_ALLOW", f"127.0.0.1:{local_server.port}")
    with guarded_client("test", follow_redirects=True) as client, pytest.raises(DestinationRefused) as refused:
        client.get(f"{local_server.url}/redirect")
    assert refused.value.host == "169.254.169.254"
    assert local_server.seen == ["/redirect"]


def test_environment_proxy_does_not_skip_the_guard(local_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_ALLOW", f"127.0.0.1:{local_server.port}")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, local_server.url)
    with guarded_client("test") as client, pytest.raises(DestinationRefused):
        client.get("http://127.0.0.1:9/")
    assert local_server.seen == []


def test_second_address_is_tried_after_the_first_fails(local_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_ALLOW", f"localhost:{local_server.port}")
    _resolves_to(monkeypatch, ["127.0.0.2", "127.0.0.1"])
    real_connect = httpcore.SyncBackend.connect_tcp
    tried: list[str] = []

    def connect_tcp(self, host, port, *args, **kwargs):
        tried.append(host)
        if len(tried) == 1:
            raise httpcore.ConnectError("no route")
        return real_connect(self, host, port, *args, **kwargs)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    with guarded_client("test") as client:
        response = client.get(f"http://localhost:{local_server.port}/")
    assert response.status_code == 200
    assert tried == ["127.0.0.2", "127.0.0.1"]


def test_the_last_failure_is_raised_when_no_address_connects(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_ALLOW", "localhost:9")
    _resolves_to(monkeypatch, ["127.0.0.2", "127.0.0.1"])

    def connect_tcp(self, host, port, *args, **kwargs):
        raise httpcore.ConnectError(f"no route to {host}")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    with guarded_client("test") as client, pytest.raises(httpx.ConnectError, match="no route to 127.0.0.1"):
        client.get("http://localhost:9/")


def test_a_missing_pool_backend_stops_construction(monkeypatch: pytest.MonkeyPatch):
    """The backend is set on a private attribute: a library change must fail loudly, never unguarded."""
    monkeypatch.setattr(outbound, "_BACKEND_ATTRIBUTE", "_no_such_attribute")
    with pytest.raises(RuntimeError):
        guarded_client("test")
    with pytest.raises(RuntimeError):
        guarded_async_client("test")


def test_find_refusal_walks_the_exception_chain():
    refusal = DestinationRefused("10.0.0.1", 80)
    try:
        try:
            try:
                raise refusal
            except DestinationRefused as exc:
                raise ValueError("wrapped") from exc
        except ValueError as exc:
            raise RuntimeError("wrapped again") from exc
    except RuntimeError as exc:
        assert find_refusal(exc) is refusal
    assert find_refusal(ValueError("plain")) is None


def test_find_refusal_survives_a_cycle():
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first
    assert find_refusal(first) is None


def test_remote_contexts_found_at_every_depth():
    assert remote_json_ld_contexts({"@context": "https://schema.org"}) == {"https://schema.org"}
    assert remote_json_ld_contexts(
        {
            "@context": ["https://a.example/c", {"@import": "https://b.example/i"}],
            "x": {"@context": "http://c.example/"},
        }
    ) == {"https://a.example/c", "https://b.example/i", "http://c.example/"}
    assert remote_json_ld_contexts([{"y": [{"@context": ["http://d.example/"]}]}]) == {"http://d.example/"}


def test_a_scoped_context_inside_a_term_definition_is_found():
    document = {"@context": {"term": {"@id": "https://schema.org/name", "@context": "http://e.example/"}}}
    assert remote_json_ld_contexts(document) == {"http://e.example/"}


def test_term_definitions_are_not_contexts():
    assert remote_json_ld_contexts({"@context": {"name": "https://schema.org/name"}}) == set()
