"""Unit tests for the INERT nightly shadow-arm grader (scripts/shadow_grade.py).

Audit 2026-09-07 (Pathia nightly rater absorption): the grader must stay
read-only — it only *recommends* promote/review, it never changes gate state.
These tests pin the pure ``grade_arm`` decision logic across all five verdicts,
both timestamp conventions (ISO string vs millisecond epoch) and the three
arm kinds (block / change / signal).
"""
import importlib.util
import os

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")
_GRADE_PATH = os.path.join(_SCRIPTS, "shadow_grade.py")


def _load_grade():
    spec = importlib.util.spec_from_file_location("shadow_grade_under_test", _GRADE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sg():
    return _load_grade()


def _rec(ts_ms, **kw):
    """Build a record with a millisecond-epoch timestamp."""
    d = {"ts": ts_ms}
    d.update(kw)
    return d


def _iso_rec(iso_str, **kw):
    """Build a record with an ISO-8601 string timestamp."""
    d = {"timestamp": iso_str}
    d.update(kw)
    return d


# ── timestamp handling ────────────────────────────────────────────────────────

def test_record_ts_handles_millis_seconds_and_iso(sg):
    now_ms = 1_700_000_000_000.0
    assert sg._record_ts_ms({"ts": now_ms}) == now_ms
    # seconds epoch gets promoted to millis
    assert sg._record_ts_ms({"ts": 1_700_000_000}) == pytest.approx(1_700_000_000_000.0)
    iso = sg._record_ts_ms({"timestamp": "2026-09-07T00:00:00Z"})
    assert iso is not None and iso > 1e12
    assert sg._record_ts_ms({}) is None


def test_window_stats_exclude_records_older_than_window(sg):
    now = 1_700_000_000_000.0
    h = 3600 * 1000
    recs = [
        _rec(now - 1 * h, would_block=True),    # inside 24h
        _rec(now - 100 * h, would_block=True),  # outside 24h
    ]
    s24 = sg._window_stats(recs, "block", 24, now)
    assert s24["total"] == 1 and s24["hits"] == 1
    s168 = sg._window_stats(recs, "block", 168, now)
    assert s168["total"] == 2 and s168["hits"] == 2


# ── hit-field detection across the three arm kinds ────────────────────────────

def test_hit_field_detects_block_change_signal(sg):
    assert sg._hit_field({"ext_would_block": True}, "block") is True
    assert sg._hit_field({"would_block": False}, "block") is False
    assert sg._hit_field({"would_change": True}, "change") is True
    assert sg._hit_field({"is_candidate": True}, "signal") is True
    assert sg._hit_field({"tripped": False}, "signal") is False
    # no decision field present → None (not counted as a decision)
    assert sg._hit_field({"unrelated": 1}, "block") is None


# ── bespoke per-arm field conventions (verified against real /data writers) ───

def test_ta_late_entry_uses_blocked_field(sg):
    # ta_late_entry writer logs `blocked`, not `would_block`.
    assert sg._hit_field({"blocked": True}, "block", "ta_late_entry") is True
    assert sg._hit_field({"blocked": False}, "block", "ta_late_entry") is False


def test_sizing_v2_uses_v1_v2_notional_delta(sg):
    # >1% notional difference between v1 baseline and v2 output counts as a change.
    assert sg._hit_field({"v1_notional_usd": 100.0, "v2_notional_usd": 120.0},
                         "change", "sizing_v2") is True
    assert sg._hit_field({"v1_notional_usd": 100.0, "v2_notional_usd": 100.2},
                         "change", "sizing_v2") is False
    # missing notionals → no decision, fall through (None here).
    assert sg._hit_field({"coin": "BTC"}, "change", "sizing_v2") is None


def test_pullback_uses_positive_composite_score(sg):
    assert sg._hit_field({"composite_score": 0.7}, "signal", "pullback") is True
    assert sg._hit_field({"composite_score": 0.0}, "signal", "pullback") is False
    assert sg._hit_field({"coin": "ETH"}, "signal", "pullback") is None


# ── verdict: DATA_GAP ─────────────────────────────────────────────────────────

def test_data_gap_when_shadow_arm_yields_no_records(sg):
    now = 1_700_000_000_000.0
    out = sg.grade_arm("ta_late_entry", "shadow", "/nonexistent.jsonl",
                       [24, 72, 168], now_ms=now, records=[])
    assert out["verdict"] == sg.DATA_GAP


def test_off_arm_with_no_records_is_reported_off_not_data_gap(sg):
    # An arm that is switched off is *expected* to produce nothing — it must be
    # reported as OFF (not a blind-gate DATA_GAP, and not a misleading
    # "still collecting"). Only shadow/enforce arms are held to DATA_GAP.
    now = 1_700_000_000_000.0
    out = sg.grade_arm("reentry_cap", "off", "/nonexistent.jsonl",
                       [24, 72, 168], now_ms=now, records=[])
    assert out["verdict"] == sg.OFF


# ── verdict: INSUFFICIENT_DATA / COLLECTING (sample count) ────────────────────

def test_insufficient_for_shadow_below_promote_threshold(sg):
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_block=True) for _ in range(10)]
    out = sg.grade_arm("ta_late_entry", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.INSUFFICIENT


def test_collecting_for_enforce_below_threshold(sg):
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_block=True) for _ in range(10)]
    out = sg.grade_arm("ta_late_entry", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.COLLECTING


# ── verdict: PROMOTE_CANDIDATE (enough samples, healthy, no outcomes yet) ─────

def test_promote_candidate_when_enough_samples_and_meaningful_rate(sg):
    now = 1_700_000_000_000.0
    # 70 records, ~10% block rate, no backfilled outcomes.
    recs = [_rec(now, would_block=(i < 7)) for i in range(70)]
    out = sg.grade_arm("ta_late_entry", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.PROMOTE
    assert out["windows"][-1]["total"] == 70


def test_collecting_when_rate_too_low_to_matter(sg):
    now = 1_700_000_000_000.0
    # 70 records but the gate almost never fires (< 3%) → promoting is pointless.
    recs = [_rec(now, would_block=(i == 0)) for i in range(70)]
    out = sg.grade_arm("ta_late_entry", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.COLLECTING


def test_enforce_arm_never_promotes(sg):
    # An arm already in enforce must never be told PROMOTE (nothing to promote to).
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_block=(i < 7)) for i in range(70)]
    out = sg.grade_arm("ta_late_entry", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.COLLECTING


# ── verdict: REVIEW (backfilled counterfactual outcomes) ──────────────────────

def test_review_when_blocks_have_positive_counterfactual_pnl(sg):
    now = 1_700_000_000_000.0
    recs = []
    # 25 blocked trades that would have WON money → gate is hurting.
    for i in range(25):
        recs.append(_rec(now, would_block=True, outcome="win", pnl_usd=5.0))
    for i in range(45):
        recs.append(_rec(now, would_block=False))
    out = sg.grade_arm("ta_late_entry", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.REVIEW
    assert out["windows"][-1]["mature_outcomes"] == 25
    assert out["windows"][-1]["pnl_usd_sum"] > 0


def test_review_when_blocked_signals_win_too_often(sg):
    now = 1_700_000_000_000.0
    recs = []
    # 22 mature outcomes, 20 wins (91% > 50% futile threshold) for a block arm,
    # but no pnl field → exercised via the win-rate branch.
    for i in range(20):
        recs.append(_rec(now, would_block=True, outcome="win"))
    for i in range(2):
        recs.append(_rec(now, would_block=True, outcome="loss"))
    for i in range(48):
        recs.append(_rec(now, would_block=False))
    out = sg.grade_arm("daily_extension_cap", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.REVIEW


def test_promote_when_outcomes_are_healthy(sg):
    now = 1_700_000_000_000.0
    recs = []
    # 25 mature outcomes, mostly losses avoided and negative counterfactual pnl
    # → the gate is doing its job.
    for i in range(22):
        recs.append(_rec(now, would_block=True, outcome="loss", pnl_usd=-4.0))
    for i in range(3):
        recs.append(_rec(now, would_block=True, outcome="win", pnl_usd=1.0))
    for i in range(45):
        recs.append(_rec(now, would_block=False))
    out = sg.grade_arm("ta_late_entry", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.PROMOTE


# ── change-arm outcome labels are reported arm-beneficial, NOT raw win-rate ──
# Audit 2026-09-08 (change-arm label fix): outcome "win" means counterfactual
# pnl>0 => v1 beats v2 => adopting the CHANGE arm foregoes profit (arm harmful).
# For a change arm the raw win-rate therefore reads inverted; the report must
# surface the arm-beneficial rate (outcome=loss) so a healthy arm is not
# described as a high win-rate for the wrong reason.

def test_change_arm_promote_reason_reports_arm_beneficial_rate(sg):
    now = 1_700_000_000_000.0
    recs = []
    # 25 mature outcomes: 22 beneficial (outcome=loss, v2 outperforms),
    # 3 harmful (outcome=win, v1 beats v2); pnl_usd sum negative (v2 earns more).
    for i in range(22):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=200.0,
                         outcome="loss", pnl_usd=-1.0))
    for i in range(3):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=200.0,
                         outcome="win", pnl_usd=0.5))
    for i in range(40):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=200.0))
    out = sg.grade_arm("sizing_v2", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.PROMOTE
    assert out["kind"] == "change"
    # The arm-beneficial rate (22/25 = 88%) must be shown, and the misleading
    # bare "回填胜率" phrasing must NOT appear for a change arm.
    assert "臂有益率" in out["reason"]
    assert "88%" in out["reason"]
    assert "回填胜率" not in out["reason"]


def test_harmful_change_arm_without_pnl_usd_is_review_not_promote(sg):
    # CS-C gate fix: a change arm whose backfilled counterfactual outcomes are
    # mostly "win" (arm-HARMFUL: the change foregoes profit) must be REVIEW,
    # even when it never writes pnl_usd (confidence_decay / atr_regime_calib).
    # Before the fix the `kind == "block"` guard skipped the high-harm-rate
    # branch for change arms and such an arm was mislabelled PROMOTE_CANDIDATE.
    now = 1_700_000_000_000.0
    recs = []
    # 20 harmful (outcome=win, no pnl_usd at all) + 5 beneficial (outcome=loss),
    # all on records the arm would have changed; 40 more non-outcome records to
    # clear the 60-record promote threshold.
    for i in range(20):
        recs.append(_rec(now, would_change=True, outcome="win"))
    for i in range(5):
        recs.append(_rec(now, would_change=True, outcome="loss"))
    for i in range(40):
        recs.append(_rec(now, would_change=(i < 4)))
    out = sg.grade_arm("confidence_decay", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["kind"] == "change"
    assert out["verdict"] == sg.REVIEW
    # 20/25 = 80% arm-harmful rate must be surfaced (not the inverted label).
    assert "臂有害率" in out["reason"]
    assert "80%" in out["reason"]


def test_change_arm_report_lines_label_outcomes_arm_beneficial(sg):
    d = {
        "generated_at": "2026-09-08 00:00 UTC",
        "windows_h": [168],
        "arms": [
            {"arm": "sizing_v2", "mode": "shadow", "kind": "change",
             "verdict": sg.PROMOTE, "verdict_cn": sg._VERDICT_CN[sg.PROMOTE],
             "reason": "健康",
             "windows": [{"window_h": 168, "total": 300, "hits": 236,
                          "decisions": 300, "hit_rate": 0.787,
                          "mature_outcomes": 200, "outcome_wins": 134,
                          "outcome_losses": 66, "pnl_usd_sum": -21.23,
                          "has_pnl": True}]},
        ],
        "real_baseline": {"real_closes": 0, "real_win_rate": None, "note": "n"},
    }
    report = sg._fmt_report(d)
    # change arm: outcome_wins(134) are the HARMFUL cases, outcome_losses(66)
    # are BENEFICIAL. The labels must be 臂有益/臂有害 (not 胜/负).
    assert "臂有益66" in report
    assert "臂有害134" in report


# ── signal-kind arm uses is_candidate/tripped, not would_block ────────────────

def test_signal_arm_grades_on_candidate_field(sg):
    now = 1_700_000_000_000.0
    recs = [_iso_rec("2026-09-07T00:00:00Z", is_candidate=(i < 10))
            for i in range(70)]
    out = sg.grade_arm("xs_reversal", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    # signal arms are not subject to the block-rate floor; healthy count → PROMOTE
    assert out["verdict"] == sg.PROMOTE
    assert out["kind"] == "signal"
    assert out["windows"][-1]["decisions"] == 70
    assert out["windows"][-1]["hits"] == 10


# ── CS-G §8.1 sizing_v2 成本上限六条件聚合（只读离线闸门）────────────────────

_NOW = 1_788_931_200_000.0  # fixed "now" for deterministic window math


def _cost_rec(ts_ms, side="long", **over):
    """One well-formed post-CS-G v2_cost shadow record (mirrors executor L3260)."""
    funding = over.pop("funding_rate_hr", None)
    hold = over.get("v2_cost_hold_hours", 8.0)
    carry = over.pop("_carry", None)
    if carry is None:
        if funding is None:
            carry = 0.0
        else:
            sign = 1.0 if side == "long" else -1.0
            carry = round(max(0.0, funding * hold * sign) * 100.0, 4)
    notional = over.get("v2_cost_notional_usd", 190.0)
    clamped = over.pop("v2_cost_notional_clamped_usd", notional)
    d = {
        "ts": ts_ms, "mode": "shadow", "coin": over.pop("_coin", "BTC"),
        "v1_notional_usd": 200.0, "v2_notional_usd": 200.0,
        "side": side,
        "v2_cost_slip_bps": 5.0,
        "v2_cost_slip_source": "coin_side",
        "v2_cost_slip_extra_pct": 0.02,
        "v2_cost_fee_rt_pct": 0.05,
        "v2_cost_fee_measured_bps": 0.0,
        "v2_cost_hold_hours": hold,
        "v2_cost_hold_source": "coin_side",
        "v2_cost_funding_rate_hr": funding,
        "v2_cost_carry_pct": carry,
        "v2_cost_borrow_bps": 0.0,
        "v2_cost_denom_pct": 1.15,
        "v2_cost_notional_usd": notional,
        "v2_cost_notional_clamped_usd": clamped,
        "v2_cost_cap_binds": clamped < notional - 1e-9,
        "v2_cost_vs_v2_ratio": 0.9,
    }
    d.update(over)
    return d


def _valid_cost_set(now_ms, n_per_side=10):
    """10/10 (default) clean mature records that pass all six conditions."""
    h = 3600 * 1000
    recs = []
    for i in range(n_per_side):
        recs.append(_cost_rec(now_ms - i * h, side="long", _coin=f"L{i}"))
        recs.append(_cost_rec(now_ms - i * h, side="short", _coin=f"S{i}"))
    return recs


def test_sv2_cost_zero_samples_is_collecting_not_data_gap(sg):
    out = sg.grade_sizing_v2_cost([], now_ms=_NOW)
    assert out["n"] == 0 and out["gate"] == sg.COLLECTING
    # The CS-G block being new must NOT read as a blind gate (never DATA_GAP).
    assert out["gate"] != sg.DATA_GAP
    assert "0 条" in out["gate_reason"]


def test_sv2_cost_ignores_legacy_records_and_window(sg):
    h = 3600 * 1000
    legacy = [{"ts": _NOW - h, "mode": "shadow", "v1_notional_usd": 80.0,
               "v2_notional_usd": 201.0}]  # pre-CS-G record, no v2_cost_*
    old = _valid_cost_set(_NOW - 200 * h)  # well-formed but outside 168h
    out = sg.grade_sizing_v2_cost(legacy + old, now_ms=_NOW)
    assert out["n"] == 0 and out["gate"] == sg.COLLECTING


def test_sv2_cost_all_six_pass(sg):
    out = sg.grade_sizing_v2_cost(_valid_cost_set(_NOW), now_ms=_NOW)
    assert out["n"] == 20 and out["n_long"] == 10 and out["n_short"] == 10
    assert out["all_pass"] is True and out["gate"] == sg.PROMOTE
    for ck, c in out["checks"].items():
        assert c["pass"] is True, ck


def test_sv2_cost_c1_requires_minimum_per_side(sg):
    h = 3600 * 1000
    recs = [_cost_rec(_NOW - i * h, side="long", _coin=f"L{i}") for i in range(10)]
    recs += [_cost_rec(_NOW - i * h, side="short", _coin=f"S{i}") for i in range(5)]
    out = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert out["checks"]["c1_sample_per_side"]["pass"] is False
    assert out["all_pass"] is False and out["gate"] == sg.COLLECTING
    assert "short 5/10" in out["checks"]["c1_sample_per_side"]["detail"]


def test_sv2_cost_c2_cold_sources_default_and_legacy(sg):
    recs = _valid_cost_set(_NOW)
    # 5/20 = 25% > 20% cold-start (default on hold, legacy on slip) → fail.
    for r in recs[:3]:
        r["v2_cost_slip_source"] = "default"
    recs[3]["v2_cost_hold_source"] = "default"
    recs[4]["v2_cost_slip_source"] = "legacy"
    out = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert out["cold_rate"] == 0.25
    assert out["checks"]["c2_source_maturity"]["pass"] is False
    # global_side / coin_shared are mature → must NOT count as cold
    recs[0]["v2_cost_slip_source"] = "global_side"
    recs[4]["v2_cost_slip_source"] = "coin_shared"
    out2 = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert out2["cold_rate"] == 0.15
    assert out2["checks"]["c2_source_maturity"]["pass"] is True


def test_sv2_cost_c3_cap_bind_rate_blocks_above_half(sg):
    recs = _valid_cost_set(_NOW)
    for r in recs[:11]:  # 11/20 = 55% > 50%
        r["v2_cost_notional_clamped_usd"] = 100.0
        r["v2_cost_cap_binds"] = True
    out = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert out["cap_bind_rate"] == 0.55
    assert out["checks"]["c3_cap_bind_rate"]["pass"] is False
    # anomalies must stay clean: cap_binds=true is consistent with clamping
    assert out["field_anomalies"] == 0


def test_sv2_cost_c4_ratio_p50_band_and_systematic_gt1(sg):
    recs = _valid_cost_set(_NOW)  # ratio 0.9 → P50 inside [0.5, 1.0]
    ok = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert ok["checks"]["c4_ratio_sanity"]["pass"] is True
    for r in recs:  # systematic ratio>1 → blocked
        r["v2_cost_vs_v2_ratio"] = 1.2
    bad = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert bad["checks"]["c4_ratio_sanity"]["pass"] is False
    assert bad["ratio_gt1_rate"] == 1.0


def test_sv2_cost_c5_carry_recompute_and_borrow_note(sg):
    # long, funding 0.00001/hr * 8h → carry 0.008% (helper computes it); valid.
    recs = _valid_cost_set(_NOW)
    for r in recs:
        r["v2_cost_funding_rate_hr"] = 0.00001
        sign = 1.0 if r["side"] == "long" else -1.0
        r["v2_cost_carry_pct"] = round(max(0.0, 0.00001 * 8.0 * sign) * 100.0, 4)
    ok = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert ok["checks"]["c5_carry_check"]["pass"] is True
    assert ok["borrow_all_zero"] is True
    assert "未计借币" in ok["checks"]["c5_carry_check"]["detail"]
    # corrupt one carry → condition 5 fails
    recs[0]["v2_cost_carry_pct"] = 0.5
    bad = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert bad["carry_mismatch"] >= 1
    assert bad["checks"]["c5_carry_check"]["pass"] is False
    # funding None but nonzero carry → mismatch
    recs[0]["v2_cost_funding_rate_hr"] = None
    bad2 = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert bad2["checks"]["c5_carry_check"]["pass"] is False


def test_sv2_cost_c6_flags_field_anomaly_and_non_shadow(sg):
    recs = _valid_cost_set(_NOW)
    recs[0]["v2_cost_denom_pct"] = None  # missing required numeric
    out = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert out["field_anomalies"] >= 1
    assert out["checks"]["c6_zero_side_effects"]["pass"] is False
    recs[0]["v2_cost_denom_pct"] = 1.15
    recs[1]["mode"] = "enforce"  # a non-shadow cost record must fail c6
    out2 = sg.grade_sizing_v2_cost(recs, now_ms=_NOW)
    assert out2["non_shadow_records"] == 1
    assert out2["checks"]["c6_zero_side_effects"]["pass"] is False
    # manual checklist for the two things a shadow file cannot prove
    manual = out2["checks"]["c6_zero_side_effects"]["manual_required"]
    assert any("仓位" in m for m in manual) and any("§5" in m for m in manual)


def test_sv2_cost_grade_arm_attach_and_report(sg):
    recs = _valid_cost_set(_NOW)
    out = sg.grade_arm("sizing_v2", "shadow", "p.jsonl", [168],
                       now_ms=_NOW, records=recs)
    assert "sv2_cost" in out and out["sv2_cost"]["all_pass"] is True
    d = {
        "generated_at": "2026-09-09 00:00 UTC", "windows_h": [168],
        "arms": [out],
        "real_baseline": {"real_closes": 0, "real_win_rate": None, "note": "n"},
    }
    report = sg._fmt_report(d)
    assert "§8.1 成本上限" in report and "六条件全过" in report
    # T0 state: only legacy records → report must render the collecting block
    t0 = sg.grade_arm("sizing_v2", "shadow", "p.jsonl", [168],
                      now_ms=_NOW, records=[{"ts": _NOW, "mode": "shadow"}])
    assert t0["sv2_cost"]["n"] == 0 and t0["sv2_cost"]["gate"] == sg.COLLECTING


def test_report_renders_and_contains_all_verdicts(sg):
    d = {
        "generated_at": "2026-09-07 00:00 UTC",
        "windows_h": [24, 72, 168],
        "arms": [
            {"arm": "a", "mode": "shadow", "kind": "block",
             "verdict": sg.DATA_GAP, "verdict_cn": sg._VERDICT_CN[sg.DATA_GAP],
             "reason": "x", "windows": []},
            {"arm": "b", "mode": "shadow", "kind": "block",
             "verdict": sg.PROMOTE, "verdict_cn": sg._VERDICT_CN[sg.PROMOTE],
             "reason": "y", "windows": []},
        ],
        "real_baseline": {"real_closes": 0, "real_win_rate": None, "note": "n"},
    }
    report = sg._fmt_report(d)
    assert "DATA_GAP" not in report  # Chinese label used, not the raw token
    assert "采数缺口" in report
    assert "可考虑升enforce" in report
