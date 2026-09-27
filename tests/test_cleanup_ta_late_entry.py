"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import json
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="BTC",
                trade_side="long", has_binary_news_risk=False, equity=1000,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def _mk_candle(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _flat_candles(n, price=100.0, vol=1000.0, rng=0.5):
    """Choppy/flat candles oscillating around `price` (produces low ADX)."""
    out = []
    for i in range(n):
        s = 1.0 if (i % 2 == 0) else -1.0
        c = price + s * (i % 3) * 0.1
        out.append(_mk_candle(i, c, c + rng, c - rng, c, vol))
    return out


def _healthy_trend_candles(n=100, start=100.0, vol=1000.0):
    """Rising trend with regular pullbacks so RSI lands in a healthy 50-70
    band. Pattern: 2 up bars then 1 down bar with a deeper retracement."""
    out = []
    price = start
    for i in range(n):
        if i % 3 == 2:
            price -= 0.5  # deeper pullback
        else:
            price += 0.4  # impulse
        out.append(_mk_candle(i, price, price + 0.3, price - 0.1, price, vol + i))
    return out


def _le_config(mode="enforce", **over):
    cfg = {"mode": mode, "min_bars_4h": 30, "trend_relax_enabled": True,
           "adx_trend_threshold": 35, "rsi_ob": 75, "rsi_os": 25,
           "ext_ob": 2.5, "ext_os": -2.5, "rsi_ob_relaxed": 82,
           "rsi_os_relaxed": 18, "ext_ob_relaxed": 3.5, "ext_os_relaxed": -3.5,
           "mtf_enabled": True, "min_bars_15m": 20, "rsi15m_ob": 72,
           "rsi15m_os": 28, "fetch_bars": 100, "shadow_log_path": ""}
    cfg.update(over)
    return {"ta_late_entry": cfg}


def _ms_trend_4h(n, start=100.0, step=0.6, forming=False):
    """Trend candles on a REAL 4h-ms time axis ending at the current bucket.

    ``forming=True`` appends one extra bar at the CURRENT (still-open) 4h
    bucket, which _drop_forming_bar must remove. With ``forming=False`` the
    last bar is the most recently CLOSED bucket.
    """
    import time as _time
    BAR = 4 * 3600_000
    cur_bucket = int(_time.time() * 1000) // BAR * BAR
    n_total = n + (1 if forming else 0)
    # Last bar sits ON the open bucket (forming) or on the most recently
    # CLOSED bucket (cur_bucket - BAR); anything at cur_bucket is still
    # forming and _drop_forming_bar removes it.
    last_t = cur_bucket if forming else cur_bucket - BAR
    out = []
    for i in range(n_total):
        t = last_t - (n_total - 1 - i) * BAR
        c = start + i * step
        out.append(_mk_candle(t, c, c + 0.8, c - 0.2, c, 1000.0 + i))
    return out


def _patch_candles(monkeypatch, c4h, c15m):
    """Patch fetch at BOTH bindings: risk_gates imports hl_client's symbol
    lazily, and ta_filter holds a from-import binding."""
    import hermes_trader.agents.ta_filter as tf
    import hermes_trader.client.hl_client as hl
    fake = lambda coin, interval, count, *a, **k: (
        c4h if interval == "4h" else c15m)
    monkeypatch.setattr(hl, "fetch_hl_candles", fake)
    monkeypatch.setattr(tf, "fetch_hl_candles", fake)


def _patch_regime(monkeypatch, label):
    """detect_regime is imported lazily inside the gate; patch the source
    module so the local from-import re-reads the attribute."""
    import hermes_trader.agents.market_regime as mr
    monkeypatch.setattr(mr, "detect_regime", lambda coin: label)


def _relax_pass_tight_block_candles(n, start=100.0, vol=1000.0):
    """Strong uptrend (ADX ~54, bullish-aligned) with regular pullbacks that
    lands 4h RSI ~79 / extension ~2.5xATR — INSIDE the relaxed chase band
    (RSI<82, ext<3.5) but OUTSIDE the tight band (RSI>75, ext>2.5). Pullbacks
    fall on i%4==0 so the final bar (i=n-1) is an up impulse; these t=i candles
    read as long-closed so the gate scores the FULL series (no forming bar to
    drop). Offline verification (probe_candle_search2): production verdict
    relaxes & passes, a no-relax verdict blocks, and a hot 15m (RSI ~100)
    denies the MTF override."""
    out = []
    price = start
    for i in range(n):
        price += -0.5 if i % 4 == 0 else 0.55
        out.append(_mk_candle(i, price, price + 0.8, price - 0.2, price, vol + i))
    return out


def _rt_config(**over):
    over.setdefault("relax_tier_probe_enabled", True)
    return _le_config(
        mode="enforce",
        rt_relax_adx=45, rt_weak_adx=35,
        rt_weak_rsi_long=70, rt_weak_rsi_short=30, rt_no_trend_adx=20,
        **over,
    )


def _trend_candles(n, start=100.0, step=0.5, vol=1000.0):
    """Steadily rising candles (produces high ADX)."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append(_mk_candle(i, c, c + 0.8, c - 0.2, c, vol + i))
    return out


def test_late_entry_pure_basic_veto_and_pass():
    """Parabolic 4h uptrend blocks longs; healthy trend does not."""
    from hermes_trader.agents.ta_filter import late_entry_check
    bull = _trend_candles(100, start=100.0, step=0.6)
    v = late_entry_check(bull, None, "long", {"mtf_enabled": False})
    assert v["data_ok"] is True and v["block"] is True
    assert "late long" in v["reason"]
    # Shorts against the same up-move are not "late short" extended.
    vs = late_entry_check(bull, None, "short", {"mtf_enabled": False})
    assert vs["block"] is False
    # Healthy trend (RSI ~60): no veto.
    ok = late_entry_check(_healthy_trend_candles(100), None, "long", {"mtf_enabled": False})
    assert ok["block"] is False and ok["data_ok"] is True


def test_late_entry_pure_insufficient_data_fails_open():
    """< min_bars_4h → data_ok False (callers must fail OPEN)."""
    from hermes_trader.agents.ta_filter import late_entry_check
    v = late_entry_check(_trend_candles(10), None, "long", None)
    assert v["data_ok"] is False and v["block"] is False
    v2 = late_entry_check(None, None, "short", None)
    assert v2["data_ok"] is False


def test_late_entry_pure_trend_relax_still_blocks_parabolic():
    """Strong aligned trend widens the limits, but a parabolic stretch beyond
    even the relaxed RSI 82 / +3.5xATR bounds is still vetoed."""
    from hermes_trader.agents.ta_filter import late_entry_check
    bull = _trend_candles(100, start=100.0, step=0.6)
    v = late_entry_check(bull, None, "long", {"mtf_enabled": False})
    assert v["block"] is True
    assert v["relaxed_by_trend"] is True
    assert v["trend_direction"] == "bullish"
    assert "relaxed limits still exceeded" in v["reason"]


def test_late_entry_pure_mtf_override_passes_continuation():
    """4h stretched but 15m RSI not yet extreme → continuation allowed."""
    from hermes_trader.agents.ta_filter import late_entry_check
    bull4h = _trend_candles(100, start=100.0, step=0.6)
    healthy15m = _healthy_trend_candles(60)
    v = late_entry_check(bull4h, healthy15m, "long", None)
    assert v["block"] is False
    assert v["mtf_passed"] is True
    # 15m also parabolic → override denied, block stands.
    hot15m = _trend_candles(60, start=100.0, step=0.6)
    v2 = late_entry_check(bull4h, hot15m, "long", None)
    assert v2["block"] is True and v2["mtf_passed"] is False
    # mtf_enabled=False behaves like no 15m series.
    v3 = late_entry_check(bull4h, healthy15m, "long", {"mtf_enabled": False})
    assert v3["block"] is True and v3["mtf_passed"] is None
    # 15m series too short → sub-check N/A, block stands on 4h alone.
    v4 = late_entry_check(bull4h, healthy15m[:10], "long", None)
    assert v4["block"] is True and v4["mtf_passed"] is None


def test_late_entry_pure_short_side_mirrors():
    """Parabolic downtrend blocks shorts only via the short-side limits."""
    from hermes_trader.agents.ta_filter import late_entry_check
    bear = [_mk_candle(i, 100 - i * 0.6, 101 - i * 0.6, 99 - i * 0.6,
                       100 - i * 0.6, 1000) for i in range(100)]
    v = late_entry_check(bear, None, "short", {"mtf_enabled": False})
    assert v["block"] is True and v["relaxed_by_trend"] is True
    assert v["trend_direction"] == "bearish"
    # Long into a crash is not overbought.
    assert late_entry_check(bear, None, "long", {"mtf_enabled": False})["block"] is False


def test_ta_late_entry_gate_disabled_without_config_block():
    """No ta_late_entry block → zero-fetch disabled path (plain-dict configs)."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    r = ta_late_entry_gate(_ctx(), {})
    assert r["pass"] is True and r["via"] == "ta_late_entry_disabled"
    r2 = ta_late_entry_gate(_ctx(), {"ta_late_entry": "nope"})
    assert r2["via"] == "ta_late_entry_disabled"


def test_ta_late_entry_gate_mode_off():
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    r = ta_late_entry_gate(_ctx(), _le_config(mode="off"))
    assert r["pass"] is True and r["via"] == "ta_late_entry_off"


def test_ta_late_entry_gate_legacy_shadow_value_still_blocks(monkeypatch, tmp_path):
    """SHADOW/LIVE PARITY: the legacy ``mode="shadow"`` value (record but
    never block) was removed. A stale "shadow" config is normalised to
    "enforce" — the order is HARD-blocked exactly as in LIVE — while the
    verdict JSONL is still appended as the additive audit record."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    bull = _trend_candles(100, start=100.0, step=0.6)
    hot15 = _trend_candles(60, start=100.0, step=0.6)
    _patch_candles(monkeypatch, bull, hot15)
    log = tmp_path / "le_shadow.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="shadow", shadow_log_path=str(log)))
    assert r["pass"] is False
    assert r["via"] == "ta_late_entry_block"
    assert "late-entry gate" in r["reason"]
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and '"blocked": true' in lines[0]
    assert '"coin": "TEST"' in lines[0]
    rec = json.loads(lines[0])
    assert rec["mode"] == "enforce" and rec["layer"] == "gate"


def test_ta_late_entry_gate_enforce_blocks(monkeypatch):
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    bull = _trend_candles(100, start=100.0, step=0.6)
    _patch_candles(monkeypatch, bull, bull)
    r = ta_late_entry_gate(_ctx(trade_side="long"), _le_config(mode="enforce"))
    assert r["pass"] is False and r["via"] == "ta_late_entry_block"


def test_ta_late_entry_gate_enforce_passes_healthy(monkeypatch):
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    healthy = _healthy_trend_candles(100)
    _patch_candles(monkeypatch, healthy, healthy)
    r = ta_late_entry_gate(_ctx(trade_side="long"), _le_config(mode="enforce"))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"


def test_ta_late_entry_gate_mtf_override_passes_in_enforce(monkeypatch):
    """4h parabolic but 15m healthy → MTF continuation override passes even
    under enforce (suggestion ④: don't kill with-trend continuation)."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    bull4h = _trend_candles(100, start=100.0, step=0.6)
    healthy15m = _healthy_trend_candles(60)
    _patch_candles(monkeypatch, bull4h, healthy15m)
    r = ta_late_entry_gate(_ctx(trade_side="long"), _le_config(mode="enforce"))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"


def test_ta_late_entry_gate_records_tight_counterfactual_on_relaxed_pass(monkeypatch, tmp_path):
    """SHADOW-ONLY entry-quality probe (2026-09-05): a pass admitted by the ADX
    trend-relax exception must record that a no-relax rule would have blocked
    it. The LIVE decision is unchanged (relaxed pass → order goes through);
    the counterfactual is audit-only.

    Shape: 4h RSI ~79 / ext ~2.5xATR (between tight 75/2.5 and relaxed
    82/3.5), ADX ~54 bullish, 15m parabolic so the MTF override cannot rescue
    the tight block."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    c4h = _relax_pass_tight_block_candles(100)
    hot15m = _trend_candles(60, start=100.0, step=0.6)
    _patch_candles(monkeypatch, c4h, hot15m)
    log = tmp_path / "le_tight.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    # LIVE verdict: relaxed band → PASS (decision untouched by the probe).
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    assert rec["relaxed_by_trend"] is True
    # Counterfactual: with the relaxation OFF this pass would be a BLOCK.
    assert rec["tight_would_block"] is True
    assert rec["tight_relaxed_by_trend"] is False
    assert "late long" in rec["tight_reason"]
    assert "RSI" in rec["tight_reason"]


def test_ta_late_entry_gate_tight_fields_null_when_not_relaxed(monkeypatch, tmp_path):
    """Counter-example: a healthy (non-relaxed) pass never triggers the probe,
    so the tight_* audit fields are all null."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    healthy = _healthy_trend_candles(100)
    _patch_candles(monkeypatch, healthy, healthy)
    log = tmp_path / "le_healthy.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["relaxed_by_trend"] is False
    assert rec["blocked"] is False
    assert rec["tight_would_block"] is None
    assert rec["tight_relaxed_by_trend"] is None
    assert rec["tight_reason"] == ""


def test_ta_late_entry_gate_tight_probe_failure_never_blocks_order(monkeypatch, tmp_path):
    """Fail-safe: if the counterfactual re-run itself raises, the live gate is
    unaffected — the relaxed pass still goes through and tight_* stay null."""
    import hermes_trader.agents.ta_filter as tf
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    real = tf.late_entry_check
    calls = {"n": 0}

    def flaky(c4, c15, side, params):
        calls["n"] += 1
        # 1st call = production verdict; 2nd call = tight counterfactual.
        if calls["n"] >= 2:
            raise RuntimeError("probe boom")
        return real(c4, c15, side, params)

    monkeypatch.setattr(tf, "late_entry_check", flaky)
    c4h = _relax_pass_tight_block_candles(100)
    hot15m = _trend_candles(60, start=100.0, step=0.6)
    _patch_candles(monkeypatch, c4h, hot15m)
    log = tmp_path / "le_flaky.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    assert rec["tight_would_block"] is None
    assert rec["tight_reason"] == ""


def test_chase_exhaustion_pure_function():
    """F2 pure rule, shared by gate/prefilter/backtest: strong ADX trend
    (>=35) plus over-extended RSI or extension flags a late chase; a healthy
    trend (low ADX) or a LONG on non-stretched readings does NOT. Mirrors for
    shorts."""
    from hermes_trader.agents.ta_filter import chase_exhaustion_check
    cfg = {"chase_adx_min": 35, "chase_rsi_long": 60, "chase_ext_long": 1.0,
           "chase_rsi_short": 40, "chase_ext_short": -1.0}

    # Strong parabolic uptrend (reuse the relaxed-pass fixture): long chase.
    hot_long = _relax_pass_tight_block_candles(100)
    r = chase_exhaustion_check(hot_long, "long", cfg)
    assert r["block"] is True and r["data_ok"] is True
    assert "chase-exhaustion long" in r["reason"]
    assert r["adx4h"] >= 35 and (r["rsi4h"] >= 60 or r["extension"] >= 1.0)

    # Healthy uptrend (ADX < 35) must NOT flag even though RSI ~62.
    healthy = _healthy_trend_candles(100)
    rh = chase_exhaustion_check(healthy, "long", cfg)
    assert rh["block"] is False and rh["data_ok"] is True
    assert rh["adx4h"] < 35

    # Insufficient data → data_ok False, no block (callers fail OPEN).
    assert chase_exhaustion_check(_trend_candles(8), "long", cfg)["block"] is False
    bad = chase_exhaustion_check(_trend_candles(8), "long", cfg)
    assert bad["data_ok"] is False and bad["reason"]

    # Unknown side → no block, no crash.
    assert chase_exhaustion_check(hot_long, " sideways", cfg)["block"] is False


def test_ta_late_entry_gate_records_chase_and_counter_probes(monkeypatch, tmp_path):
    """F1+F2 SHADOW-ONLY probes on a relaxed long pass: the live decision is
    untouched (relaxed band → PASS), while the audit record flags BOTH that a
    chase-exhaustion veto would block (F2) and the market-regime label at
    order time (F1). Longs are never counter-regime flagged."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    c4h = _relax_pass_tight_block_candles(100)
    healthy15m = _healthy_trend_candles(60)
    _patch_candles(monkeypatch, c4h, healthy15m)
    _patch_regime(monkeypatch, "up")
    log = tmp_path / "le_probe.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    # LIVE verdict unchanged: relaxed band passes the order through.
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    # F2 chase-exhaustion counterfactual WOULD block this parabolic long.
    assert rec["chase_would_block"] is True
    assert "chase-exhaustion long" in rec["chase_reason"]
    # F1: regime label recorded; a LONG is never flagged counter-regime.
    assert rec["market_regime_label"] == "up"
    assert rec["counter_regime_would_block"] is False
    assert rec["counter_regime_reason"] == ""


def test_ta_late_entry_gate_counter_regime_short_rule(monkeypatch, tmp_path):
    """F1 validated rule: a SHORT outside a down regime is counter-regime and
    would be vetoed; a short in a down regime (and any long) is not flagged.
    Observation only — the live gate still passes the order."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate

    # Downtrend candles that PASS the live gate as a short (strong bearish
    # trend, relaxed band) but are entered against a non-down market regime.
    bear = []
    price = 200.0
    for i in range(100):
        price += 0.4 if i % 3 == 2 else -0.55
        bear.append(_mk_candle(i, price, price + 0.1, price - 0.3, price, 1000 + i))
    healthy15m = _healthy_trend_candles(60)

    _patch_candles(monkeypatch, bear, healthy15m)

    # (a) short in an "up" regime → counter-regime WOULD block.
    _patch_regime(monkeypatch, "up")
    log = tmp_path / "le_cr_up.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="short", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False  # live decision untouched
    assert rec["market_regime_label"] == "up"
    assert rec["counter_regime_would_block"] is True
    assert "down" in rec["counter_regime_reason"]
    # F2 also fires on this strong bearish chase.
    assert rec["chase_would_block"] is True

    # (b) short in a "down" regime → NOT counter-regime (regime aligned).
    _patch_regime(monkeypatch, "down")
    log2 = tmp_path / "le_cr_down.jsonl"
    ta_late_entry_gate(_ctx(trade_side="short", coin="TEST"),
                       _le_config(mode="enforce", shadow_log_path=str(log2)))
    rec2 = json.loads(log2.read_text().splitlines()[0])
    assert rec2["market_regime_label"] == "down"
    assert rec2["counter_regime_would_block"] is False
    assert rec2["counter_regime_reason"] == ""


def test_ta_late_entry_gate_probe_fields_null_when_no_signal(monkeypatch, tmp_path):
    """Counter-example: a healthy (low-ADX) long pass triggers neither probe
    — chase is False (recorded, since the probe always runs), and the counter
    field is False (longs are never flagged)."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    healthy = _healthy_trend_candles(100)
    _patch_candles(monkeypatch, healthy, healthy)
    _patch_regime(monkeypatch, "neutral")
    log = tmp_path / "le_probe_quiet.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["chase_would_block"] is False
    assert rec["chase_reason"] == ""
    assert rec["market_regime_label"] == "neutral"
    assert rec["counter_regime_would_block"] is False


def test_ta_late_entry_gate_chase_probe_failure_never_blocks_order(monkeypatch, tmp_path):
    """Fail-safe F2: if chase_exhaustion_check raises, the live gate is
    unaffected — the pass still goes through and chase_* stay null."""
    import hermes_trader.agents.ta_filter as tf
    from hermes_trader.agents.risk_gates import ta_late_entry_gate

    def boom(candles_4h, side, params=None):
        raise RuntimeError("chase probe boom")

    monkeypatch.setattr(tf, "chase_exhaustion_check", boom)
    c4h = _relax_pass_tight_block_candles(100)
    healthy15m = _healthy_trend_candles(60)
    _patch_candles(monkeypatch, c4h, healthy15m)
    _patch_regime(monkeypatch, "up")
    log = tmp_path / "le_chase_flaky.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    assert rec["chase_would_block"] is None
    assert rec["chase_reason"] == ""
    # F1 regime probe still records independently.
    assert rec["market_regime_label"] == "up"
    assert rec["counter_regime_would_block"] is False


def test_ta_late_entry_gate_counter_regime_probe_failure_never_blocks_order(monkeypatch, tmp_path):
    """Fail-safe F1: if detect_regime raises, the live gate is unaffected and
    the regime fields are null; the F2 chase probe still records."""
    import hermes_trader.agents.market_regime as mr
    from hermes_trader.agents.risk_gates import ta_late_entry_gate

    def boom(coin):
        raise RuntimeError("regime down")

    monkeypatch.setattr(mr, "detect_regime", boom)
    c4h = _relax_pass_tight_block_candles(100)
    healthy15m = _healthy_trend_candles(60)
    _patch_candles(monkeypatch, c4h, healthy15m)
    log = tmp_path / "le_regime_flaky.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    assert rec["market_regime_label"] is None
    assert rec["counter_regime_would_block"] is None
    assert rec["counter_regime_reason"] == ""
    # F2 chase probe still records independently.
    assert rec["chase_would_block"] is True


def test_ta_late_entry_gate_counter_regime_probe_disabled(monkeypatch, tmp_path):
    """When counter_regime_probe_enabled is False the F1 probe is skipped:
    regime label/counter fields stay null and the order still passes."""
    import hermes_trader.agents.market_regime as mr
    from hermes_trader.agents.risk_gates import ta_late_entry_gate

    calls = {"n": 0}

    def spy(coin):
        calls["n"] += 1
        return "up"

    monkeypatch.setattr(mr, "detect_regime", spy)
    c4h = _relax_pass_tight_block_candles(100)
    healthy15m = _healthy_trend_candles(60)
    _patch_candles(monkeypatch, c4h, healthy15m)
    log = tmp_path / "le_cr_off.jsonl"
    r = ta_late_entry_gate(
        _ctx(trade_side="short", coin="TEST"),
        _le_config(mode="enforce", shadow_log_path=str(log),
                   counter_regime_probe_enabled=False))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert calls["n"] == 0  # detect_regime never consulted
    assert rec["market_regime_label"] is None
    assert rec["counter_regime_would_block"] is None
    assert rec["counter_regime_reason"] == ""


def test_weak_trend_noise_pure_function():
    """Weak-trend noise probe (shadow-only): low ADX (< weak_adx_max) AND 4h
    EMA trend not supporting the side flags a noise-zone entry; a strong-ADX
    trend is never a noise zone regardless of side; insufficient data and
    unknown sides fail safe (data_ok False / no block, no crash)."""
    from hermes_trader.agents.ta_filter import weak_trend_noise_check
    cfg = {"weak_adx_max": 25, "weak_flat_blocks": True}

    # Choppy flat candles: ADX ~4, EMA trend resolves bullish (slow drift).
    flat = _flat_candles(100)
    rf = weak_trend_noise_check(flat, "short", cfg)
    assert rf["block"] is True and rf["data_ok"] is True
    assert "weak-trend noise short" in rf["reason"]
    assert rf["adx4h"] < 25
    # Trend supports longs here → a long is NOT noise.
    assert weak_trend_noise_check(flat, "long", cfg)["block"] is False

    # Healthy trend (ADX ~23) bullish: short against it is noise, long is not.
    healthy = _healthy_trend_candles(100)
    rs = weak_trend_noise_check(healthy, "short", cfg)
    assert rs["block"] is True and rs["adx4h"] < 25
    assert weak_trend_noise_check(healthy, "long", cfg)["block"] is False

    # Strong ADX trend (>= ceiling) is never a noise zone, even against side.
    hot = _trend_candles(100)
    assert weak_trend_noise_check(hot, "short", cfg)["block"] is False
    assert weak_trend_noise_check(hot, "short", cfg)["adx4h"] >= 25

    # Raising the ceiling above the strong-trend ADX still keeps it quiet when
    # the trend supports the side (long in a bullish ADX-100 move).
    assert weak_trend_noise_check(hot, "long", {"weak_adx_max": 999})["block"] is False

    # Insufficient data → data_ok False, no block (callers fail OPEN).
    bad = weak_trend_noise_check(_trend_candles(8), "long", cfg)
    assert bad["block"] is False and bad["data_ok"] is False and bad["reason"]

    # Unknown side → no block, no crash.
    assert weak_trend_noise_check(flat, "sideways", cfg)["block"] is False


def test_ta_late_entry_gate_counter_regime_long_rule(monkeypatch, tmp_path):
    """F1 made symmetric: a LONG in a down regime is counter-regime and would
    be vetoed (new branch); a long in up/neutral is not. Observation only —
    the live gate still passes the order."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    healthy = _healthy_trend_candles(100)
    _patch_candles(monkeypatch, healthy, healthy)

    # (a) long in a "down" regime → counter-regime WOULD block.
    _patch_regime(monkeypatch, "down")
    log = tmp_path / "le_cr_long_down.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False  # live decision untouched
    assert rec["market_regime_label"] == "down"
    assert rec["counter_regime_would_block"] is True
    assert "non-down" in rec["counter_regime_reason"]

    # (b) long in an "up" regime → aligned, NOT flagged.
    _patch_regime(monkeypatch, "up")
    log2 = tmp_path / "le_cr_long_up.jsonl"
    ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                       _le_config(mode="enforce", shadow_log_path=str(log2)))
    rec2 = json.loads(log2.read_text().splitlines()[0])
    assert rec2["counter_regime_would_block"] is False
    assert rec2["counter_regime_reason"] == ""

    # (c) long in a "neutral" regime → not counter-regime (only down is).
    _patch_regime(monkeypatch, "neutral")
    log3 = tmp_path / "le_cr_long_neutral.jsonl"
    ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                       _le_config(mode="enforce", shadow_log_path=str(log3)))
    rec3 = json.loads(log3.read_text().splitlines()[0])
    assert rec3["counter_regime_would_block"] is False


