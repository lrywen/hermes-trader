"""Tests for the AI-confidence freshness decay (roadmap §2, off / shadow /
enforce).

The debate verdict cache (TTL ~300s) can hand back a stale high confidence
without a fresh LLM call, and the re-research throttle lets the same verdict
be re-routed cycles later — a "stale high-score entry". The decay wrapper in
agents.executor exponentially ages the AI confidence by the age of the
verdict CONTENT (verdict|side|confidence signature), factor = 2 ** (-age/hl).

These tests cover:
  * _confidence_decay_config: env HERMES_CONFIDENCE_DECAY_MODE > block
    confidence_decay.mode > invalid/missing → off; halflife parsing.
  * _verdict_signature: verdict/side/rounded-confidence key.
  * _confidence_decay_age_s: first sighting age 0, same signature ages,
    changed signature resets, _reset_confidence_decay clears.
  * _confidence_decay_shadow_path resolution: block > env > default.
  * _confidence_decay_record_shadow appends a JSON line.
  * Full maybe_execute wiring: off is byte-identical and writes nothing;
    shadow logs but never mutates confidence; enforce multiplies a stale
    verdict's confidence so the confidence gate blocks it, and leaves
    ai_confidence_pre_decay / confidence_decay audit fields; a fresh verdict
    (age 0) is untouched; PASS verdicts are skipped.

Default mode is OFF: with no config/env, behaviour is unchanged.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_trader.agents import executor

_ENV_MODE = "HERMES_CONFIDENCE_DECAY_MODE"
_ENV_FILE = "HERMES_CONFIDENCE_DECAY_SHADOW_FILE"


@pytest.fixture(autouse=True)
def _reset_decay_state():
    executor._reset_confidence_decay()
    yield
    executor._reset_confidence_decay()


# ── config resolution ───────────────────────────────────────────────────────
def test_config_defaults_off(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    cfg = executor._confidence_decay_config({})
    assert cfg["mode"] == "off"
    assert cfg["halflife_s"] == executor._CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S
    # Empty block still defaults off.
    assert executor._confidence_decay_config({"confidence_decay": {}})["mode"] == "off"


def test_config_block_mode(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._confidence_decay_config(
        {"confidence_decay": {"mode": "shadow"}})["mode"] == "shadow"
    assert executor._confidence_decay_config(
        {"confidence_decay": {"mode": "ENFORCE"}})["mode"] == "enforce"


def test_config_env_overrides_block(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "shadow")
    assert executor._confidence_decay_config(
        {"confidence_decay": {"mode": "enforce"}})["mode"] == "shadow"
    monkeypatch.setenv(_ENV_MODE, "enforce")
    assert executor._confidence_decay_config(
        {"confidence_decay": {"mode": "shadow"}})["mode"] == "enforce"


def test_config_invalid_mode_falls_back_off(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "bogus")
    assert executor._confidence_decay_config(
        {"confidence_decay": {"mode": "enforce"}})["mode"] == "off"
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._confidence_decay_config(
        {"confidence_decay": {"mode": "nope"}})["mode"] == "off"


def test_config_halflife_parsing(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._confidence_decay_config(
        {"confidence_decay": {"halflife_s": 300}})["halflife_s"] == 300.0
    # Garbage halflife → default; negatives clamp to 0 (decay_factor → 1.0).
    assert executor._confidence_decay_config(
        {"confidence_decay": {"halflife_s": "x"}})["halflife_s"] == \
        executor._CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S
    assert executor._confidence_decay_config(
        {"confidence_decay": {"halflife_s": -5}})["halflife_s"] == 0.0


# ── verdict signature ───────────────────────────────────────────────────────
def test_verdict_signature_keys_on_verdict_side_confidence():
    base = {"verdict": "LONG", "side": "long", "confidence": 0.9}
    assert executor._verdict_signature(base) == "LONG|long|0.90"
    # Case-insensitive verdict/side; confidence rounded to 2 dp.
    assert executor._verdict_signature(
        {"verdict": "long", "side": "LONG", "confidence": 0.904}) == "LONG|long|0.90"
    assert executor._verdict_signature(
        {"verdict": "short", "side": "SHORT", "confidence": 0.55}) == "SHORT|short|0.55"
    # Missing/garbage confidence degrades to 0.00, not a crash.
    assert executor._verdict_signature({"verdict": "LONG"}) == "LONG||0.00"
    assert executor._verdict_signature(
        {"verdict": "LONG", "side": "long", "confidence": "x"}) == "LONG|long|0.00"


# ── onset ageing ────────────────────────────────────────────────────────────
def test_age_first_sighting_is_zero_then_ages():
    now = 1000.0
    assert executor._confidence_decay_age_s("TEST", "LONG|long|0.90", now) == 0.0
    # Same signature, 120s later → age 120.
    assert executor._confidence_decay_age_s("TEST", "LONG|long|0.90", now + 120) == 120.0


def test_age_changed_signature_resets_clock():
    now = 2000.0
    executor._confidence_decay_age_s("TEST", "LONG|long|0.90", now)
    assert executor._confidence_decay_age_s("TEST", "LONG|long|0.90", now + 600) == 600.0
    # New LLM verdict (confidence moved) → fresh signature → age 0 again.
    assert executor._confidence_decay_age_s("TEST", "LONG|long|0.82", now + 600) == 0.0
    assert executor._confidence_decay_age_s("TEST", "LONG|long|0.82", now + 700) == 100.0


def test_age_coins_are_independent():
    now = 3000.0
    executor._confidence_decay_age_s("AAA", "LONG|long|0.90", now)
    executor._confidence_decay_age_s("BBB", "LONG|long|0.90", now + 300)
    assert executor._confidence_decay_age_s("AAA", "LONG|long|0.90", now + 600) == 600.0
    assert executor._confidence_decay_age_s("BBB", "LONG|long|0.90", now + 600) == 300.0


def test_reset_clears_onset():
    executor._confidence_decay_age_s("TEST", "LONG|long|0.90", 4000.0)
    executor._reset_confidence_decay()
    assert executor._confidence_decay_age_s("TEST", "LONG|long|0.90", 4900.0) == 0.0


# ── shadow path / record ────────────────────────────────────────────────────
def test_shadow_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv(_ENV_FILE, raising=False)
    blk = {"shadow_log_path": str(tmp_path / "from_config.jsonl")}
    assert executor._confidence_decay_shadow_path(blk).endswith("from_config.jsonl")
    monkeypatch.setenv(_ENV_FILE, str(tmp_path / "from_env.jsonl"))
    assert executor._confidence_decay_shadow_path({}).endswith("from_env.jsonl")
    monkeypatch.delenv(_ENV_FILE, raising=False)
    assert executor._confidence_decay_shadow_path({}).endswith(
        "confidence_decay_shadow.jsonl")


def test_record_shadow_appends_jsonl(tmp_path):
    path = tmp_path / "cd.jsonl"
    executor._confidence_decay_record_shadow({"coin": "AAA", "age_s": 0.0}, str(path))
    executor._confidence_decay_record_shadow({"coin": "BBB", "age_s": 30.0}, str(path))
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["coin"] for r in recs] == ["AAA", "BBB"]
    assert recs[1]["age_s"] == 30.0


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
    """Mock every I/O boundary around maybe_execute.

    Returns a dict capturing the GateContext the gates see (confidence is the
    value the confidence gate compares against) and the shadow order.
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
        # Confidence gate threshold: a decayed 0.9 → 0.225 must fall below
        # this while the raw 0.9 passes, proving enforce blocks stale entries.
        "min_ai_confidence": 0.70,
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
        "dsl_exit": {
            "max_loss_pct": 2.5, "max_loss_roe_pct": 25.0,
            "atr_stop": {"enabled": True, "atr_mult": 0.5,
                         "floor_pct": 1.0, "ceiling_pct": 4.0},
        },
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
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 2.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 0.0)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)
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
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *_a, **_k: {"status": "ok", "oid": "x"})
    return captured


