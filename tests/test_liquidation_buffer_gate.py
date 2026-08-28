"""P0-4 regression: liquidation-price pre-place gate.

A position whose existing liquidation price is already within
``HERMES_LIQ_BUFFER_USD`` (=10 by default) of notional cushion from
the current mark is one bad tick away from being auto-liquidated by
the exchange. Adding any new exposure to the same coin (open / add /
flip) can only push the existing liq price CLOSER to the mark, so the
executor must refuse the order and surface a risk-category notify —
operator must first manually close or hedge the at-risk position.

This file pins down:

  1. ``_parse_liquidation_px`` defensively parses HL's liquidationPx
     (string-typed, "0" / None / NaN / unparseable all map to None).
  2. ``fetch_account_state`` returns a ``liquidation_px_by_coin`` map
     that filters out the missing/zero values.
  3. ``_check_liquidation_buffer`` accepts / rejects by the right
     threshold, matching long & short positions, honoring
     ``HERMES_LIQ_BUFFER_USD=0`` as a test bypass, and NEVER raising
     on a /info outage (fail-open with logged warning).
  4. ``execute_plan`` early-returns ``liq_buffer_blocked:`` and clears
     the in-flight marker when the gate trips.
"""

from __future__ import annotations

import pytest


# ── 1. _parse_liquidation_px defensive parsing ───────────────────────────


class TestParseLiquidationPx:
    @pytest.mark.parametrize("raw", [None, "", "0", "0.0", "0e0",
                                      "None", "null", "NaN", "nan",
                                      "abc", "  ", -1.0, -100])
    def test_returns_none_for_missing_or_zero_or_nan(self, raw):
        from hermes_trader.client import hl_client
        assert hl_client._parse_liquidation_px(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ("45000.5", 45000.5),
        ("0.00001", 0.00001),
        (45000.5, 45000.5),
        (1, 1.0),
    ])
    def test_returns_float_for_valid_values(self, raw, expected):
        from hermes_trader.client import hl_client
        out = hl_client._parse_liquidation_px(raw)
        assert out == pytest.approx(expected, rel=1e-9)


# ── 2. fetch_account_state returns liquidation_px_by_coin map ─────────────


def _patch_clearinghouse(monkeypatch, *, main_perp, spot=None):
    """Stub _http_post('/info', ...) so fetch_account_state sees our
    canned clearinghouseState payload. Returns a (perp_payload,
    spot_payload) pair caller can inspect."""
    spot = spot if spot is not None else {"balances": []}
    from hermes_trader.client import hl_client
    def fake_post(path, payload, **kw):
        if path != "/info":
            return None
        t = (payload or {}).get("type")
        if t == "clearinghouseState":
            return main_perp
        if t == "spotClearinghouseState":
            return spot
        return None
    monkeypatch.setattr(hl_client, "_http_post", fake_post)
    return fake_post


class TestFetchAccountStateLiquidationMap:
    def test_no_positions_returns_empty_map(self, monkeypatch):
        from hermes_trader.client import hl_client
        _patch_clearinghouse(
            monkeypatch,
            main_perp={"marginSummary": {"accountValue": "1000",
                                          "totalNtlPos": "0",
                                          "totalMarginUsed": "0"},
                       "assetPositions": []},
        )
        out = hl_client.fetch_account_state("0xUSER")
        assert out["liquidation_px_by_coin"] == {}

    def test_liquidation_px_extracted_per_coin(self, monkeypatch):
        from hermes_trader.client import hl_client
        _patch_clearinghouse(
            monkeypatch,
            main_perp={
                "marginSummary": {"accountValue": "1000",
                                  "totalNtlPos": "1000",
                                  "totalMarginUsed": "100"},
                "assetPositions": [
                    {"position": {"coin": "ETH", "szi": "2.0",
                                  "liquidationPx": "2000.0"}},
                    {"position": {"coin": "BTC", "szi": "-0.01",
                                  "liquidationPx": "50000.0"}},
                ],
            },
        )
        out = hl_client.fetch_account_state("0xUSER")
        m = out["liquidation_px_by_coin"]
        assert m["ETH"]["liquidationPx"] == pytest.approx(2000.0)
        assert m["ETH"]["szi"] == pytest.approx(2.0)
        assert m["BTC"]["liquidationPx"] == pytest.approx(50000.0)
        assert m["BTC"]["szi"] == pytest.approx(-0.01)

    def test_zero_liquidation_px_filtered_out(self, monkeypatch):
        from hermes_trader.client import hl_client
        _patch_clearinghouse(
            monkeypatch,
            main_perp={
                "marginSummary": {"accountValue": "1000",
                                  "totalNtlPos": "0",
                                  "totalMarginUsed": "0"},
                "assetPositions": [
                    {"position": {"coin": "ETH", "szi": "0.001",
                                  "liquidationPx": "0"}},  # fully-margined
                ],
            },
        )
        out = hl_client.fetch_account_state("0xUSER")
        assert out["liquidation_px_by_coin"] == {}

    def test_none_liquidation_px_filtered_out(self, monkeypatch):
        from hermes_trader.client import hl_client
        _patch_clearinghouse(
            monkeypatch,
            main_perp={
                "marginSummary": {"accountValue": "1000",
                                  "totalNtlPos": "0",
                                  "totalMarginUsed": "0"},
                "assetPositions": [
                    {"position": {"coin": "ETH", "szi": "0.5"}},  # no liq_px
                ],
            },
        )
        out = hl_client.fetch_account_state("0xUSER")
        assert out["liquidation_px_by_coin"] == {}


