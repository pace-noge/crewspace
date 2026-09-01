"""cancel_url helper: safe same-origin back-link for form Cancel actions."""
from __future__ import annotations

from unittest.mock import Mock

from crewspace.api.rendering import cancel_url


def _req(referer: str | None, host: str = "testserver") -> Mock:
    request = Mock()
    request.headers = {"referer": referer} if referer else {}
    request.url = Mock(scheme="http", netloc=host)
    return request


def test_cancel_url_relative_same_origin() -> None:
    assert cancel_url(_req("/channels/chan_general")) == "/channels/chan_general"


def test_cancel_url_absolute_same_origin() -> None:
    assert (
        cancel_url(_req("http://testserver/channels/chan_general"))
        == "/channels/chan_general"
    )


def test_cancel_url_foreign_host_rejected() -> None:
    assert cancel_url(_req("http://evil.example/phish")) == "/management"


def test_cancel_url_missing_referer_falls_back() -> None:
    assert cancel_url(_req(None)) == "/management"


def test_cancel_url_query_preserved() -> None:
    assert (
        cancel_url(_req("/channels/chan_general?compose=1"))
        == "/channels/chan_general?compose=1"
    )