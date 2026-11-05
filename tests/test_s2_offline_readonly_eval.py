import importlib.util
import json
import math
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "s2_offline_readonly_eval.py"
spec = importlib.util.spec_from_file_location("s2_offline_eval", SCRIPT)
s2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2)


def test_short_sign_and_3_9_26_costs_applied_once():
    long_gross = s2.directional_bp(100.0, 110.0, "long")
    short_gross = s2.directional_bp(100.0, 110.0, "short")
    assert long_gross == pytest.approx(1000.0)
    assert short_gross == pytest.approx(-1000.0)
    assert s2.apply_cost_once(long_gross, 3.0) == pytest.approx(997.0)
    assert s2.apply_cost_once(long_gross, 9.26) == pytest.approx(990.74)
    assert s2.apply_cost_once(short_gross, 3.0) == pytest.approx(-1003.0)
    rows = [
        {"gross_bp": long_gross, "day": "2026-01-01", "volume_24h_usd": None},
        {"gross_bp": long_gross, "day": "2026-01-02", "volume_24h_usd": None},
    ]
    summary = s2.summarize_returns(rows)
    assert summary["net_3_mean_bp"] == pytest.approx(997.0)
    assert summary["net_9_26_mean_bp"] == pytest.approx(990.74)
    assert summary["net_9_26_ci95_bp"][0] == pytest.approx(summary["gross_block_ci95_bp"][0] - 9.26)


def test_missing_invalid_and_contradictory_flow_never_becomes_zero():
    assert s2.flow_state({"flow_data_available": False, "flow_confirm_raw": None}) == {
        "status": "ordinary", "raw": None, "reason": "available_false_both_sides_none"
    }
    full = s2.flow_state({"flow_data_available": True, "flow_confirm_raw": 0.8})
    assert full["status"] == "full_stack" and full["raw"] == 0.8
    for record in (
        {},
        {"flow_data_available": True, "flow_confirm_raw": None},
        {"flow_data_available": True, "flow_confirm_raw": float("nan")},
        {"flow_data_available": False, "flow_confirm_raw": 0.0},
    ):
        state = s2.flow_state(record)
        assert state["status"] == "missing"
        assert state["raw"] is None
    contradiction = s2.flow_state(
        {"flow_data_available": False, "flow_confirm_raw": None},
        {"fired": True, "flow_confirmed": True},
    )
    assert contradiction["status"] == "missing"


def test_missing_flow_pair_is_not_agreement_and_k_only_is_separate():
    candidate = {"flow_data_available": True, "flow_confirm_raw": None, "hits": [{"name": "breakout", "fired": True}]}
    pairs = s2.evaluate_candidate_fidelity(
        candidate,
        [{"name": "breakout", "fired": True}],
        ["breakout"],
    )
    assert pairs[0]["pairable"] is False
    assert pairs[0]["agree"] is None
    assert pairs[0]["missing_reason"] == "available_true_raw_missing_or_invalid"
    candidate["flow_data_available"] = False
    candidate["flow_confirm_raw"] = None
    pairs = s2.evaluate_candidate_fidelity(candidate, [{"name": "breakout", "fired": True}], ["breakout"])
    assert pairs[0]["pairable"] is True and pairs[0]["agree"] is True


def _passing_fidelity_metrics():
    return {
        "paired_n": 1000,
        "coin_days": 30,
        "pair_coverage": 0.90,
        "agreement_wilson95_lower": 0.95,
        "breakout_live_fired_n": 30,
        "breakout_precision": 0.90,
        "breakout_recall": 0.90,
    }


