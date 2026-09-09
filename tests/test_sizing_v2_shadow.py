"""Tests for the sizing v2 code-level gray release (off / shadow / enforce).

Sizing v2 mirrors the DSL three-layer stop (regime → ATR clamp → ROE/lev cap)
for equal-risk notional; it was historically a pure boolean
(atr_risk_sizing.sizing_v2_enabled, on = enforce immediately) with no
observe-only path. These tests cover the self-contained wrapper in
agents.executor:

  * _sizing_v2_config mode resolution: env HERMES_SIZING_V2_MODE > block
    sizing_v2_mode > legacy boolean sizing_v2_enabled (true → enforce) >
    invalid/missing → off.
  * _sizing_v2_shadow_path resolution: block > env > default.
  * _sizing_v2_record_shadow appends a JSON line.
  * Full maybe_execute wiring: in SHADOW (bot paper mode) the order keeps
    the legacy v1 width/notional while v2 computes and logs the comparison;
    neither the gray cap nor any v2 width is applied. ENFORCE applies the v2
    width and the gray cap. OFF is byte-identical to the legacy path.

Default mode is OFF: with no config/env, behavior is unchanged.
"""

from __future__ import annotations

import json

from hermes_trader.agents import executor

_ENV_MODE = "HERMES_SIZING_V2_MODE"
_ENV_FILE = "HERMES_SIZING_V2_SHADOW_FILE"


# ── config resolution ───────────────────────────────────────────────────────
def test_config_defaults_off(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._sizing_v2_config({})["mode"] == "off"
    assert executor._sizing_v2_config({"atr_risk_sizing": {}})["mode"] == "off"
    # Explicit disabled boolean stays off.
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_enabled": False}})["mode"] == "off"


def test_config_legacy_boolean_true_means_enforce(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    # Backward compatibility: the old boolean on-switch is equivalent to
    # enforce (immediate application), exactly as before the wrapper.
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_enabled": True}})["mode"] == "enforce"


def test_config_block_mode(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "shadow"}})["mode"] == "shadow"
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "ENFORCE"}})["mode"] == "enforce"
    # Explicit tri-state mode wins over the legacy boolean.
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "shadow",
                             "sizing_v2_enabled": True}})["mode"] == "shadow"


def test_config_env_overrides_everything(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "shadow")
    # Env beats both the block tri-state and the legacy boolean.
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "enforce",
                             "sizing_v2_enabled": True}})["mode"] == "shadow"
    monkeypatch.setenv(_ENV_MODE, "enforce")
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "shadow"}})["mode"] == "enforce"


def test_config_invalid_mode_falls_back_off(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "bogus")
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_enabled": True}})["mode"] == "off"
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "nope"}})["mode"] == "off"


def test_shadow_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv(_ENV_FILE, raising=False)
    blk = {"sizing_v2_shadow_log_path": str(tmp_path / "from_config.jsonl")}
    assert executor._sizing_v2_shadow_path(blk).endswith("from_config.jsonl")
    monkeypatch.setenv(_ENV_FILE, str(tmp_path / "from_env.jsonl"))
    assert executor._sizing_v2_shadow_path({}).endswith("from_env.jsonl")
    monkeypatch.delenv(_ENV_FILE, raising=False)
    assert executor._sizing_v2_shadow_path({}).endswith("sizing_v2_shadow.jsonl")


def test_record_shadow_appends_jsonl(tmp_path):
    path = tmp_path / "sv2.jsonl"
    executor._sizing_v2_record_shadow({"coin": "AAA", "v1_stop_pct": 0.5}, str(path))
    executor._sizing_v2_record_shadow({"coin": "BBB", "v1_stop_pct": 1.0}, str(path))
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["coin"] for r in recs] == ["AAA", "BBB"]
    assert recs[0]["v1_stop_pct"] == 0.5


