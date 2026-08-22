"""Security and application-boundary tests for outbound workflow webhooks."""
from __future__ import annotations

import datetime as dt

import pytest


async def test_workflow_webhook_endpoint_requires_public_https_address():
    from crewspace.infrastructure.workflow_webhooks import validate_webhook_endpoint

    async def public(_host: str):
        return {"8.8.8.8"}

    async def private(_host: str):
        return {"10.0.0.5"}

    endpoint, addresses = await validate_webhook_endpoint(
        "https://hooks.example.test/events?source=crewspace", resolver=public
    )
    assert endpoint == "https://hooks.example.test/events?source=crewspace"
    assert addresses == {"8.8.8.8"}

    for unsafe in (
        "http://hooks.example.test/events",
        "https://user:secret@hooks.example.test/events",
        "https://localhost/events",
        "https://127.0.0.1/events",
        "https://[::1]/events",
    ):
        with pytest.raises(ValueError):
            await validate_webhook_endpoint(unsafe, resolver=public)
    with pytest.raises(ValueError):
        await validate_webhook_endpoint(
            "https://hooks.example.test/events", resolver=private
        )


async def test_hardened_webhook_executor_disables_redirects_proxies_and_retries(monkeypatch):
    import httpx2

    from crewspace.infrastructure import workflow_webhooks

    observed = {}

    class Response:
        status_code = 202
        content = b"accepted"

    class Client:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            observed["request"] = (method, url, kwargs)
            return Response()

    monkeypatch.setattr(httpx2, "AsyncClient", Client)

    async def public(_host: str):
        return {"8.8.8.8"}

    result = await workflow_webhooks.HardenedWebhookExecutor(
        resolver=public
    ).call(
        url="https://hooks.example.test/events",
        method="POST",
        body={"ok": True},
        headers={"X-Event": "deploy"},
        timeout_seconds=7,
    )

    assert result == {"status": 202, "body": "accepted"}
    assert observed["follow_redirects"] is False
    assert observed["trust_env"] is False
    assert observed["timeout"] == 7
    transport = observed["transport"]
    assert transport._delegate._pool._network_backend._addresses == ("8.8.8.8",)
    assert observed["request"] == (
        "POST",
        "https://hooks.example.test/events",
        {"content": b'{"ok":true}', "headers": {"Content-Type": "application/json", "X-Event": "deploy"}},
    )


async def test_hardened_webhook_executor_rejects_oversized_request_before_network():
    from crewspace.infrastructure.workflow_webhooks import HardenedWebhookExecutor

    async def public(_host: str):
        return {"8.8.8.8"}

    executor = HardenedWebhookExecutor(resolver=public, max_request_bytes=16)
    with pytest.raises(ValueError, match="request exceeds byte limit"):
        await executor.call(
            url="https://hooks.example.test/events",
            method="POST",
            body={"payload": "x" * 100},
            headers={},
            timeout_seconds=5,
        )


async def test_hardened_webhook_transport_stops_oversized_streamed_response():
    import httpx2

    from crewspace.infrastructure.workflow_webhooks import (
        WebhookResponseTooLarge,
        _LimitedResponseTransport,
    )

    class Chunks(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"1234"
            yield b"5678"

        async def aclose(self):
            self.closed = True

    stream = Chunks()

    class Delegate(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx2.Response(200, stream=stream, request=request)

        async def aclose(self):
            return None

    transport = _LimitedResponseTransport(Delegate(), max_response_bytes=6)
    response = await transport.handle_async_request(
        httpx2.Request("POST", "https://hooks.example.test/events")
    )
    with pytest.raises(WebhookResponseTooLarge):
        await response.aread()
    assert stream.closed is True


async def test_hardened_webhook_executor_bounds_timeout_headers_and_safe_errors(monkeypatch):
    import httpx2

    from crewspace.infrastructure import workflow_webhooks

    async def public(_host: str):
        return {"8.8.8.8"}

    executor = workflow_webhooks.HardenedWebhookExecutor(
        resolver=public, max_header_bytes=16, max_timeout_seconds=30
    )
    with pytest.raises(ValueError, match="timeout must be between 1 and 30 seconds"):
        await executor.call(
            url="https://hooks.example.test/events", method="POST", body={},
            headers={}, timeout_seconds=31,
        )
    with pytest.raises(ValueError, match="headers exceed byte limit"):
        await executor.call(
            url="https://hooks.example.test/events", method="POST", body={},
            headers={"X-Large": "x" * 32}, timeout_seconds=5,
        )

    class Response:
        status_code = 503
        content = b"upstream secret response"

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(httpx2, "AsyncClient", Client)
    executor = workflow_webhooks.HardenedWebhookExecutor(resolver=public)
    with pytest.raises(ValueError) as exc_info:
        await executor.call(
            url="https://hooks.example.test/events?token=raw-secret",
            method="POST", body={}, headers={}, timeout_seconds=5,
        )
    error = str(exc_info.value)
    assert error == "Workflow webhook returned HTTP 503"
    assert "raw-secret" not in error
    assert "upstream secret response" not in error

    class FailingClient(Client):
        async def request(self, *args, **kwargs):
            raise httpx2.ConnectError(
                "failed https://hooks.example.test/events?token=raw-secret"
            )

    monkeypatch.setattr(httpx2, "AsyncClient", FailingClient)
    with pytest.raises(ValueError) as exc_info:
        await executor.call(
            url="https://hooks.example.test/events?token=raw-secret",
            method="POST", body={}, headers={}, timeout_seconds=5,
        )
    assert str(exc_info.value) == "Workflow webhook request failed"
    assert "raw-secret" not in str(exc_info.value)


async def test_workflow_call_webhook_uses_injected_executor(client, app):
    from crewspace.application.workflows import WorkflowService

    calls = []

    class Executor:
        async def call(self, **kwargs):
            calls.append(kwargs)
            return {"status": 204, "body": ""}

    payload = {
        "name": "injected_hook",
        "channel_id": "chan_general",
        "enabled": True,
        "trigger_type": "message_posted",
        "trigger_config": {},
        "filter_expression": None,
        "steps": [{
            "id": "hook",
            "action": "call_webhook",
            "timeout_seconds": 9,
            "config": {
                "url": "https://hooks.example.test/events/{{trigger.author_id}}",
                "method": "POST",
                "headers": {"X-Workflow": "{{workflow.name}}"},
                "body": {"text": "{{trigger.text}}"},
            },
        }],
    }
    workflow_id = client.post("/workflows", json=payload).json()["id"]

    async with app.state.db.uow() as uow:
        workflow = await uow.workflows.get(workflow_id)
        assert workflow is not None
        run = await WorkflowService(webhook_executor=Executor()).run(
            workflow,
            uow,
            {"author_id": "user_bilal", "text": "deploy"},
        )

    assert run.status.value == "succeeded"
    assert calls == [{
        "url": "https://hooks.example.test/events/user_bilal",
        "method": "POST",
        "body": {"text": "deploy"},
        "headers": {"X-Workflow": "injected_hook"},
        "timeout_seconds": 9,
    }]