def test_wilson_and_strict_preregistration_gate():
    lo, hi = s2.wilson_interval(95, 100)
    assert 0.88 < lo < 0.90 and 0.97 < hi < 0.99
    assert s2.wilson_interval(0, 0) == (None, None)
    assert s2.strict_fidelity_verdict(_passing_fidelity_metrics())["status"] == "pass"
    small = _passing_fidelity_metrics()
    small["paired_n"] = 999
    assert s2.strict_fidelity_verdict(small)["status"] == "inconclusive"
    poor_coverage = _passing_fidelity_metrics()
    poor_coverage["pair_coverage"] = 0.899
    assert s2.strict_fidelity_verdict(poor_coverage)["status"] == "inconclusive"
    weak_agreement = _passing_fidelity_metrics()
    weak_agreement["agreement_wilson95_lower"] = 0.949
    assert s2.strict_fidelity_verdict(weak_agreement)["status"] == "fail"
    few_breakouts = _passing_fidelity_metrics()
    few_breakouts.update(breakout_live_fired_n=29, breakout_precision=None, breakout_recall=None)
    assert s2.strict_fidelity_verdict(few_breakouts)["status"] == "inconclusive"


def test_50m_liquidity_is_separate_and_missing_volume_is_not_micro():
    assert s2.volume_tier(None) == "unknown"
    assert s2.volume_tier(50_000_000) == "mid"
    rows = [
        {"gross_bp": 20.0, "day": "2026-01-01", "volume_24h_usd": 49_999_999},
        {"gross_bp": 30.0, "day": "2026-01-01", "volume_24h_usd": 50_000_000},
        {"gross_bp": -10.0, "day": "2026-01-02", "volume_24h_usd": None},
    ]
    liquidity = s2._volume_liquidity(rows)
    assert liquidity["strata"]["5m_to_50m"]["n"] == 1
    assert liquidity["strata"]["gte_50m"]["n"] == 1
    assert liquidity["strata"]["unknown"]["n"] == 1
    assert liquidity["main_direction_not_filtered_by_50m"] is True
    assert liquidity["s0_tier_baselines"]["unknown"]["n"] == 1
    assert liquidity["s0_tier_baselines"]["micro"]["n"] == 0


def test_joint_distribution_uses_only_real_finite_raw_intersection():
    rows = [
        {"side": "long", "breakout_fired": True, "raw": 0.7, "out_of_range": True},
        {"side": "long", "breakout_fired": True, "raw": None, "out_of_range": True},
        {"side": "short", "breakout_fired": False, "raw": -0.8, "out_of_range": False},
    ]
    out = s2.joint_distribution(rows)
    assert out["real_intersection_n"] == 2
    assert out["fired_and_directionalraw07_and_out_n"] == 1
    assert out["missing_not_imputed"] == 1


def test_scored_bar_open_never_uses_future_bar():
    assert s2.scored_bar_open({"bar_close_ms": 1_000_000}) == 700_000
    assert s2.scored_bar_open({"ts": 1_000_000}) == 600_000
    assert s2.scored_bar_open({"ts": 999_999}) == 600_000
    assert s2.scored_bar_open({"bar_close_ms": 1_000_000, "ts": 1_200_000}) == 700_000
    assert s2.scored_bar_open({}) is None