# ── full maybe_execute wiring (bot SHADOW mode = paper, no real order) ──────
class _StubMemory:
    """Neutral memory stub: no disk, no state, zero slip/cooldowns/pnl."""

    def __getattr__(self, name):
        def _stub(*_a, **_k):
            return None
        return _stub

    def avg_exit_slip_bps(self, coin, days=None):
        return 0.0

    # CS-G: neutral side-aware readers still degrade to the conservative
    # default (never zero) so the shadow cost-cap fields are always populated.
    def avg_exit_slip_bps_side(self, coin, side, days=None, min_samples=None,
                               default_bps=2.0):
        return default_bps, "default"

    def avg_hold_hours_side(self, coin, side, days=None, min_samples=None,
                            default_hours=8.0):
        return default_hours, "default"

    def avg_round_trip_fee_bps(self, coin, days=None, min_samples=None):
        return 0.0

    def loss_cooldown_remaining_min(self, coin):
        return 0

    def get_daily_pnl(self):
        return 0.0

    def peak_daily_pnl(self):
        return 0.0

    def daily_realized_pnl(self):
        return 0.0

    def peak_daily_realized_pnl(self):
        return 0.0

    def get_recent_trades(self, n=10):
        return []

    def track_daily_pnl(self, equity, net_contributions=0.0):
        return None


def _wire_executor(monkeypatch, tmp_path, cfg_extra, shadow_file):
    """Mock every I/O boundary around maybe_execute's sizing section.

    Returns a dict capturing the GateContext (trade_notional_usd) the gates
    see — that is the exact notional that would be ordered.
    """
    from hermes_trader.agents import market_regime, shadow_book
    from hermes_trader.client import hl_client

    # CS-G shadow cost block reads the latest funding rate via the cached
    # primitive; keep the test offline and deterministic (no carry contribution
    # unless a test overrides it).
    monkeypatch.setattr(hl_client, "fetch_funding_history",
                        lambda *_a, **_k: [])

    cfg = {
        "mode": "SHADOW", "enable_crypto": True,
        "leverage": 1,
        "max_trade_notional_usd": 0,
        "max_concurrent": 9999,
        "min_market_volume_usd": 0,
        "min_hip3_volume_usd": 0,
        "min_short_volume_usd": 0,
        "max_total_notional_pct": 50.0,
        "max_daily_loss_usd": -1_000_000_000,
        "min_ai_confidence": 0.0,
        "aligned_min_conf": None,
        "min_trend_score": 0.0,
        "coin_allowlist": [],
        "coin_blocklist": [],
        "max_crypto_long_correlated": 9999,
        "cooldown_min": 0,
        "counter_regime_min_conf": 0.0,
        "block_counter_trend_bypass": False,
        "crowded_with_min_conf": 0.0,
        "debate_gate": {"enabled": False},
        "news_blackout": {"enabled": False},
        "circuit_breaker": {"consecutive_loss_limit": 0,
                            "coin_daily_loss_pct": 0.0,
                            "max_drawdown_pct": 0.0},
        "liquidation_maint_margin_pct": 1.0,
        "sl_buffer_bps": 10.0,
        # Legacy v1 width: min(2.5% top-level, 25% ROE at 1x) = 2.5%. v2 runs
        # the DSL mirror: atr_stop clamps atr% 2.0 * 0.5 mult = 1.0% to the
        # 1.0% floor → effective 1.0% (vs v1 2.5%, the documented 2.5x gap).
        "dsl_exit": {
            "max_loss_pct": 2.5, "max_loss_roe_pct": 25.0,
            "atr_stop": {"enabled": True, "atr_mult": 0.5,
                         "floor_pct": 1.0, "ceiling_pct": 4.0},
        },
        # v2 sizing path; gray cap starts at 10%.
        "atr_risk_sizing": {
            "enabled": True,
            "risk_per_trade_pct": 0.02,
            "sizing_basis": "primary_stop",
            "sizing_v2_cap_pct": 0.1,
        },
    }
    cfg.update(cfg_extra)
    monkeypatch.setattr(executor, "read_agent_config", lambda: dict(cfg))
    monkeypatch.setattr(executor, "memory", _StubMemory())
    monkeypatch.setattr(executor, "get_max_leverage", lambda _c: 1)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda *_a, **_k: {"equity": 1000.0, "available": 900.0,
                                           "total_ntl": 0.0, "asset_positions": []})
    # mid 100; ATR 2.0 → atr% = 2.0.
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 2.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 0.0)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)
    # v2 inputs: neutral regime, atr% 2.0 against a mean of 2.0 (no spike),
    # zero slippage. With the dsl_exit atr_stop block above the v2 mirror
    # clamps atr% 2.0*0.5 = 1.0% to the 1.0% floor → effective 1.0%.
    monkeypatch.setattr(market_regime, "detect_regime", lambda *_a, **_k: "neutral")
    monkeypatch.setattr(executor, "get_atr_hist_mean_pct",
                        lambda *_a, **_k: 2.0)
    captured = {}
    real_eval = executor.eval_all_gates

    def _spy_eval(ctx, config, *args, **kw):
        captured["ctx"] = ctx
        return real_eval(ctx, config, *args, **kw)

    monkeypatch.setattr(executor, "eval_all_gates", _spy_eval)
    monkeypatch.setattr(shadow_book, "shadow_open",
                        lambda **kw: captured.update({"shadow_open": kw}))
    # Never place a real order in any configuration.
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *_a, **_k: {"status": "ok", "oid": "x"})
    return captured


