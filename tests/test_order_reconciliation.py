"""P0-6 regression: response-shape hardening + post-place reconciliation.

Two distinct surfaces:

1. _oid_is_valid: place_hl_order used to claim success on {"ok": True, ...}
   even when the SDK returned no usable oid. That left the position on the
   exchange with no handle to cancel / reconcile — the textbook orphan.
   Now an invalid oid (None, "", "0", "None", "null", non-numeric, or
   non-positive) flips the result to {"ok": False, error="order_id_missing:..."}.

2. verify_order_exists: after a successful place, cross-check openOrders +
   userFills for the (oid, cloid). If neither confirms, return
   {"verified": False, ...} so the caller can alert + flag unverified=True.
   The verifier must NEVER raise; a /info outage is best-effort, the order
   is already submitted.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# _oid_is_valid
# ---------------------------------------------------------------------------

class TestOidIsValid:
    def test_none_rejected(self):
        from hermes_trader.client.exchange import _oid_is_valid
        assert _oid_is_valid(None) is False

    def test_empty_string_rejected(self):
        from hermes_trader.client.exchange import _oid_is_valid
        assert _oid_is_valid("") is False
        assert _oid_is_valid("   ") is False

    def test_zero_rejected(self):
        from hermes_trader.client.exchange import _oid_is_valid
        assert _oid_is_valid("0") is False
        assert _oid_is_valid(0) is False

    def test_text_placeholders_rejected(self):
        from hermes_trader.client.exchange import _oid_is_valid
        for bad in ("None", "null", "false", "FALSE", "NaN"):
            assert _oid_is_valid(bad) is False, f"placeholder {bad!r} should reject"

    def test_non_numeric_rejected(self):
        from hermes_trader.client.exchange import _oid_is_valid
        for bad in ("abc", "12a", "1.5", "0x123", "true", "[]"):
            assert _oid_is_valid(bad) is False, f"non-numeric {bad!r} should reject"

    def test_positive_integers_accepted(self):
        from hermes_trader.client.exchange import _oid_is_valid
        for good in ("1", "12345", "9999999999"):
            assert _oid_is_valid(good) is True, f"oid {good!r} should accept"

    def test_negative_rejected(self):
        from hermes_trader.client.exchange import _oid_is_valid
        # HL oids are positive; a leading sign with negative int is a red flag.
        assert _oid_is_valid("-1") is False

    def test_plus_prefix_accepted(self):
        from hermes_trader.client.exchange import _oid_is_valid
        # Defensive: SDK might someday prepend +. Not exercised in practice,
        # but we promised the docstring allows it.
        assert _oid_is_valid("+42") is True


# ---------------------------------------------------------------------------
# _parse_order_result — response-shape hardening
# ---------------------------------------------------------------------------

class TestParseOrderResult:
    def test_filled_with_valid_oid_passes(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": "12345", "avgPx": "100.5", "totalSz": "0.5"}}]}}}
        out = _parse_order_result(r)
        assert out["ok"] is True
        assert out["order_id"] == "12345"
        assert out["avg_px"] == 100.5
        assert out["total_sz"] == 0.5

    def test_filled_with_missing_oid_fails(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"avgPx": "100.5", "totalSz": "0.5"}}]}}}
        out = _parse_order_result(r)
        assert out["ok"] is False
        assert "order_id_missing" in out["error"]

    def test_filled_with_zero_oid_fails(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": "0", "avgPx": "100"}}]}}}
        out = _parse_order_result(r)
        assert out["ok"] is False
        assert "order_id_missing" in out["error"]

    def test_filled_with_none_oid_fails(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": None, "avgPx": "100"}}]}}}
        out = _parse_order_result(r)
        assert out["ok"] is False
        assert "order_id_missing" in out["error"]

    def test_resting_with_valid_oid_passes(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": "99999"}}]}}}
        out = _parse_order_result(r, accept_resting=True)
        assert out["ok"] is True
        assert out["order_id"] == "99999"

    def test_resting_with_missing_oid_fails(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}}
        out = _parse_order_result(r, accept_resting=True)
        assert out["ok"] is False
        assert "order_id_missing" in out["error"]

    def test_error_status_still_fails_normally(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": [{"error": "minTradeNtlRejected"}]}}}
        out = _parse_order_result(r)
        assert out["ok"] is False
        assert "minTradeNtl" in str(out.get("error", ""))

    def test_empty_statuses_still_fails(self):
        from hermes_trader.client.exchange import _parse_order_result
        r = {"status": "ok", "response": {"data": {"statuses": []}}}
        out = _parse_order_result(r)
        assert out["ok"] is False
        assert "no order status" in out["error"]


# ---------------------------------------------------------------------------
# verify_order_exists — post-place reconciliation
# ---------------------------------------------------------------------------

def _patch_info(monkeypatch, hl_client, *, open_orders=None, fills=None):
    """Stub _http_post('/info', ...) to return our canned payloads.

    `verify_order_exists` lives in `hermes_trader.client.exchange` and uses
    the LOCAL name `_http_post` it imported from `hl_client` — patching
    `hl_client._http_post` alone does NOT reach the caller. Patch BOTH
    attributes (and resolve_user_address) to keep these tests hermetic.
    """
    from hermes_trader.client import exchange
    open_orders = open_orders if open_orders is not None else []
    fills = fills if fills is not None else []
    def fake_post(path, payload, **kw):
        if path != "/info":
            return None
        t = (payload or {}).get("type")
        if t == "openOrders":
            return open_orders
        if t == "userFills":
            return fills
        return None
    monkeypatch.setattr(hl_client, "_http_post", fake_post)
    monkeypatch.setattr(exchange, "_http_post", fake_post)
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")


class TestVerifyOrderExists:
    def test_oid_in_open_orders_verified(self, monkeypatch):
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        exchange.resolve_user_address = lambda: "0xUSER"
        _patch_info(monkeypatch, hl_client, open_orders=[{"coin": "ETH", "oid": 12345}], fills=[])
        out = exchange.verify_order_exists(coin="ETH", oid="12345")
        assert out["verified"] is True
        assert out["in_open_orders"] is True
        assert out["in_user_fills"] is False

    def test_oid_in_fills_verified(self, monkeypatch):
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        exchange.resolve_user_address = lambda: "0xUSER"
        _patch_info(monkeypatch, hl_client, open_orders=[], fills=[{"coin": "ETH", "oid": 12345}])
        out = exchange.verify_order_exists(coin="ETH", oid="12345")
        assert out["verified"] is True
        assert out["in_user_fills"] is True

    def test_cloid_in_open_orders_verified(self, monkeypatch):
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        exchange.resolve_user_address = lambda: "0xUSER"
        _patch_info(monkeypatch, hl_client, open_orders=[{"coin": "ETH", "oid": 999, "cloid": "0xabc"}], fills=[])
        # Cloid is matched as string, so we just pass a string and the matcher
        # compares against str(o.get("cloid")).
        out = exchange.verify_order_exists(coin="ETH", cloid="0xabc")
        assert out["verified"] is True

    def test_oid_missing_everywhere_unverified(self, monkeypatch):
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        exchange.resolve_user_address = lambda: "0xUSER"
        _patch_info(monkeypatch, hl_client, open_orders=[{"coin": "ETH", "oid": 99999}], fills=[])
        out = exchange.verify_order_exists(coin="ETH", oid="11111")
        assert out["verified"] is False
        assert out["in_open_orders"] is False
        assert out["in_user_fills"] is False
        assert "not in openOrders" in out["reason"]

    def test_coin_narrowing_matters(self, monkeypatch):
        """An oid that exists in openOrders but for a DIFFERENT coin must not
        count as verified — we don't want a stale BTC oid to mask a missing ETH one."""
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        exchange.resolve_user_address = lambda: "0xUSER"
        _patch_info(monkeypatch, hl_client, open_orders=[{"coin": "BTC", "oid": 12345}], fills=[])
        out = exchange.verify_order_exists(coin="ETH", oid="12345")
        assert out["verified"] is False

    def test_no_user_address_unverified(self, monkeypatch):
        from hermes_trader.client import exchange
        exchange.resolve_user_address = lambda: ""
        out = exchange.verify_order_exists(coin="ETH", oid="1")
        assert out["verified"] is False
        assert "no_user_address" in out["reason"]

    def test_no_oid_or_cloid_unverified(self, monkeypatch):
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        exchange.resolve_user_address = lambda: "0xUSER"
        _patch_info(monkeypatch, hl_client)
        out = exchange.verify_order_exists(coin="ETH")
        assert out["verified"] is False
        assert "no_oid_or_cloid" in out["reason"]

    def test_info_outage_does_not_raise(self, monkeypatch):
        """verify must NEVER raise — a /info outage is best-effort, the order
        is already on the wire. Report unverified (caller will alert) but
        never propagate the exception."""
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        def boom(path, payload, **kw):
            if (payload or {}).get("type") == "openOrders":
                raise RuntimeError("network down")
            return None
        # Inner try/except inside verify_order_exists swallows the openOrders
        # exception and continues with empty list — so the function should
        # complete cleanly with verified=False rather than re-raising.
        monkeypatch.setattr(hl_client, "_http_post", boom)
        monkeypatch.setattr(exchange, "_http_post", boom)
        monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
        out = exchange.verify_order_exists(coin="ETH", oid="1")
        assert out["verified"] is False
        # Either an explicit verify_exception OR a clean "not in openOrders
        # or userFills" reason is acceptable — what we forbid is raising.
        assert ("verify_exception" in out["reason"]
                or "not in openOrders or userFills" in out["reason"])

    def test_user_fills_oid_string_match(self, monkeypatch):
        """userFills oid is numeric too; verify oid-string match against an int-filled entry."""
        from hermes_trader.client import exchange
        from hermes_trader.client import hl_client
        exchange.resolve_user_address = lambda: "0xUSER"
        _patch_info(monkeypatch, hl_client,
                    open_orders=[],
                    fills=[{"coin": "ETH", "oid": 12345, "cloid": "0xff"}])
        out = exchange.verify_order_exists(coin="ETH", oid="12345")
        assert out["verified"] is True
        assert out["in_user_fills"] is True