def _analysis(verdict="LONG", confidence=0.9):
    return {
        "id": "cdtest", "coin": "TEST",
        "action": verdict, "verdict": verdict,
        "side": "long" if verdict == "LONG" else ("short" if verdict == "SHORT" else ""),
        "confidence": confidence, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 99.0, "tp_px": 110.0,
        "reasoning": "confidence decay test",
    }


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_off_is_byte_identical_and_writes_nothing(monkeypatch, tmp_path):
    """OFF: confidence is never touched and no JSONL appears."""
    monkeypatch.delenv(_ENV_MODE, raising=False)
    shadow_file = tmp_path / "confidence_decay_shadow.jsonl"
    monkeypatch.setenv(_ENV_FILE, str(shadow_file))
    captured = _wire_executor(monkeypatch, tmp_path, {}, str(shadow_file))

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    # Raw 0.9 confidence flows straight to the gates.
    assert abs(captured["ctx"].confidence - 0.9) < 1e-9
    assert not shadow_file.exists()


def test_shadow_logs_but_never_mutates_confidence(monkeypatch, tmp_path):
    """SHADOW: a stale verdict is aged and logged but the gates still see the
    raw confidence — no trading behaviour changes."""
    monkeypatch.setenv(_ENV_MODE, "shadow")
    shadow_file = tmp_path / "confidence_decay_shadow.jsonl"
    monkeypatch.setenv(_ENV_FILE, str(shadow_file))
    captured = _wire_executor(monkeypatch, tmp_path,
                              {"confidence_decay": {"halflife_s": 900.0}},
                              str(shadow_file))
    # Pre-seed a 1800s-old identical verdict → factor 2^(-2) = 0.25.
    executor._confidence_decay_onset["TEST"] = {
        "sig": "LONG|long|0.90", "first_ts": time.time() - 1800.0}

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    # Gates still see the raw 0.9 (shadow must not mutate).
    assert abs(captured["ctx"].confidence - 0.9) < 1e-9

    recs = _read_jsonl(shadow_file)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["mode"] == "shadow"
    assert rec["coin"] == "TEST"
    assert rec["verdict"] == "LONG"
    assert abs(rec["confidence_raw"] - 0.9) < 1e-9
    assert abs(rec["decay_factor"] - 0.25) < 0.01
    assert abs(rec["confidence_decayed"] - 0.225) < 0.01
    # Counterfactual tag: raw 0.9 passes the 0.70 gate, decayed 0.225 fails.
    assert rec["would_block_gate"] is True