def _analysis():
    return {
        "id": "sv2test", "coin": "TEST", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 99.0, "tp_px": 110.0,
        "reasoning": "sizing v2 shadow test",
    }


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_shadow_keeps_v1_notional_and_logs_comparison(monkeypatch, tmp_path):
    """v2 shadow: math runs and JSONL records v1-vs-v2, but the order keeps
    the legacy v1 width/notional — the gray cap must NOT scale it."""
    monkeypatch.setenv(_ENV_MODE, "shadow")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    captured = _wire_executor(monkeypatch, tmp_path, {}, shadow_file)

    res = executor.maybe_execute(_analysis())
    # Bot SHADOW paper-books the would-be order; nothing is really placed.
    assert res.get("reason") == "shadow_mode_would_execute"

    # v1 width 2.5% → risk $20 / 0.025 = $800, under the 1x lev cap ($1000),
    # so the order notional stays $800. Shadow must NOT apply the v2 width
    # nor the 10% gray cap.
    assert abs(captured["ctx"].trade_notional_usd - 800.0) < 1e-6
    assert abs(captured["shadow_open"]["size_usd"] - 800.0) < 1e-6

    recs = _read_jsonl(shadow_file)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["mode"] == "shadow"
    assert rec["coin"] == "TEST"
    assert rec["v1_stop_pct"] == 2.5
    # v2 DSL-mirror width: atr% 2.0*0.5 clamped to the 1.0% floor → 1.0%.
    assert rec["v2_stop_pct"] == 1.0
    # Comparison fields present.
    for key in ("v1_notional_usd", "v2_notional_usd", "notional_ratio",
                "atr_pct", "atr_hist_mean_pct", "atr_calib_mode",
                "atr_calib_regime", "atr_calib_factor"):
        assert key in rec
    # Tighter v2 stop → LARGER un-throttled notional (800 → 2000); the 10%
    # gray cap is enforce-only and must not touch the shadow order.
    assert rec["v1_notional_usd"] == 800.0
    assert rec["v2_notional_usd"] == 2000.0
    assert rec["notional_ratio"] == 2.5