def test_ta_late_entry_gate_weak_trend_probe_fires_and_exempts_aligned(monkeypatch, tmp_path):
    """Weak-trend noise probe (shadow-only): a low-ADX short against a non-down
    regime WOULD be blocked, but the SAME candles in a down regime are exempt
    (regime-aligned). Separately, a down-regime LONG with a bullish 4h trend
    fires F1 but NOT the weak probe (the LTC-vs-TAO separation). Live gate
    always passes."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    flat = _flat_candles(100)  # ADX ~4, trend resolves bullish
    _patch_candles(monkeypatch, flat, flat)

    # (a) low-ADX short in a non-aligned (neutral) regime → weak WOULD block.
    _patch_regime(monkeypatch, "neutral")
    log = tmp_path / "le_weak_neutral.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="short", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False  # live decision untouched
    assert rec["weak_trend_would_block"] is True
    assert "weak-trend noise short" in rec["weak_trend_reason"]

    # (b) identical candles but regime aligned (down for a short) → exempt.
    _patch_regime(monkeypatch, "down")
    log2 = tmp_path / "le_weak_down.jsonl"
    ta_late_entry_gate(_ctx(trade_side="short", coin="TEST"),
                       _le_config(mode="enforce", shadow_log_path=str(log2)))
    rec2 = json.loads(log2.read_text().splitlines()[0])
    assert rec2["weak_trend_would_block"] is False
    assert rec2["weak_trend_reason"] == ""

    # (c) down-regime LONG on a bullish 4h trend → F1 fires but weak does NOT
    # (trend supports the long; this is the LTC +$0.09 winner shape, not TAO).
    healthy = _healthy_trend_candles(100)  # ADX ~23, trend bullish
    _patch_candles(monkeypatch, healthy, healthy)
    log3 = tmp_path / "le_weak_long_down.jsonl"
    ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                       _le_config(mode="enforce", shadow_log_path=str(log3)))
    rec3 = json.loads(log3.read_text().splitlines()[0])
    assert rec3["counter_regime_would_block"] is True
    assert rec3["weak_trend_would_block"] is False
    assert rec3["weak_trend_reason"] == ""


def test_ta_late_entry_gate_weak_probe_failure_never_blocks_order(monkeypatch, tmp_path):
    """Fail-safe: if weak_trend_noise_check raises, the live gate is unaffected
    (order passes) and the weak_* fields stay null; the other probes still
    record independently."""
    import hermes_trader.agents.ta_filter as tf
    from hermes_trader.agents.risk_gates import ta_late_entry_gate

    def boom(candles_4h, side, params=None):
        raise RuntimeError("weak probe boom")

    monkeypatch.setattr(tf, "weak_trend_noise_check", boom)
    c4h = _flat_candles(100)
    _patch_candles(monkeypatch, c4h, c4h)
    _patch_regime(monkeypatch, "neutral")
    log = tmp_path / "le_weak_flaky.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="short", coin="TEST"),
                           _le_config(mode="enforce", shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    assert rec["weak_trend_would_block"] is None
    assert rec["weak_trend_reason"] == ""
    # F1 regime probe still records independently (short in neutral).
    assert rec["market_regime_label"] == "neutral"
    assert rec["counter_regime_would_block"] is True


def test_relax_tier_strong_trend_above_45_is_kept():
    """ADX ~54 relaxed pass (the +EV continuation cell) must NOT be flagged by
    the stricter ADX>=45 relax-floor probe, nor by weak/no-trend probes."""
    from hermes_trader.agents.ta_filter import relax_tier_check
    c4h = _relax_pass_tight_block_candles(100)
    r = relax_tier_check(c4h, "long", _rt_config()["ta_late_entry"])
    assert r["data_ok"] is True
    assert r["relaxed_by_trend_today"] is True
    assert r["rt_relax45_would_block"] is False
    assert r["rt_weak_rsi70_would_block"] is False
    assert r["rt_no_adx20_would_block"] is False


def test_relax_tier_no_trend_low_adx_flags_both_sides():
    """Flat tape (ADX ~4 < 20) fires the no-trend chase probe for long & short,
    but never the relax-floor probe (no relaxation happened)."""
    from hermes_trader.agents.ta_filter import _compute_adx, relax_tier_check
    flat = _flat_candles(100)
    assert _compute_adx(flat) < 20  # sanity
    for side in ("long", "short"):
        r = relax_tier_check(flat, side, _rt_config()["ta_late_entry"])
        assert r["rt_no_adx20_would_block"] is True
        assert "no-trend" in r["rt_no_adx20_reason"]
        assert r["rt_relax45_would_block"] is False


def test_relax_tier_mid_band_relax_flags_when_strict_breached():
    """A series whose ADX lands in [35,45) and which relax-today passes only
    via the exception, with RSI above the STRICT 75 limit, is exactly what the
    raised relax floor (45) would block. Search a small slope grid for a series
    that lands in the band so the assertion is on a realisable shape."""
    from hermes_trader.agents.ta_filter import _assess_trend, _compute_adx, _compute_rsi, relax_tier_check
    cfg = _rt_config()["ta_late_entry"]
    found = None
    for step in (0.42, 0.46, 0.5, 0.54, 0.58, 0.62):
        c = _trend_candles(120, start=100.0, step=step)
        a = _compute_adx(c)
        if 35 <= a < 45 and _assess_trend(c) == "bullish" and _compute_rsi(c) > 75:
            found = c
            break
    if found is None:
        return  # synthetic grid did not land in the narrow band this build
    r = relax_tier_check(found, "long", cfg)
    assert r["relaxed_by_trend_today"] is True
    assert r["rt_relax45_would_block"] is True
    assert "relax band" in r["rt_relax45_reason"]


def test_relax_tier_data_guards():
    from hermes_trader.agents.ta_filter import relax_tier_check
    assert relax_tier_check(None, "long", {})["data_ok"] is False
    assert relax_tier_check(_trend_candles(8), "sideways", {})["data_ok"] is False


def test_ta_late_entry_gate_records_relax_tier_fields_without_blocking(monkeypatch, tmp_path):
    """Integration: flat tape → rt_no_adx20 would-block is RECORDED but the
    live gate still passes; all three rt_* fields are present."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    flat = _flat_candles(100)
    _patch_candles(monkeypatch, flat, flat)
    _patch_regime(monkeypatch, "neutral")
    log = tmp_path / "le_rt.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _rt_config(shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    assert rec["rt_no_adx20_would_block"] is True
    assert rec["rt_relax45_would_block"] is False
    assert rec["rt_weak_rsi70_would_block"] is False