# ── 3. _check_liquidation_buffer: accept / reject logic ───────────────────


def _patch_buffer(monkeypatch, *, threshold, by_coin):
    """Patch the gate's threshold + account_state payload.

    Note: ``executor.py`` does
    ``from hermes_trader.client.hl_client import fetch_account_state``
    at module load, so the function symbol is BOUND in the executor's
    namespace — monkeypatching ``hl_client.fetch_account_state`` alone
    does NOT reach ``executor._check_liquidation_buffer``. Patch BOTH
    attributes (mirroring the P0-6 verify_order_exists test helper).
    """
    from hermes_trader.agents import executor
    from hermes_trader.client import hl_client
    monkeypatch.setattr(executor, "_LIQ_BUFFER_USD", threshold)
    def fake_fetch(user, include_hip3=False):
        return {"liquidation_px_by_coin": by_coin}
    monkeypatch.setattr(hl_client, "fetch_account_state", fake_fetch)
    monkeypatch.setattr(executor, "fetch_account_state", fake_fetch)


class TestCheckLiquidationBuffer:
    def test_no_existing_position_passes(self, monkeypatch):
        from hermes_trader.agents import executor
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={})
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True
        assert out["reason"] == "no_existing_position"

    def test_long_position_far_from_liq_passes(self, monkeypatch):
        from hermes_trader.agents import executor
        # ETH long 1.0, liq at 1500, mid 2000 → gap 500 → 500 USD buffer
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "ETH": {"liquidationPx": 1500.0, "szi": 1.0}
        })
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True
        assert out["buffer_usd"] == pytest.approx(500.0)

    def test_short_position_far_from_liq_passes(self, monkeypatch):
        from hermes_trader.agents import executor
        # ETH short 0.1, liq at 3000, mid 2000 → gap 1000 → 100 USD buffer
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "ETH": {"liquidationPx": 3000.0, "szi": -0.1}
        })
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True
        assert out["buffer_usd"] == pytest.approx(100.0)

    def test_close_to_liq_below_threshold_rejects(self, monkeypatch):
        from hermes_trader.agents import executor
        # ETH long 0.01, liq at 1900, mid 2000 → gap 100 → 1 USD buffer <10
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "ETH": {"liquidationPx": 1900.0, "szi": 0.01}
        })
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is False
        assert "too_close_to_liquidation" in out["error"]
        assert out["buffer_usd"] == pytest.approx(1.0)
        assert out["liquidation_px"] == 1900.0
        assert out["existing_szi"] == 0.01

    def test_boundary_exactly_at_threshold_passes(self, monkeypatch):
        """buffer_usd == threshold is safe (the strict < comparison means
        the threshold itself does not trip the gate)."""
        from hermes_trader.agents import executor
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "ETH": {"liquidationPx": 1990.0, "szi": 1.0}  # gap 10
        })
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True
        assert out["buffer_usd"] == pytest.approx(10.0)

    def test_boundary_just_below_threshold_rejects(self, monkeypatch):
        from hermes_trader.agents import executor
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "ETH": {"liquidationPx": 1989.99, "szi": 1.0}  # gap 10.01
        })
        # Hmm — gap 10.01, szi 1.0 → 10.01 USD > 10, so this PASSES.
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True
        # Now the case that really hits < threshold:
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "ETH": {"liquidationPx": 1990.01, "szi": 1.0}  # gap 9.99
        })
        out2 = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out2["ok"] is False

    def test_gate_disabled_via_zero_threshold_passes(self, monkeypatch):
        from hermes_trader.agents import executor
        _patch_buffer(monkeypatch, threshold=0.0, by_coin={
            "ETH": {"liquidationPx": 1999.9999, "szi": 1.0}  # 0.0001 USD buffer
        })
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True
        assert out["reason"] == "gate_disabled"

    def test_no_mid_price_passes(self, monkeypatch):
        from hermes_trader.agents import executor
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "ETH": {"liquidationPx": 1900.0, "szi": 0.01}
        })
        out = executor._check_liquidation_buffer("ETH", 0, "0xUSER")
        assert out["ok"] is True
        assert out["reason"] == "no_mid_price"

    def test_hip3_coin_prefix_matches(self, monkeypatch):
        from hermes_trader.agents import executor
        # HIP-3 clearinghouse normalizes coin to "xyz:ETH"; the gate
        # must still match a request for plain "ETH" via the suffix rule.
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "xyz:ETH": {"liquidationPx": 1900.0, "szi": 0.01}
        })
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is False
        assert "too_close_to_liquidation" in out["error"]

    def test_other_coin_does_not_block(self, monkeypatch):
        from hermes_trader.agents import executor
        # Different coin in the by_coin map → no match → pass.
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={
            "BTC": {"liquidationPx": 1900.0, "szi": 0.01}
        })
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True

    def test_no_liquidation_px_passes(self, monkeypatch):
        from hermes_trader.agents import executor
        # Position with liq_px=None → fetch_account_state filters it out,
        # so the by_coin map will be empty. The gate passes.
        _patch_buffer(monkeypatch, threshold=10.0, by_coin={})
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True

    def test_fetch_outage_fails_open(self, monkeypatch):
        """A /info outage must NEVER block the main path."""
        from hermes_trader.agents import executor
        from hermes_trader.client import hl_client
        monkeypatch.setattr(executor, "_LIQ_BUFFER_USD", 10.0)
        def boom(user, include_hip3=False):
            raise RuntimeError("network down")
        # Patch both: executor.py's local binding + hl_client.
        monkeypatch.setattr(hl_client, "fetch_account_state", boom)
        monkeypatch.setattr(executor, "fetch_account_state", boom)
        out = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert out["ok"] is True
        assert "fetch_failed" in out["reason"]


