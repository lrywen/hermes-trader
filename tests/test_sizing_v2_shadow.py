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

    def track_daily_pnl(self, equity):
        return None


def _wire_executor(monkeypatch, tmp_path, cfg_extra, shadow_file):
    """Mock every I/O boundary around maybe_execute's sizing section.

    Returns a dict capturing the GateContext (trade_notional_usd) the gates
    see — that is the exact notional that would be ordered.
    """
    from hermes_trader.agents import market_regime, shadow_book

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