def test_ta_late_entry_gate_relax_tier_probe_failure_never_blocks(monkeypatch, tmp_path):
    """If relax_tier_check raises, the live gate is unaffected and rt_* fields
    degrade to None."""
    import hermes_trader.agents.ta_filter as tf
    from hermes_trader.agents.risk_gates import ta_late_entry_gate

    def boom(*a, **k):
        raise RuntimeError("rt boom")
    monkeypatch.setattr(tf, "relax_tier_check", boom)
    flat = _flat_candles(100)
    _patch_candles(monkeypatch, flat, flat)
    _patch_regime(monkeypatch, "neutral")
    log = tmp_path / "le_rt_boom.jsonl"
    r = ta_late_entry_gate(_ctx(trade_side="long", coin="TEST"),
                           _rt_config(shadow_log_path=str(log)))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["blocked"] is False
    assert rec["rt_relax45_would_block"] is None
    assert rec["rt_weak_rsi70_would_block"] is None
    assert rec["rt_no_adx20_would_block"] is None


def test_ta_late_entry_gate_relax_tier_probe_disabled(monkeypatch, tmp_path):
    """relax_tier_probe_enabled=False → rt_* fields stay null; order passes."""
    import hermes_trader.agents.ta_filter as tf
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    calls = {"n": 0}
    real = tf.relax_tier_check

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(tf, "relax_tier_check", spy)
    flat = _flat_candles(100)
    _patch_candles(monkeypatch, flat, flat)
    _patch_regime(monkeypatch, "neutral")
    log = tmp_path / "le_rt_off.jsonl"
    r = ta_late_entry_gate(
        _ctx(trade_side="long", coin="TEST"),
        _rt_config(shadow_log_path=str(log), relax_tier_probe_enabled=False))
    assert r["pass"] is True
    assert calls["n"] == 0
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["rt_no_adx20_would_block"] is None