def test_off_matches_legacy_v1_and_writes_nothing(monkeypatch, tmp_path):
    """OFF: v2 math never runs; sizing is the legacy v1 width, no JSONL."""
    monkeypatch.delenv(_ENV_MODE, raising=False)
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    captured = _wire_executor(monkeypatch, tmp_path, {}, shadow_file)

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    # Same v1-derived notional as the shadow case (2.5% width → $800)...
    assert abs(captured["ctx"].trade_notional_usd - 800.0) < 1e-6
    # ...but no v2 shadow record at all.
    assert not (tmp_path / "sizing_v2_shadow.jsonl").exists()


def test_enforce_applies_v2_width_and_gray_cap(monkeypatch, tmp_path):
    """ENFORCE (== legacy boolean true): the v2 width drives notional and the
    gray cap throttles it; no shadow comparison JSONL is written."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    captured = _wire_executor(monkeypatch, tmp_path, {}, shadow_file)

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"

    # v2 width 1.0% → risk $20 / 0.01 = $2000 → 1x lev cap clamps to $1000
    # → 10% gray cap scales to $100.
    assert abs(captured["ctx"].trade_notional_usd - 100.0) < 1e-6
    assert abs(captured["shadow_open"]["size_usd"] - 100.0) < 1e-6
    # Enforce does not write the shadow comparison file.
    assert not (tmp_path / "sizing_v2_shadow.jsonl").exists()


def test_legacy_boolean_enforce_behaves_like_enforce(monkeypatch, tmp_path):
    """sizing_v2_enabled=true with no tri-state config is identical to the
    explicit enforce mode (backward compatibility)."""
    monkeypatch.delenv(_ENV_MODE, raising=False)
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    captured = _wire_executor(
        monkeypatch, tmp_path,
        {"atr_risk_sizing": {"enabled": True, "risk_per_trade_pct": 0.02,
                             "sizing_basis": "primary_stop",
                             "sizing_v2_enabled": True,
                             "sizing_v2_cap_pct": 0.1}},
        shadow_file)

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    assert abs(captured["ctx"].trade_notional_usd - 100.0) < 1e-6


def test_enforce_gray_cap_bumps_sub_min_to_floor(monkeypatch, tmp_path):
    """ENFORCE gray floor (Audit 2026-09-08): when the gray cap scales the
    notional below the exchange minimum by MORE than the usual 50% bump-gap,
    the order is still bumped to the minimum instead of rejected — otherwise a
    small gray cap (e.g. 0.10 on a micro account) rejects 100% of orders and
    the gray release gathers no fills. The v2 stop and notional_cap still cap
    the resulting risk."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    captured = _wire_executor(monkeypatch, tmp_path, {}, shadow_file)
    # Gray-scaled notional is $100 (see test_enforce_applies_v2_width...);
    # a $200 minimum means a 100% gap — past the strict 50% reject gate.
    monkeypatch.setattr(executor, "min_entry_notional_usd",
                        lambda _c, _m: 200.0)

    res = executor.maybe_execute(_analysis())
    # Not rejected: bumped to the exchange minimum and paper-booked as usual.
    assert res.get("reason") == "shadow_mode_would_execute"
    assert abs(captured["ctx"].trade_notional_usd - 200.0) < 1e-6
    assert abs(captured["shadow_open"]["size_usd"] - 200.0) < 1e-6


