"""Where the server may connect when a user or curator chose the URL.

Every address a name resolves to must be public, unless the operator named that
``host:port`` in ``OUTBOUND_ALLOW``. The check sits in the connection layer and the
connection goes to the checked address, so it holds on every redirect hop and every new
connection, and a name that changes between the check and the connect cannot get round it.

``OUTBOUND_ALLOW`` is read only from the process environment: nothing saved in the UI or
in ``project.json`` can widen it. This module must not import ``sparql_llm.utils`` or
``sparql_llm.config``: ``utils`` imports it.
"""

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
from rdflib.plugins.sparql import CUSTOM_EVALS

logger = logging.getLogger("sparql_llm.outbound")

REFUSED_MESSAGE = "This address isn't allowed on this instance because it points to a private network. Ask whoever runs it."
CHAT_REFUSED_MESSAGE = (
    "The address saved in your [Settings](/api-settings) isn't allowed on this instance "
    "because it points to a private network. Change it there, or ask whoever runs this instance."
)

#: httpx has no public way to pass a network backend, so it is set on the transport's pool.
_BACKEND_ATTRIBUTE = "_network_backend"

#: Python's ``is_global`` lets these through: NAT64 and 6to4 embed any IPv4 address, site-local is deprecated.
_NOT_PUBLIC_IPV6 = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("fec0::/10"),
)


class DestinationRefused(Exception):
    """The outbound policy refused this destination. No connection was made.

    Not an httpx error on purpose: code that tolerates network failures must not swallow it.
    """

    def __init__(self, host: str, port: int):
        super().__init__(f"{host}:{port} is not an allowed destination")
        self.host = host
        self.port = port


def is_public_address(address: str) -> bool:
    """Whether an IP address is on the public internet."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    return not (isinstance(ip, ipaddress.IPv6Address) and any(ip in network for network in _NOT_PUBLIC_IPV6))


def allowed_destinations() -> set[tuple[str, int]]:
    """The ``(host, port)`` pairs the operator opened in ``OUTBOUND_ALLOW``. A malformed entry is ignored."""
    allowed = set()
    for entry in os.getenv("OUTBOUND_ALLOW", "").split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        host, _, port = entry.rpartition(":")
        host = host.strip("[]")
        if not host or not port.isascii() or not port.isdigit():
            logger.warning("OUTBOUND_ALLOW: ignoring %r, expected host:port", entry)
            continue
        allowed.add((host, int(port)))
    return allowed


def checked_addresses(host: str, port: int, purpose: str) -> list[str]:
    """Resolve ``host`` and return the addresses that may be connected to, or raise ``DestinationRefused``."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise httpcore.ConnectError(str(exc)) from exc
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if (host.lower(), port) in allowed_destinations() or all(is_public_address(a) for a in addresses):
        return addresses
    logger.warning("Outbound connection refused: %s:%s (%s)", host, port, purpose)
    raise DestinationRefused(host, port)


class _GuardedBackend(httpcore.NetworkBackend):
    """Connects only to checked addresses, each in turn. The host name stays with httpcore for TLS."""

    def __init__(self, purpose: str):
        self._purpose = purpose
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        failure: Exception | None = None
        for address in checked_addresses(host, port, self._purpose):
            try:
                return self._backend.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None  # getaddrinfo answers at least one address or raises
        raise failure

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _AsyncGuardedBackend(httpcore.AsyncNetworkBackend):
    """The same for async clients. The lookup runs in a thread, bounded by the connect timeout."""

    def __init__(self, purpose: str):
        self._purpose = purpose
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(checked_addresses, host, port, self._purpose), timeout
            )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"looking up {host} took too long") from exc
        failure: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None  # getaddrinfo answers at least one address or raises
        raise failure

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _no_remote_service(ctx: Any, part: Any) -> Any:
    """Answer a SPARQL ``SERVICE`` clause in a locally run query with no rows.

    rdflib would run it itself through urllib, outside the guard. Local queries read the
    Turtle an endpoint serves, and the text of such a query can hold what that endpoint chose.
    """
    if part.name != "ServiceGraphPattern":
        raise NotImplementedError  # rdflib evaluates everything else as usual
    logger.warning("Outbound connection refused: SERVICE in a locally run SPARQL query")
    return iter(())


CUSTOM_EVALS["sparql_llm_no_remote_service"] = _no_remote_service


def _install_backend(transport: Any, backend: Any) -> None:
    """Put ``backend`` on the transport's pool. Without that the client would be unguarded, so refuse to build it."""
    pool = getattr(transport, "_pool", None)
    if not hasattr(pool, _BACKEND_ATTRIBUTE):
        raise RuntimeError("httpx/httpcore changed: the outbound guard cannot be installed")
    setattr(pool, _BACKEND_ATTRIBUTE, backend)


def guarded_client(purpose: str, **kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` under the outbound policy. ``purpose`` names the fetch in the log.

    ``trust_env=False``: otherwise httpx mounts a proxy transport from ``HTTPS_PROXY`` and
    the guard is skipped.
    """
    transport = httpx.HTTPTransport()
    _install_backend(transport, _GuardedBackend(purpose))
    return httpx.Client(transport=transport, trust_env=False, **kwargs)


def guarded_async_client(purpose: str, **kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` under the outbound policy, see :func:`guarded_client`."""
    transport = httpx.AsyncHTTPTransport()
    _install_backend(transport, _AsyncGuardedBackend(purpose))
    return httpx.AsyncClient(transport=transport, trust_env=False, **kwargs)


def find_refusal(exc: BaseException) -> DestinationRefused | None:
    """The refusal behind ``exc``, if any: the OpenAI SDK wraps every exception in its own."""
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, DestinationRefused):
            return current
        pending += [current.__context__, current.__cause__]
    return None


def remote_json_ld_contexts(document: object) -> set[str]:
    """Every context a JSON-LD processor would fetch for ``document``, at any depth."""
    found: set[str] = set()

    def in_context(value: object) -> None:
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, list):
            for item in value:
                in_context(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "@import":
                    if isinstance(item, str):
                        found.add(item)
                elif key == "@context":
                    in_context(item)
                else:
                    # A term definition: only a scoped context inside it is fetched.
                    walk(item)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "@context":
                    in_context(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return found