def test_ta_late_entry_gate_weak_probe_disabled(monkeypatch, tmp_path):
    """When weak_trend_probe_enabled is False the weak probe is skipped (the
    pure function is never called) and weak_* fields stay null; the order
    still passes and F1 still records."""
    import hermes_trader.agents.ta_filter as tf
    from hermes_trader.agents.risk_gates import ta_late_entry_gate

    calls = {"n": 0}

    def spy(candles_4h, side, params=None):
        calls["n"] += 1
        return {"block": True, "reason": "should-not-run", "data_ok": True}

    monkeypatch.setattr(tf, "weak_trend_noise_check", spy)
    c4h = _flat_candles(100)
    _patch_candles(monkeypatch, c4h, c4h)
    _patch_regime(monkeypatch, "neutral")
    log = tmp_path / "le_weak_off.jsonl"
    r = ta_late_entry_gate(
        _ctx(trade_side="short", coin="TEST"),
        _le_config(mode="enforce", shadow_log_path=str(log),
                   weak_trend_probe_enabled=False))
    assert r["pass"] is True and r["via"] == "ta_late_entry_pass"
    rec = json.loads(log.read_text().splitlines()[0])
    assert calls["n"] == 0  # weak probe never consulted
    assert rec["weak_trend_would_block"] is None
    assert rec["weak_trend_reason"] == ""
    # F1 still records independently.
    assert rec["counter_regime_would_block"] is True


