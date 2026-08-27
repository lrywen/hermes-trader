"""Tests for the three-webhook Feishu notify routing and HMAC signing.

Covers:
  * _sign() produces a valid HMAC-SHA256 base64 digest per Feishu spec.
  * _resolve_webhook() routes categories to the correct webhook pair.
  * signal/ai categories fall back to primary when signal webhook unset.
  * non-trade categories fall back to primary when non-trade webhook unset.
  * is_enabled() honours the category allow-list and configured URL.
  * send_card() POSTs to the resolved webhook and injects a valid signature.
  * notify_dispatch.dispatch() tags intercepted opens as "signal" and
    AI research verdicts as "ai" (which routes to the signal webhook).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
from unittest import mock

import pytest

from hermes_trader import notify
from hermes_trader import notify_dispatch


PRIMARY_URL = "https://open.feishu.cn/hook/PRIMARY"
PRIMARY_SECRET = "primary-secret"
SIGNAL_URL = "https://open.feishu.cn/hook/SIGNAL"
SIGNAL_SECRET = "signal-secret"
NONTRADE_URL = "https://open.feishu.cn/hook/NONTRADE"
NONTRADE_SECRET = "nontrade-secret"


@pytest.fixture
def three_webhooks(monkeypatch):
    """Configure all three webhook pairs and a permissive allow-list."""
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_URL", PRIMARY_URL)
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_SECRET", PRIMARY_SECRET)
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_URL", SIGNAL_URL)
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_SECRET", SIGNAL_SECRET)
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_URL", NONTRADE_URL)
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_SECRET", NONTRADE_SECRET)
    monkeypatch.setattr(
        notify, "ENABLED_CATEGORIES",
        {"trade", "signal", "risk", "system", "ai", "surge", "report"},
    )
    # Clear throttle state so dedup never leaks between tests.
    monkeypatch.setattr(notify, "_last_sent", {})
    yield


@pytest.fixture
def primary_only(monkeypatch):
    """Only the primary webhook is configured (single-bot deployment)."""
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_URL", PRIMARY_URL)
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_SECRET", PRIMARY_SECRET)
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_URL", "")
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_SECRET", "")
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_URL", "")
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        notify, "ENABLED_CATEGORIES",
        {"trade", "signal", "risk", "system", "ai", "surge", "report"},
    )
    monkeypatch.setattr(notify, "_last_sent", {})
    yield


# --------------------------------------------------------------------------- #
# Signature
# --------------------------------------------------------------------------- #

def test_sign_matches_feishu_spec():
    """string_to_sign = "{ts}\\n{secret}"; HMAC-SHA256 then base64."""
    ts = 1700000000
    secret = "AdiNKBg74IdJsOvayOHGzf"
    expected = base64.b64encode(
        hmac.new(f"{ts}\n{secret}".encode("utf-8"),
                 digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    assert notify._sign(secret, ts) == expected


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def test_resolve_trade_to_primary(three_webhooks):
    assert notify._resolve_webhook("trade") == (PRIMARY_URL, PRIMARY_SECRET)


def test_resolve_signal_to_signal_webhook(three_webhooks):
    assert notify._resolve_webhook("signal") == (SIGNAL_URL, SIGNAL_SECRET)


def test_resolve_ai_alias_to_signal_webhook(three_webhooks):
    """The legacy 'ai' category must route identically to 'signal'."""
    assert notify._resolve_webhook("ai") == (SIGNAL_URL, SIGNAL_SECRET)


@pytest.mark.parametrize("cat", ["risk", "system", "surge", "report"])
def test_resolve_nontrade_to_nontrade_webhook(three_webhooks, cat):
    assert notify._resolve_webhook(cat) == (NONTRADE_URL, NONTRADE_SECRET)


def test_resolve_is_case_insensitive_and_strips(three_webhooks):
    assert notify._resolve_webhook("  SIGNAL ")[0] == SIGNAL_URL
    assert notify._resolve_webhook("Trade")[0] == PRIMARY_URL
    assert notify._resolve_webhook("RISK")[0] == NONTRADE_URL


def test_signal_falls_back_to_primary(primary_only):
    assert notify._resolve_webhook("signal") == (PRIMARY_URL, PRIMARY_SECRET)
    assert notify._resolve_webhook("ai") == (PRIMARY_URL, PRIMARY_SECRET)


def test_nontrade_falls_back_to_primary(primary_only):
    assert notify._resolve_webhook("risk") == (PRIMARY_URL, PRIMARY_SECRET)
    assert notify._resolve_webhook("report") == (PRIMARY_URL, PRIMARY_SECRET)


# --------------------------------------------------------------------------- #
# Enable gate
# --------------------------------------------------------------------------- #

def test_is_enabled_requires_url_and_allowlist(three_webhooks, monkeypatch):
    assert notify.is_enabled("trade") is True
    assert notify.is_enabled("signal") is True
    # Disabled category.
    monkeypatch.setattr(notify, "ENABLED_CATEGORIES", {"trade"})
    assert notify.is_enabled("signal") is False
    assert notify.is_enabled("risk") is False
    # No URL at all.
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_URL", "")
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_URL", "")
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_URL", "")
    assert notify.is_enabled("trade") is False


# --------------------------------------------------------------------------- #
# send_card delivery + signature injection
# --------------------------------------------------------------------------- #

def _fake_urlopen(payload: dict):
    """Return a mock urlopen that captures the request and replies code=0."""
    captured = {}

    def fake(urlopen_req, timeout=None):
        captured["url"] = urlopen_req.full_url
        captured["body"] = json.loads(urlopen_req.data.decode("utf-8"))
        captured["timeout"] = timeout
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"code": 0}).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    return fake, captured


def test_send_card_trade_posts_to_primary_with_signature(three_webhooks):
    fake, captured = _fake_urlopen({})
    with mock.patch.object(notify.urllib.request, "urlopen", side_effect=fake):
        ok = notify.send_card("开仓成交 — BTC", fields={"币种": "BTC"},
                              category="trade", level="success")
    assert ok is True
    assert captured["url"] == PRIMARY_URL
    body = captured["body"]
    assert body["msg_type"] == "interactive"
    # Signature injected and verifies against the primary secret.
    assert "timestamp" in body and "sign" in body
    expected_sig = notify._sign(PRIMARY_SECRET, int(body["timestamp"]))
    assert hmac.compare_digest(body["sign"], expected_sig)


def test_send_card_signal_posts_to_signal_webhook(three_webhooks):
    fake, captured = _fake_urlopen({})
    with mock.patch.object(notify.urllib.request, "urlopen", side_effect=fake):
        ok = notify.send_card("开仓被风控拦截 — ETH",
                              fields={"币种": "ETH", "拦截原因": "风控"},
                              category="signal", level="warn")
    assert ok is True
    assert captured["url"] == SIGNAL_URL
    body = captured["body"]
    expected_sig = notify._sign(SIGNAL_SECRET, int(body["timestamp"]))
    assert hmac.compare_digest(body["sign"], expected_sig)


def test_send_card_ai_posts_to_signal_webhook(three_webhooks):
    fake, captured = _fake_urlopen({})
    with mock.patch.object(notify.urllib.request, "urlopen", side_effect=fake):
        notify.send_card("AI 决策 — 做多 SOL",
                         fields={"币种": "SOL", "结论": "做多"},
                         category="ai", level="info")
    assert captured["url"] == SIGNAL_URL
    body = captured["body"]
    expected_sig = notify._sign(SIGNAL_SECRET, int(body["timestamp"]))
    assert hmac.compare_digest(body["sign"], expected_sig)


def test_send_card_no_signature_when_secret_empty(monkeypatch):
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_URL", PRIMARY_URL)
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_SECRET", "")
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_URL", "")
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_URL", "")
    monkeypatch.setattr(notify, "ENABLED_CATEGORIES", {"trade", "signal"})
    monkeypatch.setattr(notify, "_last_sent", {})

    fake, captured = _fake_urlopen({})
    with mock.patch.object(notify.urllib.request, "urlopen", side_effect=fake):
        notify.send_card("no-secret card", category="trade")
    assert captured["url"] == PRIMARY_URL
    assert "sign" not in captured["body"]
    assert "timestamp" not in captured["body"]


def test_send_card_returns_false_when_disabled(three_webhooks, monkeypatch):
    monkeypatch.setattr(notify, "ENABLED_CATEGORIES", set())
    # urlopen must not be called.
    with mock.patch.object(notify.urllib.request, "urlopen") as m:
        assert notify.send_card("x", category="trade") is False
        m.assert_not_called()


def test_send_card_never_raises_on_network_error(three_webhooks):
    def boom(*_a, **_k):
        raise OSError("connection refused")
    with mock.patch.object(notify.urllib.request, "urlopen", side_effect=boom):
        # Must not raise; returns False.
        assert notify.send_card("x", category="trade") is False


def test_send_card_throttles_dedup_key(three_webhooks):
    fake, captured = _fake_urlopen({})
    with mock.patch.object(notify.urllib.request, "urlopen", side_effect=fake):
        first = notify.send_card("开仓被风控拦截 — BTC", category="signal",
                                 dedup_key="blocked:BTC")
        second = notify.send_card("开仓被风控拦截 — BTC", category="signal",
                                  dedup_key="blocked:BTC")
    assert first is True
    assert second is False  # throttled


# --------------------------------------------------------------------------- #
# notify_dispatch event -> category mapping
# --------------------------------------------------------------------------- #

def test_dispatch_intercepted_open_uses_signal_category():
    record = {
        "event": "execute",
        "coin": "BTC",
        "side": "LONG",
        "executed": False,
        "blocked_by": ["daily_loss", "exposure"],
        "detail": "风控拦截",
    }
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    m.assert_called_once()
    assert m.call_args.kwargs["category"] == "signal"
    assert "拦截" in m.call_args.args[0]


def test_dispatch_executed_open_uses_trade_category():
    record = {
        "event": "execute",
        "coin": "ETH",
        "side": "LONG",
        "executed": True,
        "size_usd": 1000.0,
        "entry_px": 3000.0,
        "stop_px": 2900.0,
        "tp_px": 3300.0,
        "regime": "trend",
    }
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    assert m.call_args.kwargs["category"] == "trade"


def test_dispatch_research_long_uses_ai_category():
    record = {
        "event": "research",
        "coin": "SOL",
        "verdict": "LONG",
        "confidence": 78,
        "news_risk": "none",
        "reasoning": "strong momentum",
    }
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    assert m.call_args.kwargs["category"] == "ai"
    assert "做多" in m.call_args.args[0]


def test_dispatch_research_hold_is_suppressed():
    record = {"event": "research", "coin": "SOL", "verdict": "HOLD"}
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    m.assert_not_called()


def test_dispatch_ai_close_uses_trade_category():
    record = {"event": "ai_close", "coin": "BTC", "executed": True,
              "detail": "close ok", "reasoning": "target hit"}
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    assert m.call_args.kwargs["category"] == "trade"


def test_dispatch_dsl_exit_uses_trade_category():
    record = {"event": "dsl_exit", "coin": "ETH", "side": "SHORT",
              "leverage": 3, "reason": "stop", "realized_pnl_pct": -1.2}
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    assert m.call_args.kwargs["category"] == "trade"


def test_dispatch_killswitch_uses_risk_category():
    record = {"event": "hard_killswitch", "daily_pnl": -55.0,
              "limit": 50, "flattened": ["BTC", "ETH"]}
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    assert m.call_args.kwargs["category"] == "risk"


def test_dispatch_killswitch_none_fields_does_not_raise():
    """P1-17: a killswitch record with None numerics must not crash card
    rendering (the f-string fields). Previously the TypeError was swallowed at
    debug level and the high-severity alert silently vanished. None renders as
    $0 and the card still goes out under the risk category."""
    record = {"event": "hard_killswitch", "daily_pnl": None,
              "limit": None, "flattened": None}
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        # Must not raise.
        notify_dispatch.dispatch(record)
    m.assert_called_once()
    assert m.call_args.kwargs["category"] == "risk"
    fields = m.call_args.kwargs.get("fields") or {}
    # None coerced to 0 in the money f-strings.
    assert "$0.00" in fields.get("当日盈亏", "")
    assert "$0" in fields.get("亏损上限", "")