def test_enforce_blocks_stale_high_confidence(monkeypatch, tmp_path):
    """ENFORCE: a 1800s-old verdict (factor 0.25) drives 0.9 → 0.225, below
    the 0.70 confidence gate, so the stale entry is blocked; the raw value is
    preserved in ai_confidence_pre_decay for audit."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    shadow_file = tmp_path / "confidence_decay_shadow.jsonl"
    monkeypatch.setenv(_ENV_FILE, str(shadow_file))
    captured = _wire_executor(monkeypatch, tmp_path,
                              {"confidence_decay": {"halflife_s": 900.0}},
                              str(shadow_file))
    executor._confidence_decay_onset["TEST"] = {
        "sig": "LONG|long|0.90", "first_ts": time.time() - 1800.0}

    res = executor.maybe_execute(_analysis())
    # Confidence gate blocks the decayed conviction.
    assert res.get("executed") is False
    assert any("confidence" in str(b).lower() for b in res.get("blocked_by", []))
    # The value the gate compared against is the decayed one.
    assert captured["ctx"].confidence < 0.70
    assert abs(captured["ctx"].confidence - 0.225) < 0.01
    # Enforce also writes the shadow JSONL for calibration.
    recs = _read_jsonl(shadow_file)
    assert len(recs) == 1
    assert recs[0]["mode"] == "enforce"
    assert recs[0]["would_block_gate"] is True


def test_enforce_fresh_verdict_passes(monkeypatch, tmp_path):
    """ENFORCE with a fresh verdict (age 0 → factor 1.0): confidence is
    untouched and the entry proceeds."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    shadow_file = tmp_path / "confidence_decay_shadow.jsonl"
    monkeypatch.setenv(_ENV_FILE, str(shadow_file))
    captured = _wire_executor(monkeypatch, tmp_path,
                              {"confidence_decay": {"halflife_s": 900.0}},
                              str(shadow_file))
    # No pre-seeded onset → first sighting, age 0.

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    assert abs(captured["ctx"].confidence - 0.9) < 1e-9
    recs = _read_jsonl(shadow_file)
    assert len(recs) == 1
    assert recs[0]["decay_factor"] == 1.0
    assert recs[0]["would_block_gate"] is False


def test_pass_verdict_is_never_decayed(monkeypatch, tmp_path):
    """PASS verdicts are skipped entirely (structural-override entries are
    driven by live structure, not AI conviction): no shadow record."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    shadow_file = tmp_path / "confidence_decay_shadow.jsonl"
    monkeypatch.setenv(_ENV_FILE, str(shadow_file))
    _wire_executor(monkeypatch, tmp_path,
                   {"confidence_decay": {"halflife_s": 900.0}},
                   str(shadow_file))

    executor._apply_confidence_decay(_analysis(verdict="PASS", confidence=0.95),
                                     {"confidence_decay": {"mode": "enforce"}})
    assert not shadow_file.exists()