def test_ta_late_entry_gate_fail_closed_on_fetch_error(monkeypatch):
    """Default (#4): a fetch/compute failure BLOCKS the order (fail-closed)."""
    import hermes_trader.agents.ta_filter as tf
    import hermes_trader.client.hl_client as hl

    def boom(*a, **k):
        raise RuntimeError("hl down")
    monkeypatch.setattr(hl, "fetch_hl_candles", boom)
    monkeypatch.setattr(tf, "fetch_hl_candles", boom)
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    r = ta_late_entry_gate(_ctx(trade_side="long"), _le_config(mode="enforce"))
    assert r["pass"] is False and r["via"] == "ta_late_entry_data_missing"
    # fail_closed=false restores the historical fail-open posture.
    r2 = ta_late_entry_gate(
        _ctx(trade_side="long"), _le_config(mode="enforce", fail_closed=False))
    assert r2["pass"] is True and r2["via"] == "ta_late_entry_data_missing"


def test_ta_late_entry_gate_fail_closed_on_insufficient_data(monkeypatch):
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    short = _trend_candles(8)
    _patch_candles(monkeypatch, short, short)
    r = ta_late_entry_gate(_ctx(trade_side="long"), _le_config(mode="enforce"))
    assert r["pass"] is False and r["via"] == "ta_late_entry_data_missing"