def test_non_gray_sub_min_still_rejected_past_gap(monkeypatch, tmp_path):
    """The gray floor must NOT weaken the fail-closed gate for non-gray
    undersizing: with cap_pct=1.0 (no gray scale-down), a notional that sits
    >50% under the exchange minimum is still rejected."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    captured = _wire_executor(
        monkeypatch, tmp_path,
        {"atr_risk_sizing": {"enabled": True, "risk_per_trade_pct": 0.02,
                             "sizing_basis": "primary_stop",
                             "sizing_v2_cap_pct": 1.0}},
        shadow_file)
    # No gray scale-down: notional is $1000 (1x lev cap); a $2000 minimum is a
    # 100% gap — the strict 50% gate must reject.
    monkeypatch.setattr(executor, "min_entry_notional_usd",
                        lambda _c, _m: 2000.0)

    res = executor.maybe_execute(_analysis())
    assert res.get("executed") is False
    assert "below_min_order_notional" in res.get("reason", "")
    # No order/paper-book reached the gates.
    assert "ctx" not in captured


# ── CS-G: short side + cost-cap (shadow-only observation) ───────────────────
class _CostStubMemory(_StubMemory):
    """Memory stub with explicit side-aware slip/hold/funding inputs."""

    def __init__(self, side_slip_bps=2.0, hold_hours=8.0, slip_source="default",
                 hold_source="default"):
        self._side_slip = side_slip_bps
        self._hold = hold_hours
        self._slip_src = slip_source
        self._hold_src = hold_source

    def avg_exit_slip_bps_side(self, coin, side, days=None, min_samples=None,
                               default_bps=2.0):
        return self._side_slip, self._slip_src

    def avg_hold_hours_side(self, coin, side, days=None, min_samples=None,
                            default_hours=8.0):
        return self._hold, self._hold_src


def _short_analysis():
    a = _analysis()
    a.update({"action": "SHORT", "side": "short",
              "stop_px": 101.0, "tp_px": 90.0})
    return a


def _set_funding(monkeypatch, rate_hr):
    from hermes_trader.client import hl_client
    monkeypatch.setattr(
        hl_client, "fetch_funding_history",
        lambda *_a, **_k: [{"fundingRate": rate_hr}])


def test_shadow_long_cost_cap_fields_present_and_zero_regression(monkeypatch, tmp_path):
    """CS-G long cold start: cost-cap decomposition is logged additively while
    the existing v1/v2 widths and notionals stay byte-identical ($800/$2000)."""
    monkeypatch.setenv(_ENV_MODE, "shadow")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    captured = _wire_executor(monkeypatch, tmp_path, {}, shadow_file)

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    # Live order sizing is untouched by the cost-cap experiment.
    assert abs(captured["ctx"].trade_notional_usd - 800.0) < 1e-6

    rec = _read_jsonl(shadow_file)[0]
    # Original fields unchanged.
    assert rec["v1_stop_pct"] == 2.5
    assert rec["v2_stop_pct"] == 1.0
    assert rec["v2_notional_usd"] == 2000.0
    assert rec["notional_ratio"] == 2.5
    # CS-G fields: side, fee 0.025%*2 = 0.05% round trip, no funding (stub [])
    # and cold-start side slip default 2bps over the 0 shared slip → 0.02%.
    assert rec["side"] == "long"
    assert rec["v2_cost_slip_source"] == "default"
    assert abs(rec["v2_cost_slip_extra_pct"] - 0.02) < 1e-9
    assert abs(rec["v2_cost_fee_rt_pct"] - 0.05) < 1e-9
    assert rec["v2_cost_funding_rate_hr"] is None
    assert rec["v2_cost_carry_pct"] == 0.0
    assert rec["v2_cost_borrow_bps"] == 0.0
    assert rec["v2_cost_hold_source"] == "default"
    assert abs(rec["v2_cost_hold_hours"] - 8.0) < 1e-9
    # Denominator 1.0% + 0.02% slip + 0.05% fee = 1.07% → $20/0.0107.
    assert abs(rec["v2_cost_denom_pct"] - 1.07) < 1e-9
    assert abs(rec["v2_cost_notional_usd"] - (20.0 / 0.0107)) < 0.01
    # Cost-widened notional is smaller than the plain v2 notional.
    assert rec["v2_cost_notional_usd"] < rec["v2_notional_usd"]
    assert 0.0 < rec["v2_cost_vs_v2_ratio"] < 1.0


def test_shadow_short_negative_funding_adds_carry(monkeypatch, tmp_path):
    """CS-G short: consistent with close accounting
    (funding_cost = rate × hrs × notional × -1 for shorts), NEGATIVE funding
    is a COST for shorts; the signed carry widens the denominator and the
    $500 total-room cap binds."""
    monkeypatch.setenv(_ENV_MODE, "shadow")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    # max_total_notional_pct is a FRACTION of equity: 0.5 × $1000 equity →
    # $500 total-room cap (binds before the 1x lev cap of $1000).
    captured = _wire_executor(monkeypatch, tmp_path,
                              {"max_total_notional_pct": 0.5}, shadow_file)
    monkeypatch.setattr(executor, "memory", _CostStubMemory())
    # -0.01%/hr × 8h hold × (-1 short sign) = +0.08% carry cost.
    _set_funding(monkeypatch, -0.0001)

    res = executor.maybe_execute(_short_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    # Live sizing still on the v1 width (2.5%); the cost model itself is
    # observation-only. The $500 total-room cap binds on both paths, so the
    # v1 raw $800 is clamped to $500 here (same clamp the enforce path uses).
    assert abs(captured["ctx"].trade_notional_usd - 500.0) < 1e-6

    rec = _read_jsonl(shadow_file)[0]
    assert rec["side"] == "short"
    assert abs(rec["v2_cost_carry_pct"] - 0.08) < 1e-9
    # 1.0 stop + 0.02 slip + 0.05 fee + 0.08 carry = 1.15%.
    assert abs(rec["v2_cost_denom_pct"] - 1.15) < 1e-9
    raw = 20.0 / 0.0115
    assert abs(rec["v2_cost_notional_usd"] - raw) < 0.01
    # Raw cost notional ~ $1739 → lev cap $1000 → total-room cap $500 binds.
    assert rec["v2_cost_notional_clamped_usd"] == 500.0
    assert rec["v2_cost_cap_binds"] is True


def test_shadow_short_positive_funding_income_clamped_to_zero(monkeypatch, tmp_path):
    """CS-G short: POSITIVE funding PAYS the short (longs pay shorts on HL),
    i.e. carry income. It must be clamped to 0 so expected income never
    inflates notional."""
    monkeypatch.setenv(_ENV_MODE, "shadow")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    _wire_executor(monkeypatch, tmp_path, {}, shadow_file)
    monkeypatch.setattr(executor, "memory", _CostStubMemory())
    _set_funding(monkeypatch, 0.0001)  # short receives funding

    executor.maybe_execute(_short_analysis())
    rec = _read_jsonl(shadow_file)[0]
    assert rec["side"] == "short"
    # Signed carry = 0.0001*8*(-1) < 0 → clamped to 0 (no denominator relief).
    assert rec["v2_cost_carry_pct"] == 0.0
    assert abs(rec["v2_cost_denom_pct"] - 1.07) < 1e-9


def test_shadow_side_slip_measured_reuses_shared_no_extra(monkeypatch, tmp_path):
    """When the side-aware slip equals the shared coin slip already embedded in
    the v2 stop, the incremental slip widening is exactly zero (no double
    count)."""
    monkeypatch.setenv(_ENV_MODE, "shadow")
    shadow_file = str(tmp_path / "sizing_v2_shadow.jsonl")
    monkeypatch.setenv(_ENV_FILE, shadow_file)
    _wire_executor(monkeypatch, tmp_path, {}, shadow_file)
    # side slip 0 with a coin_side source means shared slip is also 0 here.
    monkeypatch.setattr(executor, "memory",
                        _CostStubMemory(side_slip_bps=0.0, slip_source="coin_side"))

    executor.maybe_execute(_short_analysis())
    rec = _read_jsonl(shadow_file)[0]
    assert rec["v2_cost_slip_source"] == "coin_side"
    assert rec["v2_cost_slip_extra_pct"] == 0.0
    # Denominator only carries the fee (no funding stub): 1.0 + 0.05 = 1.05%.
    assert abs(rec["v2_cost_denom_pct"] - 1.05) < 1e-9
