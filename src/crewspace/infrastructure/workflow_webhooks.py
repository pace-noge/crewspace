"""Hardened outbound HTTP transport for workflow webhook actions."""
from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit

import httpx2

from .mcp_client import _PinnedNetworkBackend, _is_public_address, _resolve_host


class WebhookResponseTooLarge(ValueError):
    pass


async def validate_webhook_endpoint(
    endpoint: str, *, resolver=_resolve_host,
) -> tuple[str, set[str]]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Workflow webhook URLs must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Workflow webhook URLs must not embed credentials")
    if parsed.fragment:
        raise ValueError("Workflow webhook URLs must not contain fragments")
    if parsed.hostname.lower() == "localhost":
        raise ValueError("Loopback workflow webhook URLs are not allowed")
    try:
        addresses = {str(ipaddress.ip_address(parsed.hostname))}
    except ValueError:
        addresses = await resolver(parsed.hostname)
    if not addresses:
        raise ValueError("Workflow webhook hostname did not resolve")
    if any(not _is_public_address(address) for address in addresses):
        raise ValueError("Workflow webhooks must resolve only to public addresses")
    return endpoint, addresses


class _LimitedByteStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self):
        received = 0
        async for chunk in self._stream:
            received += len(chunk)
            if received > self._limit:
                await self._stream.aclose()
                raise WebhookResponseTooLarge("Workflow webhook response exceeds byte limit")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _LimitedResponseTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self, delegate: httpx2.AsyncBaseTransport, *, max_response_bytes: int,
    ) -> None:
        self._delegate = delegate
        self._max_response_bytes = max_response_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._delegate.handle_async_request(request)
        content_length = response.headers.get("content-length")
        if content_length is not None and int(content_length) > self._max_response_bytes:
            await response.aclose()
            raise WebhookResponseTooLarge("Workflow webhook response exceeds byte limit")
        if not isinstance(response.stream, httpx2.AsyncByteStream):
            await response.aclose()
            raise ValueError("Workflow webhook response did not provide an async stream")
        response.stream = _LimitedByteStream(response.stream, self._max_response_bytes)
        return response

    async def aclose(self) -> None:
        await self._delegate.aclose()


class HardenedWebhookExecutor:
    def __init__(
        self, *, resolver=_resolve_host, max_request_bytes: int = 65_536,
        max_response_bytes: int = 65_536, max_header_bytes: int = 8_192,
        max_timeout_seconds: int = 30,
    ) -> None:
        self._resolver = resolver
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._max_header_bytes = max_header_bytes
        self._max_timeout_seconds = max_timeout_seconds

    async def call(
        self, *, url: str, method: str, body: Any, headers: dict[str, str],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not 1 <= timeout_seconds <= self._max_timeout_seconds:
            raise ValueError(
                "Workflow webhook timeout must be between 1 and "
                f"{self._max_timeout_seconds} seconds"
            )
        endpoint, addresses = await validate_webhook_endpoint(
            url, resolver=self._resolver
        )
        normalized_method = method.upper()
        if normalized_method not in {"POST", "PUT", "PATCH"}:
            raise ValueError("Workflow webhooks support POST, PUT, or PATCH")
        encoded = json.dumps(body, separators=(",", ":")).encode()
        if len(encoded) > self._max_request_bytes:
            raise ValueError("Workflow webhook request exceeds byte limit")
        safe_headers = {"Content-Type": "application/json"}
        for name, value in headers.items():
            if name.lower() in {"host", "content-length", "transfer-encoding"}:
                raise ValueError(f"Workflow webhook header is not allowed: {name}")
            safe_headers[str(name)] = str(value)
        header_bytes = sum(
            len(name.encode()) + len(value.encode())
            for name, value in safe_headers.items()
        )
        if header_bytes > self._max_header_bytes:
            raise ValueError("Workflow webhook headers exceed byte limit")
        transport = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)
        transport._pool._network_backend = _PinnedNetworkBackend(addresses)  # pyright: ignore
        limited = _LimitedResponseTransport(
            transport, max_response_bytes=self._max_response_bytes
        )
        try:
            async with httpx2.AsyncClient(
                follow_redirects=False,
                timeout=timeout_seconds,
                transport=limited,
                trust_env=False,
            ) as client:
                response = await client.request(
                    normalized_method, endpoint, content=encoded, headers=safe_headers
                )
                content = response.content
        except (httpx2.HTTPError, WebhookResponseTooLarge) as exc:
            raise ValueError("Workflow webhook request failed") from exc
        if not 200 <= response.status_code < 300:
            raise ValueError(
                f"Workflow webhook returned HTTP {response.status_code}"
            )
        return {
            "status": response.status_code,
            "body": content.decode(errors="replace"),
        }


def build_workflow_webhook_executor() -> HardenedWebhookExecutor:
    return HardenedWebhookExecutor()