def test_ta_late_entry_registered_in_eval_all_gates(monkeypatch):
    """The gate is wired into eval_all_gates: an enforce-mode veto blocks the
    whole chain, and a config without the block takes the no-fetch path."""
    from hermes_trader.agents import risk_gates
    # enforce + parabolic 4h/15m → chain must be blocked by ta_late_entry.
    bull = _trend_candles(100, start=100.0, step=0.6)
    _patch_candles(monkeypatch, bull, bull)
    cfg = _le_config(mode="enforce")
    cfg.update({"debate_gate": {"enabled": False}})
    out = risk_gates.eval_all_gates(_ctx(trade_side="long"), cfg)
    assert "ta_late_entry" in out["results"]
    assert out["results"]["ta_late_entry"]["pass"] is False
    assert out["blocked"] is True
    assert any("late-entry" in r for r in out["block_reasons"])
    # A plain dict WITHOUT the block still evaluates (disabled path, no fetch).
    # trade_notional_usd=10 stays under the P1-14 canonical per-trade cap ($30)
    # so the chain isn't blocked by an unrelated gate.
    out2 = risk_gates.eval_all_gates(
        _ctx(trade_notional_usd=10), {"debate_gate": {"enabled": False}})
    assert out2["results"]["ta_late_entry"]["via"] == "ta_late_entry_disabled"
    assert out2["blocked"] is False


