"""Characterization tests for the S13-15 H-6 price-divergence veto leaf.

``_price_divergence_veto`` was extracted verbatim from the post-lock tail of
``maybe_execute`` in the P1-1 step ③ phase split. It is a decision/notify
leaf ONLY: it calls the Binance cross-check, sends the category=risk alert,
and returns ``(block, reason, px_check)``. It must never touch the entry
flock or in-flight markers — the caller owns the lock-paired BLOCK exit —
and a cross-check failure must fail open (proceed), never raise.
"""

import pytest

from hermes_trader.agents import executor

veto = executor._price_divergence_veto


@pytest.fixture
def patched_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "hermes_trader.notify.send_text",
        lambda msg, category=None: sent.append(
            {"msg": msg, "category": category}))
    return sent


def _patch_crosscheck(monkeypatch, result=None, raises=None):
    if raises is not None:
        def _boom(coin, mid):
            raise raises
        monkeypatch.setattr(
            "hermes_trader.client.price_crosscheck.crosscheck_price", _boom)
    else:
        monkeypatch.setattr(
            "hermes_trader.client.price_crosscheck.crosscheck_price",
            lambda coin, mid: result)


def test_block_divergence_returns_block_with_risk_alert(monkeypatch,
                                                        patched_notify):
    check = {"ok": False, "checked": True, "action": "block",
             "reason": "gap 3.2%"}
    _patch_crosscheck(monkeypatch, result=check)

    block, reason, px_check = veto(coin="BTC", mid_price=50000.0, aid="A1")

    assert block is True
    assert reason == "gap 3.2%"
    assert px_check is check
    assert len(patched_notify) == 1
    alert = patched_notify[0]
    assert alert["category"] == "risk"
    assert "BTC" in alert["msg"]
    assert "gap 3.2%" in alert["msg"]


def test_missing_reason_defaults_to_price_divergence(monkeypatch,
                                                     patched_notify):
    _patch_crosscheck(
        monkeypatch,
        result={"ok": False, "checked": True, "action": "block"})
    block, reason, _ = veto(coin="ETH", mid_price=3000.0, aid="A2")
    assert block is True
    assert reason == "price divergence"
    assert patched_notify[0]["category"] == "risk"


def test_warn_level_divergence_proceeds_but_alerts(monkeypatch,
                                                   patched_notify):
    check = {"ok": False, "checked": True, "action": "warn",
             "reason": "gap 1.1%"}
    _patch_crosscheck(monkeypatch, result=check)

    block, reason, px_check = veto(coin="SOL", mid_price=150.0, aid="A3")

    # Sub-threshold: never block, but still send the proceeding alert.
    assert block is False
    assert reason == ""
    assert px_check is check
    assert len(patched_notify) == 1
    assert patched_notify[0]["category"] == "risk"
    assert "SOL" in patched_notify[0]["msg"]


def test_checked_ok_proceeds_silently(monkeypatch, patched_notify):
    check = {"ok": True, "checked": True, "action": None, "reason": None}
    _patch_crosscheck(monkeypatch, result=check)
    block, reason, px_check = veto(coin="DOGE", mid_price=0.1, aid="A4")
    assert (block, reason) == (False, "")
    assert px_check is check
    assert patched_notify == []


def test_unchecked_secondary_source_proceeds_silently(monkeypatch,
                                                      patched_notify):
    # Unsupported coin / secondary source unavailable: fail open, no alert.
    check = {"ok": True, "checked": False, "reason": "unsupported"}
    _patch_crosscheck(monkeypatch, result=check)
    block, _, px_check = veto(coin="XYZ", mid_price=1.0, aid="A5")
    assert block is False
    assert px_check is check
    assert patched_notify == []


def test_crosscheck_exception_fails_open(monkeypatch, patched_notify):
    _patch_crosscheck(monkeypatch, raises=RuntimeError("binance down"))
    # Must never raise into the order tail.
    block, reason, px_check = veto(coin="ARB", mid_price=1.2, aid="A6")
    assert block is False
    assert reason == ""
    assert px_check["ok"] is True
    assert px_check["checked"] is False
    assert px_check["reason"].startswith("exception:")
    assert "binance down" in px_check["reason"]
    assert patched_notify == []


def test_block_alert_failure_still_blocks(monkeypatch):
    _patch_crosscheck(
        monkeypatch,
        result={"ok": False, "checked": True, "action": "block",
                "reason": "gap"})

    def _boom(msg, category=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("hermes_trader.notify.send_text", _boom)
    block, reason, _ = veto(coin="AVAX", mid_price=20.0, aid="A7")
    assert block is True
    assert reason == "gap"


def test_warn_alert_failure_still_proceeds(monkeypatch):
    _patch_crosscheck(
        monkeypatch,
        result={"ok": False, "checked": True, "action": "warn",
                "reason": "gap"})

    def _boom(msg, category=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("hermes_trader.notify.send_text", _boom)
    block, _, _ = veto(coin="LINK", mid_price=12.0, aid="A8")
    assert block is False