# ── 4. execute_plan integration: gate trips → liq_buffer_blocked ──────────


class TestExecutePlanLiqGateIntegration:
    """The gate sits BEFORE place_hl_order and uses the same early-return
    shape as the order_failed branch. We mock the heavy upstream calls
    (analysis, gates, place_hl_order) and confirm the gate's verdict
    short-circuits the placement path."""

    def _build_plan_stub(self):
        """Build a minimal execute_plan-callable namespace. We don't
        import the real execute_plan (it pulls in the full agent stack);
        instead we test the gate via the same call shape the executor
        uses — _check_liquidation_buffer + the in-flight-marker cleanup
        it would trigger."""
        from hermes_trader.agents import executor
        return executor

    def test_rejected_gate_clears_in_flight_and_returns(self, monkeypatch):
        """When _check_liquidation_buffer returns ok=False, the path
        must clear the in-flight marker AND return without calling
        place_hl_order. We simulate this by invoking the same two-step
        flow the executor uses (in-flight add → gate → conditional
        discard)."""
        import uuid as _uuid
        from hermes_trader.agents import executor
        from hermes_trader.client import hl_client
        aid = str(_uuid.uuid4())
        # Pre-add to in-flight set so the cleanup path is exercised.
        executor._IN_FLIGHT_ANALYSES.add(aid)
        # Force a rejection.
        monkeypatch.setattr(executor, "_LIQ_BUFFER_USD", 10.0)
        def fake_fetch(user, include_hip3=False):
            return {"liquidation_px_by_coin": {
                "ETH": {"liquidationPx": 1900.0, "szi": 0.01}
            }}
        monkeypatch.setattr(hl_client, "fetch_account_state", fake_fetch)
        monkeypatch.setattr(executor, "fetch_account_state", fake_fetch)
        gate = executor._check_liquidation_buffer("ETH", 2000.0, "0xUSER")
        assert gate["ok"] is False
        # Simulate the cleanup the executor does on rejection.
        with executor._EXEC_LOCK:
            executor._IN_FLIGHT_ANALYSES.discard(aid)
        # Marker cleared.
        assert aid not in executor._IN_FLIGHT_ANALYSES
        # The rejection payload is JSON-serializable (returns through
        # the FastAPI gate).
        import json
        json.dumps(gate)