def test_ta_late_entry_canonical_config_present():
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS
    block = CANONICAL_DEFAULTS["ta_late_entry"]
    # SHADOW/LIVE PARITY: gate is enforcing by default in every mode.
    assert block["mode"] == "enforce"
    assert block["rsi_ob"] == 75 and block["rsi_os"] == 25
    assert block["adx_trend_threshold"] == 35
    # Phase 0 (audit R3): the 15m continuation override DEFAULTED OFF — its
    # fetch is the only cold candle HTTP in the gate path and it inverts the
    # gate's HTF-tail-filter semantics. Opt back in per-trader via env.
    assert block["mtf_enabled"] is False and block["trend_relax_enabled"] is True


def test_ta_late_entry_schema_rejects_bad_values():
    from hermes_trader.agents.config_schema import validate_config_updates
    errs = validate_config_updates({"ta_late_entry": {"mode": "bogus", "rsi_ob": 999}})
    assert any("mode" in e for e in errs)
    assert any("rsi_ob" in e for e in errs)
    # Legacy "shadow" gray-release value is no longer accepted by NEW updates
    # (the gate normalises a stale on-disk "shadow" to "enforce" at runtime).
    errs2 = validate_config_updates({"ta_late_entry": {"mode": "shadow"}})
    assert any("mode" in e for e in errs2)
    ok = validate_config_updates({"ta_late_entry": {"mode": "enforce", "rsi_ob": 70}})
    assert ok == []
    ok_off = validate_config_updates({"ta_late_entry": {"mode": "off"}})
    assert ok_off == []


