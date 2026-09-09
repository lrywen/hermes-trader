"""CS-E: structured funnel tests for scripts/shadow_validate.py.

The old script parsed the free-text trading-loop.log ("TA" substring guessing,
gate BLOCKED only counted when the same line carried "Triggers="). The default
path now consumes structured session-log.jsonl events and, crucially, keeps the
TA-judgement layer (REJECTED/WEAK) separate from the pre-TA capacity/cooldown
throttle layer (HELD_THROTTLE / COOLDOWN / RESEARCH_THROTTLE / ... /
JOBS_BACKPRESSURE). These tests pin that layering and the execute-event
classification against synthetic events — no network, no real logs.

The legacy regex Stats path is kept as the --log-regex escape hatch and gets a
smoke test so it does not silently regress.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location(
        "shadow_validate_under_test", _SCRIPTS_DIR / "shadow_validate.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["shadow_validate_under_test"] = m
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------- #
# ta_skip: true TA judgement vs pre-TA throttle/backpressure
# --------------------------------------------------------------------------- #
def test_ta_skip_separates_judgement_from_throttle(mod):
    st = mod.FunnelStats()
    for ev in (
        {"event": "ta_skip", "coin": "BTC", "signal": "REJECTED"},
        {"event": "ta_skip", "coin": "ETH", "signal": "REJECTED"},
        {"event": "ta_skip", "coin": "SOL", "signal": "WEAK"},
        {"event": "ta_skip", "coin": "DOGE", "signal": "RESEARCH_THROTTLE"},
        {"event": "ta_skip", "coin": "XRP", "signal": "RESEARCH_THROTTLE"},
        {"event": "ta_skip", "coin": "ADA", "signal": "HELD_THROTTLE"},
        {"event": "ta_skip", "coin": "BNB", "signal": "COOLDOWN"},
        {"event": "ta_skip", "coin": "LTC", "signal": "BLOCKLISTED"},
        {"event": "ta_skip", "coin": "AVAX", "signal": "SIGNAL_DEDUP"},
        {"event": "ta_skip", "coin": "LINK", "signal": "JOBS_BACKPRESSURE"},
    ):
        st.feed(ev)

    # TA layer only carries real technical-analysis verdicts.
    assert st.ta["REJECTED"] == 2
    assert st.ta["WEAK"] == 1
    # Every pre-TA drop lands in the throttle layer, never the TA counters.
    assert st.throttle["re_research_throttle"] == 2
    assert st.throttle["held_research_throttle"] == 1
    assert st.throttle["post_trade_cooldown"] == 1
    assert st.throttle["coin_blocklist"] == 1
    assert st.throttle["same_setup_content_dedup"] == 1
    assert st.throttle["jobs_backpressure_cap"] == 1
    assert sum(st.ta.values()) == 3
    assert sum(st.throttle.values()) == 7


def test_unknown_throttle_signal_is_bucketed_not_as_ta(mod):
    st = mod.FunnelStats()
    st.feed({"event": "ta_skip", "signal": "SOME_NEW_CAP"})
    assert st.ta == {}
    assert st.throttle["some_new_cap"] == 1


# --------------------------------------------------------------------------- #
# scan / research layers
# --------------------------------------------------------------------------- #
def test_scan_counts_cycles_perceptions_and_coins(mod):
    st = mod.FunnelStats()
    st.feed({"event": "scan", "perceptions": 5,
             "coin_scores": [{"coin": "BTC"}, {"coin": "ETH"}]})
    # legacy alias key "triggers" must still be accepted
    st.feed({"event": "scan", "triggers": 3,
             "coin_scores": [{"coin": "SOL"}]})
    st.feed({"event": "scan"})  # neither key -> 0, must not raise
    assert st.scan_cycles == 3
    assert st.scan_perceptions == 8
    assert st.coins == {"BTC", "ETH", "SOL"}


def test_research_verdict_counter_uppercased(mod):
    st = mod.FunnelStats()
    st.feed({"event": "research", "verdict": "long"})
    st.feed({"event": "research", "verdict": "SHORT"})
    st.feed({"event": "research", "verdict": "PASS"})
    st.feed({"event": "research"})  # missing verdict ignored
    assert st.verdict == {"LONG": 1, "SHORT": 1, "PASS": 1}


# --------------------------------------------------------------------------- #
# execute event: four structurally different outcomes
# --------------------------------------------------------------------------- #
def test_execute_real_fill_counts_only(mod):
    st = mod.FunnelStats()
    st.feed({"event": "execute", "executed": True, "detail": "oid-123",
             "blocked_by": None})
    assert st.exec_events == 1
    assert st.real_filled == 1
    assert st.shadow_would == 0
    assert st.gate_events == 0


def test_execute_shadow_would_execute(mod):
    st = mod.FunnelStats()
    st.feed({"event": "execute", "executed": False,
             "detail": "shadow_mode_would_execute"})
    assert st.real_filled == 0
    assert st.shadow_would == 1
    assert st.gate_events == 0


def test_execute_runner_gate_free_text_classified(mod):
    st = mod.FunnelStats()
    cases = {
        "runner_gate_blocked (confidence 0.55 < 0.62 floor)":
            "confidence_floor",
        "runner_gate_blocked (short confidence 0.40 < 0.55)":
            "short_confidence_floor",
        "runner_gate_blocked (shorts disabled)":
            "shorts_disabled",
        "runner_gate_blocked (late trend-only chase; no fresh breakout)":
            "late_trend_chase",
        "runner_gate_blocked (needs fresh breakout/burst and structure)":
            "needs_impulse_structure",
        "runner_gate_blocked (RSI 78.2 > 70, overbought)":
            "rsi_extension",
        "runner_gate_blocked (extension 4.1x ATR > 3.0)":
            "atr_extension",
        "runner_gate_blocked (HIP-3 composite 0.41 < 0.50)":
            "hip3_composite_floor",
        "runner_gate_blocked (pullback-long SHADOW - no impulse)":
            "pullback_long_shadow",
        "runner_gate_blocked (some brand new reason)":
            "other",
    }
    for detail, label in cases.items():
        st.feed({"event": "execute", "executed": False, "detail": detail})
    for label, n in st.runner_block.items():
        assert n == 1, label
    assert set(st.runner_block) == set(cases.values())
    # confidence values harvested for the floor-vs-conf distribution report
    assert sorted(st.runner_conf) == [0.40, 0.55]
    # runner blocks never enter the 22-gate accounting
    assert st.gate_events == 0
    assert st.shadow_would == 0


def test_execute_blocked_by_list_classified_once_per_event(mod):
    st = mod.FunnelStats()
    st.feed({"event": "execute", "executed": False,
             "detail": "blocked",
             "blocked_by": ["volume below floor", "counter-regime short"]})
    assert st.gate_events == 1            # one evaluation, two gates hit
    assert st.gate_block["liquidity"] == 1
    assert st.gate_block["market_regime"] == 1


def test_execute_gates_bool_map(mod):
    st = mod.FunnelStats()
    st.feed({"event": "execute", "executed": False, "detail": "blocked",
             "gates": {"liquidity_volume": False,
                       "drawdown_halt": True,
                       "notional_cap": False}})
    assert st.gate_events == 1
    assert st.gate_block["liquidity"] == 1
    assert st.gate_block["notional_cap"] == 1
    assert "drawdown_halt" not in st.gate_block   # passed -> not counted


# --------------------------------------------------------------------------- #
# shadow exits
# --------------------------------------------------------------------------- #
def test_shadow_exit_pnl_collected(mod):
    st = mod.FunnelStats()
    st.feed({"event": "shadow_exit", "coin": "BTC", "side": "long",
             "realized_pnl_usd": 1.5})
    st.feed({"event": "shadow_exit", "coin": "ETH", "side": "short",
             "realized_pnl_usd": -0.4})
    assert st.shadow_closes == [("long", "BTC", 1.5), ("short", "ETH", -0.4)]


# --------------------------------------------------------------------------- #
# JSONL ingestion + since bound
# --------------------------------------------------------------------------- #
def test_build_funnel_reads_jsonl_and_applies_since(tmp_path, mod):
    p = tmp_path / "session-log.jsonl"
    rows = [
        {"ts": 1000, "event": "ta_skip", "signal": "REJECTED"},
        {"ts": 2000, "event": "ta_skip", "signal": "JOBS_BACKPRESSURE"},
        "not-json-ignored",
        {"ts": 3000, "event": "execute", "executed": False,
         "detail": "shadow_mode_would_execute"},
    ]
    p.write_text("\n".join(
        r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n",
        encoding="utf-8")

    full = mod.build_funnel(str(p))
    assert full.ta["REJECTED"] == 1
    assert full.throttle["jobs_backpressure_cap"] == 1
    assert full.shadow_would == 1

    bounded = mod.build_funnel(str(p), since_ms=2000)
    assert bounded.ta["REJECTED"] == 0          # ts=1000 filtered out
    assert bounded.throttle["jobs_backpressure_cap"] == 1
    assert bounded.shadow_would == 1


def test_build_funnel_missing_file_is_empty(tmp_path, mod):
    st = mod.build_funnel(str(tmp_path / "nope.jsonl"))
    assert st.exec_events == 0 and st.scan_cycles == 0


def test_parse_since_ms(mod):
    assert mod._parse_since_ms("") == 0
    assert mod._parse_since_ms("garbage") == 0
    early = mod._parse_since_ms("2026-09-01 00:00")
    later = mod._parse_since_ms("2026-09-02 00:00")
    assert early > 0 and later > early


# --------------------------------------------------------------------------- #
# Legacy --log-regex path: preserved escape hatch, smoke-pinned
# --------------------------------------------------------------------------- #
def test_legacy_regex_path_still_feeds(mod):
    st = mod.Stats()
    st.feed("2026-09-08 10:00:00 INFO verdict=LONG confidence=0.7")
    st.feed("2026-09-08 10:00:01 INFO [runner_gate] BTC BLOCKED: "
            "confidence 0.55 < 0.62 floor")
    assert st.verdict["LONG"] == 1
    assert st.runner_block["confidence_floor"] == 1
