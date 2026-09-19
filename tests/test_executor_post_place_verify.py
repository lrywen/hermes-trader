"""Characterization tests for the S13-15 post-place verification leaf.

``_verify_post_placement`` was extracted verbatim from the tail of
``maybe_execute`` (P0-6 best-effort reconciliation) in the P1-1 step ③ phase
split. It is a smoke test, never a gate: it never raises, never touches the
entry flock or in-flight markers, and its return value is currently discarded
by the caller (the original inline ``_unverified`` was assigned but never
read). These tests pin the leaf's observable behaviour — the cross-check
call shape, the category=risk alert on mismatch, and the swallow-everything
contract — so the lock-critical tail stays behaviourally frozen.
"""

import pytest

from hermes_trader.agents import executor

verify_leaf = executor._verify_post_placement


@pytest.fixture
def patched_verify(monkeypatch):
    """Patch verify_order_exists at its source module (the leaf does a
    function-local ``from hermes_trader.client.exchange import ...``)."""
    calls = []

    def _fake(coin, oid=None, cloid=None):
        calls.append({"coin": coin, "oid": oid, "cloid": cloid})
        return {"verified": True, "in_open_orders": True,
                "in_user_fills": False, "reason": None}

    monkeypatch.setattr("hermes_trader.client.exchange.verify_order_exists",
                        _fake)
    return calls


@pytest.fixture
def patched_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "hermes_trader.notify.send_text",
        lambda msg, category=None: sent.append(
            {"msg": msg, "category": category}))
    return sent


def test_verified_returns_false_and_stays_silent(patched_verify, patched_notify):
    unverified = verify_leaf(
        order_res={"order_id": "0xabc", "cloid": "42"}, coin="BTC")
    assert unverified is False
    assert patched_verify == [
        {"coin": "BTC", "oid": "0xabc", "cloid": "42"}]
    assert patched_notify == []


def test_not_verified_returns_true_and_alerts_risk(patched_verify,
                                                   patched_notify,
                                                   monkeypatch):
    monkeypatch.setattr(
        "hermes_trader.client.exchange.verify_order_exists",
        lambda coin, oid=None, cloid=None:
        {"verified": False, "reason": "not_found_open_or_fills"})

    unverified = verify_leaf(
        order_res={"order_id": "0xdead", "cloid": "7"}, coin="ETH")

    assert unverified is True
    assert len(patched_notify) == 1
    alert = patched_notify[0]
    assert alert["category"] == "risk"
    assert "ETH" in alert["msg"]
    assert "0xdead" in alert["msg"]
    assert "7" in alert["msg"]


def test_missing_verified_key_defaults_to_verified(patched_verify,
                                                   patched_notify,
                                                   monkeypatch):
    # .get("verified", True): a shaped response without the key is treated
    # as verified (fail-open for the smoke test; the main path is unaffected).
    monkeypatch.setattr(
        "hermes_trader.client.exchange.verify_order_exists",
        lambda coin, oid=None, cloid=None: {"reason": None})
    assert verify_leaf(order_res={"order_id": "1"}, coin="SOL") is False
    assert patched_notify == []


def test_no_oid_and_no_cloid_skips_exchange_call(patched_verify,
                                                 patched_notify):
    assert verify_leaf(order_res={}, coin="DOGE") is False
    assert verify_leaf(order_res={"order_id": "", "cloid": ""},
                       coin="DOGE") is False
    assert patched_verify == []
    assert patched_notify == []


def test_cloid_keyword_fallback_when_order_res_lacks_cloid(patched_verify):
    verify_leaf(order_res={"order_id": "0x9"}, coin="XRP",
                cloid=123456789)
    assert patched_verify == [
        {"coin": "XRP", "oid": "0x9", "cloid": "123456789"}]


def test_order_res_cloid_takes_precedence_over_keyword(patched_verify):
    verify_leaf(order_res={"order_id": "0x9", "cloid": "111"},
                coin="XRP", cloid="222")
    assert patched_verify[0]["cloid"] == "111"


def test_oid_only_still_cross_checks(patched_verify):
    verify_leaf(order_res={"order_id": "0x55"}, coin="ARB")
    assert patched_verify == [
        {"coin": "ARB", "oid": "0x55", "cloid": None}]


def test_verify_exception_is_swallowed_and_returns_false(patched_notify,
                                                         monkeypatch):
    def _boom(coin, oid=None, cloid=None):
        raise RuntimeError("exchange endpoint down")

    monkeypatch.setattr(
        "hermes_trader.client.exchange.verify_order_exists", _boom)
    # Must never raise into the order tail.
    assert verify_leaf(
        order_res={"order_id": "0x1", "cloid": "2"}, coin="SUI") is False
    assert patched_notify == []


def test_notify_exception_still_returns_true(patched_verify, monkeypatch):
    monkeypatch.setattr(
        "hermes_trader.client.exchange.verify_order_exists",
        lambda coin, oid=None, cloid=None:
        {"verified": False, "reason": "missing"})

    def _alert_boom(msg, category=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("hermes_trader.notify.send_text", _alert_boom)
    # The alert failure is logged but the unverified verdict stands.
    assert verify_leaf(
        order_res={"order_id": "0x1", "cloid": "2"}, coin="AVAX") is True


def test_leaf_is_read_only_on_order_res(patched_verify):
    order_res = {"order_id": "0x77", "cloid": "99"}
    import copy
    before = copy.deepcopy(order_res)
    verify_leaf(order_res=order_res, coin="LINK")
    assert order_res == before