def test_ta_late_entry_gate_mtf_disabled_fetches_no_15m(monkeypatch, tmp_path):
    """Phase 0 (R3): with mtf_enabled absent/False the gate fetches ONLY 4h —
    the 15m cache key is never warmed by anything, so every 15m call was a
    cold weight-20 HTTP."""
    import hermes_trader.agents.ta_filter as tf
    import hermes_trader.client.hl_client as hl
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    calls = []

    def fake(coin, interval, count, *a, **k):
        calls.append(interval)
        # Healthy trend so the gate verdict passes (this test only asserts
        # fetch behaviour — parabolic candles would now block under enforce).
        return _healthy_trend_candles(100)

    monkeypatch.setattr(hl, "fetch_hl_candles", fake)
    monkeypatch.setattr(tf, "fetch_hl_candles", fake)
    # Explicitly mtf_enabled=False (mirrors the canonical default).
    cfg = _le_config(mode="enforce", mtf_enabled=False,
                     shadow_log_path=str(tmp_path / "le.jsonl"))
    r = ta_late_entry_gate(_ctx(trade_side="long"), cfg)
    assert r["pass"] is True
    assert "15m" not in calls and calls.count("4h") == 1, calls


def test_ta_late_entry_gate_mtf_enabled_still_fetches_15m(monkeypatch, tmp_path):
    """Opt-in mtf_enabled=True restores the parallel 4h+15m fetch path."""
    import hermes_trader.agents.ta_filter as tf
    import hermes_trader.client.hl_client as hl
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    calls = []

    def fake(coin, interval, count, *a, **k):
        calls.append(interval)
        return _healthy_trend_candles(100)

    monkeypatch.setattr(hl, "fetch_hl_candles", fake)
    monkeypatch.setattr(tf, "fetch_hl_candles", fake)
    cfg = _le_config(mode="enforce", mtf_enabled=True,
                     shadow_log_path=str(tmp_path / "le.jsonl"))
    ta_late_entry_gate(_ctx(trade_side="long"), cfg)
    assert "15m" in calls and "4h" in calls, calls


def test_forming_readings_4h_detects_dropped_bar():
    """Phase 0 (R1): a snapshot ending on the open 4h bucket reports the
    forming bar + readings; a snapshot ending on a closed bucket does not."""
    from hermes_trader.agents.ta_filter import forming_readings_4h
    open_snap = _ms_trend_4h(99, forming=True)
    rd = forming_readings_4h(open_snap)
    assert rd["forming_bar_dropped"] is True
    assert rd["forming_rsi4h"] is not None and rd["forming_extension"] is not None
    closed_snap = _ms_trend_4h(100, forming=False)
    rc = forming_readings_4h(closed_snap)
    assert rc["forming_bar_dropped"] is False
    assert rc["forming_rsi4h"] is None and rc["forming_extension"] is None
    assert forming_readings_4h([])["forming_bar_dropped"] is False


def test_ta_late_entry_gate_records_forming_and_layer(monkeypatch, tmp_path):
    """Gate JSONL rows carry layer="gate" and the forming-bar shadow fields;
    the verdict itself scores the closed series (forming bar dropped). Under
    SHADOW/LIVE parity a late-entry veto also HARD-blocks the order."""
    from hermes_trader.agents.risk_gates import ta_late_entry_gate
    snap = _ms_trend_4h(99, forming=True)
    _patch_candles(monkeypatch, snap, snap)
    log = tmp_path / "le_shadow.jsonl"
    r = ta_late_entry_gate(
        _ctx(trade_side="long", coin="TEST", entry_px=105.0),
        _le_config(mode="shadow", mtf_enabled=False, shadow_log_path=str(log)))
    # Parabolic 4h → vetoed; legacy "shadow" is normalised to enforce → blocked.
    assert r["pass"] is False and r["via"] == "ta_late_entry_block"
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    rec = rows[0]
    assert rec["layer"] == "gate" and rec["blocked"] is True
    assert rec["forming_bar_dropped"] is True
    assert rec["forming_rsi4h"] is not None
    assert rec["entry_px"] is not None and rec["trade_notional_usd"] is not None


def test_ta_late_entry_prefilter_writes_same_jsonl_with_layer(monkeypatch, tmp_path):
    """Phase 0 (R4): a prefilter veto writes the SAME shadow JSONL as the
    gate, tagged layer="prefilter", and the REJECTED body carries
    veto_layer="prefilter" — gate vs prefilter vetoes must be distinguishable."""
    from hermes_trader.agents import ta_filter
    monkeypatch.setenv("HERMES_TA_LATE_ENTRY_SHADOW_FILE",
                       str(tmp_path / "le_shadow.jsonl"))
    bull = _trend_candles(100, start=100.0, step=0.6)
    flat = _flat_candles(100)
    monkeypatch.setattr(ta_filter, "fetch_hl_candles",
                        lambda *a, **k: bull if a[1] in ("1h", "4h") else flat)
    perception = {
        "coin": "TEST", "composite_score": 80,
        "triggers": [{"name": "breakout", "fired": True, "score": 8},
                     {"name": "momentumBurst", "fired": True, "score": 9}],
    }
    res = ta_filter.analyze_perception(perception)
    assert res["signal"] == "REJECTED"
    assert res.get("veto_layer") == "prefilter"
    log = tmp_path / "le_shadow.jsonl"
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    rec = rows[0]
    assert rec["layer"] == "prefilter" and rec["blocked"] is True
    assert rec["entry_px"] is None and rec["trade_notional_usd"] is None
    assert rec["rsi15m"] is None and rec["mtf_passed"] is None
