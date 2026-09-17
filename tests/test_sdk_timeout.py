"""P0-4 — SDK read-timeout plumbing for the two shared ``Info`` instances.

The pinned Hyperliquid SDK builds ``API`` with ``timeout=None`` and hands
it straight to ``session.post(..., timeout=self.timeout)`` — None means
*wait forever* (hyperliquid/api.py). Both places that construct a shared
``hyperliquid.info.Info`` must instead pin the configured
``hl_client_io.sdk_timeout_s`` so a hung HL endpoint fails LOUD.

Contract pinned here:
  * ``hl_client.init_info()`` constructs the real SDK ``Info`` with the
    configured timeout and the canonical ``HL_API`` base URL.
  * ``ws_client._connect_and_subscribe()`` does the same AND pins
    ``base_url`` (it previously fell back to the SDK mainnet default, a
    testnet foot-gun since the WS manager reads ``info.base_url``).
  * A slow/hung read surfaces as ``requests.exceptions.ReadTimeout`` and
    the timeout value is the one handed to ``Session.post`` — callers
    must never block indefinitely.
  * Constructor failures stay LOUD: ``init_info`` warns and
    ``get_info()`` returns None; the ws path logs an error and re-raises.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import requests

from hermes_trader.client import hl_client, ws_client
from hermes_trader.client.rate_limit import _HL_CLIENT_IO

# Minimal valid-shape meta: the SDK constructor indexes spot_meta["tokens"]
# and iterates meta["universe"]; empty collections skip every loop while
# proving externally-injected meta means zero network during construction.
_PERP_META = {"universe": []}
_SPOT_META = {"tokens": [], "universe": []}


@pytest.fixture(autouse=True)
def _reset_info_instance() -> None:
    """Keep the module-level Info singleton from leaking between tests."""
    hl_client._info_instance = None
    yield
    hl_client._info_instance = None


@pytest.fixture
def bare_ws(monkeypatch: pytest.MonkeyPatch) -> "ws_client.HyperliquidWebSocket":
    """Bare WS client without ``start()`` (no threads, no network)."""
    monkeypatch.setenv("HERMES_WS_HEARTBEAT_S", "3600")
    monkeypatch.setenv("HERMES_WS_SEQ_MAX_BACKWARD", "1024")
    return ws_client.HyperliquidWebSocket()


def _fake_http_post(path: str, payload: dict, timeout: float | None = None):
    """Stand-in for the two weight-20 meta fetches."""
    if payload.get("type") == "spotMeta":
        return _SPOT_META
    return _PERP_META


# ---------------------------------------------------------------------------
# hl_client.init_info
# ---------------------------------------------------------------------------

def test_init_info_passes_configured_timeout_to_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(_HL_CLIENT_IO, "sdk_timeout_s", 7.0)
    monkeypatch.setattr(hl_client, "_fetch_meta_sync", lambda: (_PERP_META, _SPOT_META))

    hl_client.init_info()
    info = hl_client.get_info()

    assert info is not None
    assert info.timeout == 7.0
    assert info.base_url == hl_client.HL_API


def test_slow_read_raises_readtimeout_with_timeout_plumbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung server must become ReadTimeout, not an infinite wait."""
    monkeypatch.setitem(_HL_CLIENT_IO, "sdk_timeout_s", 0.01)
    monkeypatch.setattr(hl_client, "_fetch_meta_sync", lambda: (_PERP_META, _SPOT_META))
    hl_client.init_info()
    info = hl_client.get_info()
    assert info is not None

    captured: dict = {}

    def hanging_post(url, json=None, timeout=None, **kwargs):
        captured["timeout"] = timeout
        # What requests raises once the server misses the read deadline:
        raise requests.exceptions.ReadTimeout(f"read timed out after {timeout}s")

    monkeypatch.setattr(info.session, "post", hanging_post)

    with pytest.raises(requests.exceptions.ReadTimeout):
        info.post("/info", {"type": "meta"})

    assert captured["timeout"] == 0.01


def test_init_info_constructor_failure_is_loud_and_yields_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(hl_client, "_fetch_meta_sync", lambda: (_PERP_META, _SPOT_META))

    import hyperliquid.info as sdk_info

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sdk_info, "Info", boom)

    with caplog.at_level(logging.WARNING):
        hl_client.init_info()

    assert hl_client.get_info() is None
    assert "Failed to create Info" in caplog.text


# ---------------------------------------------------------------------------
# ws_client._connect_and_subscribe
# ---------------------------------------------------------------------------

def test_ws_info_passes_timeout_and_pinned_base_url(
    bare_ws: "ws_client.HyperliquidWebSocket", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_client, "_http_post", _fake_http_post)
    captured: dict = {}

    class FakeInfo:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.base_url = kwargs.get("base_url")
            self.ws_manager = None

        def subscribe(self, *args, **kwargs):
            return 1

    class FakeWsManager:
        def __init__(self, base_url: str):
            self.ws = SimpleNamespace(
                url=f"{base_url}/ws",
                on_open=lambda _w: None,
            )

        def start(self) -> None:
            pass

    monkeypatch.setattr(ws_client, "Info", FakeInfo)
    monkeypatch.setattr(ws_client, "HLSSLOptWebsocketManager", FakeWsManager)

    bare_ws._connect_and_subscribe()

    assert captured["skip_ws"] is True
    assert captured["meta"] == _PERP_META
    assert captured["spot_meta"] == _SPOT_META
    assert captured["timeout"] == float(_HL_CLIENT_IO["sdk_timeout_s"])
    assert captured["base_url"] == hl_client.HL_API


def test_ws_info_constructor_failure_is_loud_and_reraised(
    bare_ws: "ws_client.HyperliquidWebSocket",
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(ws_client, "_http_post", _fake_http_post)

    class BoomInfo:
        def __init__(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(ws_client, "Info", BoomInfo)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="boom"):
            bare_ws._connect_and_subscribe()

    assert "Failed to create Info" in caplog.text