def test_session_loader_uses_real_sides_and_deduplicates(tmp_path):
    records = [
        {"event": "research", "coin": "AAA", "verdict": "LONG", "ts": 1_000_000},
        {"event": "execute", "coin": "AAA", "side": "long", "ts": 1_000_100},
        {"event": "ta_skip", "coin": "AAA", "signal": "LONG_ONLY_SKIP_SHORT", "ts": 1_000_000},
        {"event": "near_miss", "coin": "AAA", "ts": 1_000_000},
        {"event": "ta_skip", "coin": "BBB", "signal": "LONG_ONLY_SKIP_SHORT"},
    ]
    (tmp_path / "session-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    candidates, stats = s2.load_session_candidates(tmp_path)
    assert len(candidates["long"]) == 1
    assert len(candidates["short"]) == 1
    assert candidates["long"][0]["source_record_n"] == 2
    assert set(candidates["long"][0]["source_events"]) == {"research", "execute"}
    assert candidates["short"][0]["source_events"] == ["ta_skip"]
    assert stats["near_miss_records_disclosed_not_used"] == 1
    assert stats["unalignable_short"] == 1


def test_runner_loader_keeps_legacy_and_marks_conflicts(tmp_path):
    records = [
        {"coin": "AAA", "side": "short", "ts": 1_000_000},
        {"coin": "AAA", "side": "short", "ts": 1_000_000, "trigger_hits": [{"name": "breakout", "fired": False}]},
        {"coin": "BBB", "side": "long", "ts": 1_000_000, "trigger_hits": [{"name": "breakout", "fired": False}]},
        {"coin": "BBB", "side": "long", "ts": 1_000_100, "trigger_hits": [{"name": "breakout", "fired": True}]},
    ]
    (tmp_path / "runner_gate_shadow.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    candidates, stats = s2.load_runner_gate_candidates(tmp_path)
    assert len(candidates["short"]) == 1 and len(candidates["long"]) == 1
    assert candidates["short"][0]["source_record_n"] == 2
    assert not candidates["short"][0].get("dedup_conflict")
    assert candidates["long"][0]["dedup_conflict"] is True
    assert stats["raw_short"] == 2
    assert stats["duplicate_short"] == 1
    assert stats["duplicate_long"] == 1


def test_pit_volume_requires_288_contiguous_closed_bars():
    native = s2.NativeCandles([])
    bars = [s2.Candle(t=i * s2.BAR_5M, o=10.0, h=11.0, l=9.0, c=10.0 + i / 1000, v=2.0) for i in range(400)]
    native._series[("AAA", "5m")] = bars
    anchor = bars[-1].t
    expected = sum(b.c * b.v for b in bars[-288:])
    assert native.volume_24h_usd("AAA", anchor) == pytest.approx(expected)
    native._series[("GAP", "5m")] = bars[:200] + bars[201:]
    assert native.volume_24h_usd("GAP", anchor) is None
    assert native.volume_24h_usd("AAA", anchor - s2.BAR_5M // 2) is None


def test_native_candles_merge_supplemental_snapshot_without_replacing_history(tmp_path):
    primary = tmp_path / "primary.json"
    primary.write_text(json.dumps({
        "AAA|5m": {"0": [10, 11, 9, 10, 2]},
    }))
    supplemental = tmp_path / "supplemental.json"
    supplemental.write_text(json.dumps({
        "requested_end_5m": 300000,
        "series": {
            "AAA|5m": {
                "0": [20, 21, 19, 20, 3],
                "300000": [11, 12, 10, 11, 4],
            },
        },
    }))
    native = s2.NativeCandles([primary, supplemental])
    bars = native.series("AAA", "5m")
    assert [b.t for b in bars] == [0, 300000]
    assert bars[0].c == 10.0
    assert native.selected_sources == {"primary.json": 1, "supplemental.json": 1}


def test_btc_4h_regime_uses_closed_bars_with_current_classifier():
    native = s2.NativeCandles([])
    bars = []
    for i in range(60):
        close = 100.0 * 1.01 ** i
        bars.append(s2.Candle(
            t=i * 14_400_000,
            o=close / 1.01,
            h=close * 1.02,
            l=close / 1.02,
            c=close,
            v=1.0,
        ))
    native._series[("BTC", "4h")] = bars
    assert s2._regime(native, "AAA", 60 * 14_400_000) == "up"


def test_return_evidence_and_direction_verdict_inconclusive_priority():
    rows = [
        {"coin": "AAA", "day": "2026-01-01", "gross_bp": 20.0},
        {"coin": "AAA", "day": "2026-01-01", "gross_bp": 30.0},
        {"coin": "BBB", "day": "2026-01-02", "gross_bp": 40.0},
    ]
    summary = s2.summarize_returns(rows)
    assert summary["coin_days"] == 2
    fidelity = {"strict_gate": {"status": "pass"}}
    verdict = s2.direction_verdict(summary, fidelity)
    assert verdict["verdict"] == "inconclusive"
    assert verdict["return_evidence_checks"]["n"]["pass"] is False
    failing = {"strict_gate": {"status": "fail"}}
    assert s2.direction_verdict(summary, failing)["verdict"] == "inconclusive"


def test_legacy_live_schema_remains_in_fidelity_denominator():
    replay = [{"name": "pctMoveSpike", "fired": False}, {"name": "breakout", "fired": False}]
    pairs = s2.evaluate_candidate_fidelity({"coin": "AAA", "side": "long"}, replay, ["pctMoveSpike", "breakout"])
    assert len(pairs) == 2
    assert all(not p["pairable"] for p in pairs)
    assert all(p["missing_reason"] == "live_key_missing" for p in pairs)


def test_short_flow_confirmation_uses_negative_directional_raw():
    state = s2.flow_state(
        {"side": "short", "flow_data_available": True, "flow_confirm_raw": -0.8},
        {"fired": True, "flow_confirmed": True},
    )
    assert state["status"] == "full_stack" and state["raw"] == -0.8
    contradiction = s2.flow_state(
        {"side": "short", "flow_data_available": True, "flow_confirm_raw": 0.8},
        {"fired": True, "flow_confirmed": True},
    )
    assert contradiction["status"] == "missing"


def _native_payload(anchor):
    bars = {}
    for i in range(500):
        close = 100.0 + 0.01 * i
        bars[str(i * s2.BAR_5M)] = [close - 0.01, close + 0.02, close - 0.02, close, 10.0 + (i % 7)]
    scored_close = anchor + s2.BAR_5M
    hourly_end = scored_close // 3_600_000 * 3_600_000 - 3_600_000
    hourly = {str(hourly_end - (129 - i) * 3_600_000): [100.0, 101.0, 99.0, 100.0 + 0.001 * i, 100.0] for i in range(130)}
    four_hour_end = scored_close // 14_400_000 * 14_400_000 - 14_400_000
    four_hour = {str(four_hour_end - (129 - i) * 14_400_000): [100.0, 101.0, 99.0, 100.0 + 0.001 * i, 100.0] for i in range(130)}
    return {"AAA|5m": bars, "AAA|1h": hourly, "AAA|4h": four_hour, "BTC|4h": four_hour}


def test_evaluate_uses_actual_side_and_same_key_live_hit_intersection(tmp_path):
    anchor = 400 * s2.BAR_5M
    scored_close = anchor + s2.BAR_5M
    (tmp_path / "ch_hist_candles.json").write_text(json.dumps(_native_payload(anchor)))
    (tmp_path / ".agent-config.json").write_text("{}")
    session = [{"event": "research", "coin": "AAA", "verdict": "LONG", "ts": scored_close}]
    runner = [
        {"coin": "AAA", "side": "long", "ts": scored_close, "flow_data_available": False, "flow_confirm_raw": None, "trigger_hits": [{"name": "pctMoveSpike", "fired": True}]},
        {"coin": "AAA", "side": "long", "ts": scored_close + s2.BAR_5M},
    ]
    (tmp_path / "session-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in session))
    (tmp_path / "runner_gate_shadow.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runner))
    result = s2.evaluate(tmp_path)
    assert result["long"]["n"] == 1
    assert result["short"]["n"] == 0
    assert result["direction_verdict"]["long"]["verdict"] == "inconclusive"
    assert result["direction_verdict"]["short"]["verdict"] == "inconclusive"
    fidelity = result["fidelity_by_side"]["long"]
    assert fidelity["candidate_n"] == 2
    assert fidelity["paired_n"] == 1
    assert sum(v["total"] for v in fidelity["per_trigger"].values()) == fidelity["total_candidate_trigger_pairs"]
    assert result["fidelity_by_side"]["short"]["candidate_n"] == 0
    flow_long = result["flow"]["s2_3_runner_gate"]["by_side"]["long"]["coverage"]
    assert flow_long["ic_join_matched"] == 1
    assert result["per_hit_ic"]["sides"]["long"]["overall"]["pctMoveSpike"]["n"] == 1
    assert result["per_hit_ic"]["sides"]["short"]["overall"]["pctMoveSpike"]["n"] == 0
    assert result["s3_start_allowed"] is False
    assert result["s3_gate"]["version"] == "relaxed_v1_2026_11_05"
    assert "short_relaxed" in result["s3_gate"]
    assert "long_strict" in result["s3_gate"]
