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


def test_event_arm_fresh_heartbeat_zero_events_is_insufficient_not_data_gap(sg):
    # M13 extension: an event-only arm (market_circuit) writes a heartbeat every
    # tick but only appends to the event JSONL on a real trip. Zero events in
    # the longest window with a FRESH heartbeat means the evaluator is running
    # and the market simply never tripped — not a blind gate. shadow ->
    # INSUFFICIENT_DATA (keep collecting), and it must not look like a gap.
    now = 1_700_000_000_000.0
    out = sg.grade_arm("market_circuit", "shadow", "/nonexistent.jsonl",
                       [24, 72, 168], now_ms=now, records=[],
                       heartbeat_age_sec=12.0)
    assert out["verdict"] == sg.INSUFFICIENT
    assert "heartbeat_ok" in out and out["heartbeat_ok"] is True
    assert "collection_stalled" not in out


def test_event_arm_fresh_heartbeat_enforce_zero_events_is_collecting(sg):
    # enforce arm with a fresh heartbeat and zero trip events is still healthy
    # but unproven -> COLLECTING, never DATA_GAP.
    now = 1_700_000_000_000.0
    out = sg.grade_arm("market_circuit", "enforce", "/nonexistent.jsonl",
                       [24, 72, 168], now_ms=now, records=[],
                       heartbeat_age_sec=300.0)
    assert out["verdict"] == sg.COLLECTING
    assert out["verdict"] != sg.DATA_GAP


def test_event_arm_stale_heartbeat_zero_events_is_real_data_gap(sg):
    # Heartbeat beyond the freshness threshold => the evaluator really stopped
    # (or the write path broke). Zero events with a stale/missing heartbeat must
    # stay DATA_GAP — the exemption must not mask a genuine blind gate.
    now = 1_700_000_000_000.0
    out_stale = sg.grade_arm("market_circuit", "shadow", "/nonexistent.jsonl",
                             [24, 72, 168], now_ms=now, records=[],
                             heartbeat_age_sec=7200.0)
    assert out_stale["verdict"] == sg.DATA_GAP
    out_none = sg.grade_arm("market_circuit", "shadow", "/nonexistent.jsonl",
                            [24, 72, 168], now_ms=now, records=[],
                            heartbeat_age_sec=None)
    assert out_none["verdict"] == sg.DATA_GAP


def test_non_heartbeat_arm_zero_events_still_data_gap_even_if_age_passed(sg):
    # The exemption applies ONLY to arms registered in ARM_HEARTBEAT_FILE. A
    # plain block arm that somehow receives a heartbeat age must NOT be exempt.
    now = 1_700_000_000_000.0
    out = sg.grade_arm("ta_late_entry", "shadow", "/nonexistent.jsonl",
                       [24, 72, 168], now_ms=now, records=[],
                       heartbeat_age_sec=1.0)
    assert out["verdict"] == sg.DATA_GAP


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
    # An arm already in enforce must never be told PROMOTE (nothing to promote
    # to). Audit 2026-09-10 (M1): with data and no health alarm it now gets the
    # dedicated ENFORCE_MAINTAIN health verdict instead of the promotion-oriented
    # COLLECTING wording.
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_block=(i < 7)) for i in range(70)]
    out = sg.grade_arm("ta_late_entry", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.MAINTAIN
    # Below the sample floor it still reports collecting.
    few = sg.grade_arm("ta_late_entry", "enforce", "p.jsonl", [168],
                       now_ms=now, records=[_rec(now, would_block=True)] * 10)
    assert few["verdict"] == sg.COLLECTING


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
    # M2 (2026-09-10): every changed record is a >1% notional change. Add a
    # substantial non-change tail so hit rate stays under 50% and the test
    # isolates the beneficial-rate label, not the width rule.
    for i in range(40):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=200.0))
    for i in range(180):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=100.2))
    # Mature set stays exactly the 25 outcomes above (backfill 25/245=10% is
    # below the M4 floor); assert the low-confidence COLLECTING path explicitly
    # instead, and keep the beneficial-rate label pinned below in the
    # sufficient-backfill variant.
    out = sg.grade_arm("sizing_v2", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.COLLECTING  # M4: 10% backfill blocks promotion
    assert any("回填率仅 10.2%" in w for w in out.get("warnings", []))


def test_change_arm_promote_shows_beneficial_rate_when_backfilled(sg):
    # Same 22/25 beneficial mature set but with ≥20% backfill: the arm-beneficial
    # rate (88%) must appear in the PROMOTE reason.
    now = 1_700_000_000_000.0
    recs = []
    for i in range(22):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=200.0,
                         outcome="loss", pnl_usd=-1.0))
    for i in range(3):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=200.0,
                         outcome="win", pnl_usd=0.5))
    # 25 more mature beneficial outcomes on changed rows → hit set 50 mature
    # with 3 harmful = 6% harmful / 94% beneficial; overall hit rate 50/100.
    for i in range(25):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=200.0,
                         outcome="loss", pnl_usd=-1.0))
    # 50 non-change mature tail → backfill 100%, hit rate 50% (not >50%).
    for i in range(50):
        recs.append(_rec(now, v1_notional_usd=100.0, v2_notional_usd=100.2,
                         outcome="loss"))
    out = sg.grade_arm("sizing_v2", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.PROMOTE
    assert out["kind"] == "change"
    assert "臂有益率 94%" in out["reason"]
    assert out["hit_set_mature"] == 50


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


# ════════════════════════════════════════════════════════════════════════════
# Audit 2026-09-10 — mechanism-defect remediation (M1/M2/M3/M4/M8/M12/M13/M14)
# ════════════════════════════════════════════════════════════════════════════

H = 3600 * 1000


# ── ta_late_entry 命中率口径修正：prefilter 观察流不污染 gate 宽度 ──────────

def test_ta_prefilter_only_records_do_not_inflate_hit_rate(sg):
    # 生产实况：~26k 条全是 prefilter 层（只记拦截，天然 100% blocked），
    # 真实 gate 层 348 次仅拦 1 次。宽度必须只数 gate 层 → 0.3%，不触发
    # too_wide，enforce 臂应判 MAINTAIN。
    now = 1_700_000_000_000.0
    recs = []
    # 200 条 prefilter 拦截观察（每条都带反事实 outcome，模拟已回填）
    for _ in range(200):
        recs.append(_rec(now, layer="prefilter", blocked=True, outcome="loss"))
    # gate 层：300 次真实决策，只拦 1 次
    for _ in range(299):
        recs.append(_rec(now, layer="gate", blocked=False))
    recs.append(_rec(now, layer="gate", blocked=True))
    out = sg.grade_arm("ta_late_entry", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    long = out["windows"][0]
    assert long["decision_scope"] == "gate_layer"
    assert long["total"] == 500          # 全量观察仍计入 total
    assert long["decisions"] == 300      # 命中率分母只含 gate
    assert long["hits"] == 1
    assert long["hit_rate"] < 0.01
    assert out["verdict"] == sg.MAINTAIN
    assert "下单闸门" in out["reason"]


def test_ta_legacy_records_without_layer_count_as_gate(sg):
    # 8-29 前旧记录无 layer 字段，按 gate 语义计入命中率分母。
    now = 1_700_000_000_000.0
    recs = [_rec(now, blocked=True) for _ in range(40)]
    recs += [_rec(now, blocked=False) for _ in range(20)]
    out = sg.grade_arm("ta_late_entry", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    long = out["windows"][0]
    assert long["decisions"] == 60 and long["hits"] == 40
    # 40/60 = 66.7% > 50% → 真实 gate 太宽，仍应触发降级复核
    assert out["verdict"] == sg.DEGRADED_REVIEW


def test_other_arms_hit_rate_unaffected_by_layer_scope(sg):
    # 非 ta_late_entry 臂维持全记录口径，decision_scope=all_records。
    now = 1_700_000_000_000.0
    recs = [_rec(now, layer="prefilter", would_block=True) for _ in range(10)]
    s = sg._window_stats(recs, "block", 168, now, arm="daily_extension_cap")
    assert s["decision_scope"] == "all_records"
    assert s["decisions"] == 10 and s["hits"] == 10


# ── M1: enforce arms get a health verdict, never a promotion-oriented one ─────

def test_enforce_high_hit_rate_triggers_degraded_review(sg):
    # ta_late_entry production profile: 97% hit rate, mature harm rate 37%.
    # Width alone (>50%) must raise ENFORCE_DEGRADED_REVIEW, not a benign
    # "collecting/maintain" verdict.
    now = 1_700_000_000_000.0
    recs = [_rec(now, blocked=True) for _ in range(60)]
    recs += [_rec(now, blocked=False) for _ in range(2)]
    out = sg.grade_arm("ta_late_entry", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.DEGRADED_REVIEW
    assert "拦/改太宽" in out["reason"]


def test_enforce_harmful_rate_triggers_degraded_review(sg):
    now = 1_700_000_000_000.0
    recs = []
    for _ in range(15):  # 60% harmful, moderate 30% hit rate
        recs.append(_rec(now, would_block=True, outcome="win"))
    for _ in range(10):
        recs.append(_rec(now, would_block=True, outcome="loss"))
    for _ in range(35):
        recs.append(_rec(now, would_block=False))
    out = sg.grade_arm("daily_extension_cap", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.DEGRADED_REVIEW
    assert "臂有害率" in out["reason"]


def test_enforce_healthy_arm_maintains(sg):
    now = 1_700_000_000_000.0
    recs = []
    for _ in range(5):
        recs.append(_rec(now, would_block=True, outcome="win"))
    for _ in range(20):
        recs.append(_rec(now, would_block=True, outcome="loss"))
    for _ in range(45):
        recs.append(_rec(now, would_block=False))
    out = sg.grade_arm("reentry_cap", "enforce", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.MAINTAIN
    assert "维持" in out["reason"]


# ── M2: shadow arms with >50% hit rate are REVIEW (too wide) ─────────────────

def test_shadow_too_wide_with_no_outcomes_is_review(sg):
    now = 1_700_000_000_000.0
    recs = [_rec(now - H, would_block=True) for _ in range(60)]
    out = sg.grade_arm("reentry_cap", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.REVIEW
    assert "太宽" in out["reason"]


def test_shadow_wide_but_healthy_outcomes_still_review(sg):
    # 90% hit rate, harm rate only 10%: healthy effectiveness, but width alone
    # blocks promotion pending human review.
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_change=True, outcome="win") for _ in range(5)]
    recs += [_rec(now, would_change=True, outcome="loss") for _ in range(45)]
    recs += [_rec(now, would_change=False, outcome="loss") for _ in range(10)]
    out = sg.grade_arm("confidence_decay", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.REVIEW
    assert out["windows"][-1]["decisions"] == 60


# ── M3: PROMOTE requires ≤40% harm rate safety margin ────────────────────────

def test_promote_grey_zone_45pct_harm_is_collecting(sg):
    # confidence_decay production profile: 45.1% harm rate, healthy backfill.
    now = 1_700_000_000_000.0
    recs = []
    # 45% harm rate (45 wins / 100 mature) on hit records. All 400 records are
    # mature (100% backfill clears M4); only the first 100 are hits so the
    # overall hit rate is 25% (under the too-wide line).
    for _ in range(45):
        recs.append(_rec(now, would_block_gate=True, outcome="win"))
    for _ in range(55):
        recs.append(_rec(now, would_block_gate=True, outcome="loss"))
    for _ in range(300):
        recs.append(_rec(now, would_block_gate=False, outcome="loss"))
    out = sg.grade_arm("confidence_decay", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.COLLECTING
    assert "安全余量" in out["reason"]


def test_promote_at_40pct_or_below_harm_passes(sg):
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_block=True, outcome="win") for _ in range(8)]
    recs += [_rec(now, would_block=True, outcome="loss") for _ in range(17)]
    recs += [_rec(now, would_block=False, outcome="loss") for _ in range(40)]
    out = sg.grade_arm("reentry_cap", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.PROMOTE


# ── M4: low backfill → low-confidence REVIEW / blocked PROMOTE ───────────────

def test_low_backfill_harmful_review_is_flagged_low_confidence(sg):
    # atr_regime_calib profile: 325 records, 25 mature, 22 wins (88%), 7.7%.
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_change=True, outcome="win") for _ in range(22)]
    recs += [_rec(now, would_change=True, outcome="loss") for _ in range(3)]
    recs += [_rec(now, would_change=(i < 0)) for i in range(300)]
    out = sg.grade_arm("atr_regime_calib", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.REVIEW
    assert "低置信" in out["reason"]
    assert any("回填率仅 7.7%" in w for w in out["warnings"])
    assert any("独立场景" in w for w in out["warnings"])
    assert out["backfill_rate"] == sg.pytest.approx(25 / 325, abs=0.001) \
        if hasattr(sg, "pytest") else out["backfill_rate"] > 0


def test_low_backfill_healthy_arm_collecting_not_promote(sg):
    now = 1_700_000_000_000.0
    # 60 records, 20 mature all beneficial → healthy but 33% backfill is above
    # 20% floor here; use 10 mature to land below floor with healthy rates.
    recs = [_rec(now, would_block=True, outcome="loss") for _ in range(10)]
    recs += [_rec(now, would_block=False) for _ in range(50)]
    # 10 mature < MIN_MATURE_OUTCOMES(20) → falls in the "not yet reconciled"
    # promote branch. To exercise the mature≥20 low-confidence path instead:
    recs = [_rec(now, would_block=True, outcome="loss") for _ in range(20)]
    recs += [_rec(now, would_block=False) for _ in range(180)]  # 10% backfill
    out = sg.grade_arm("reentry_cap", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == sg.COLLECTING
    assert any("回填率" in w for w in out["warnings"])


# ── M8: signal arms expose a manual harmful-rate note, never auto-REVIEW ─────

def test_signal_arm_harmful_outcomes_get_manual_note_only(sg):
    now = 1_700_000_000_000.0
    # 100% harmful signal arm: signal arms have no auto-REVIEW channel, so it
    # must NOT be REVIEW, but the human reviewer must see the rate explicitly.
    recs = [_rec(now, is_candidate=True, outcome="win") for _ in range(25)]
    recs += [_rec(now, is_candidate=False) for _ in range(35)]
    out = sg.grade_arm("xs_reversal", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] != sg.REVIEW
    assert "signal_harmful_rate_note" in out
    assert "100%" in out["signal_harmful_rate_note"]
    assert any("无自动有害率 REVIEW 通道" in w for w in out["warnings"])


# ── M12: zero-outcome backfill diagnostic ────────────────────────────────────

def test_zero_backfill_with_volume_warns(sg):
    now = 1_700_000_000_000.0
    # block arm: 70 fired records, zero outcomes → promotion-pending, but the
    # reconcile coverage warning must still be attached.
    recs = [_rec(now - 2 * H, would_block=True) for _ in range(70)]
    out = sg.grade_arm("reentry_cap", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert any("回填为 0" in w for w in out["warnings"])


def test_zero_backfill_below_promote_threshold_still_warns(sg):
    # M12：pullback 实测 42 条（< MIN_SAMPLES_PROMOTE=60）且全周零 outcome，
    # 诊断门槛独立为 ZERO_BACKFILL_MIN_SAMPLES=30，不能因样本<60 长期沉默。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 2 * H, tripped=True) for _ in range(42)]
    out = sg.grade_arm("pullback", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["verdict"] == "INSUFFICIENT_DATA"
    assert any("回填为 0/42" in w for w in out["warnings"])


def test_zero_backfill_under_min_samples_silent(sg):
    # 30 以下 = 刚上线噪音区间，不触发零回填告警。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 2 * H, tripped=True) for _ in range(20)]
    out = sg.grade_arm("pullback", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert not any("回填为 0" in w for w in (out.get("warnings") or []))


# ── M13: collection stall (records exist but none in 24h) ────────────────────

def test_collection_stall_flagged(sg):
    now = 1_700_000_000_000.0
    # 13 records all ~6 days old, nothing in the trailing 24h window.
    recs = [_rec(now - 6 * 24 * H, tripped=True) for _ in range(13)]
    out = sg.grade_arm("market_circuit", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs)
    assert out["collection_stalled"] is not None
    assert out["collection_stalled"]["window_h"] == 24
    assert any("采数停滞" in w for w in out["warnings"])
    # Verdict stays INSUFFICIENT (13 < 60); the stall rides alongside.
    assert out["verdict"] == sg.INSUFFICIENT


def test_no_stall_when_24h_has_records(sg):
    now = 1_700_000_000_000.0
    recs = [_rec(now - H, tripped=True) for _ in range(13)]
    out = sg.grade_arm("market_circuit", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs)
    assert "collection_stalled" not in out


def test_fresh_heartbeat_suppresses_stall_for_event_arm(sg):
    # M13 修正：market_circuit 每 tick 写心跳、仅 trip 才落事件流。24h 无事件
    # 但心跳只有 12s → 评估在跑，不算停滞。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 6 * 24 * H, tripped=True) for _ in range(13)]
    out = sg.grade_arm("market_circuit", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs, heartbeat_age_sec=12.0)
    assert "collection_stalled" not in out
    assert out["heartbeat_ok"] is True
    assert out["heartbeat_age_sec"] == 12.0
    assert not any("停采或写路径异常" in w for w in out.get("warnings", []))
    assert any("心跳正常" in w and "非采数停滞" in w for w in out["warnings"])


def test_stale_heartbeat_keeps_stall_for_event_arm(sg):
    # 心跳陈旧（超过 30min 阈值）→ 仍然判定停滞。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 6 * 24 * H, tripped=True) for _ in range(13)]
    out = sg.grade_arm("market_circuit", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs, heartbeat_age_sec=7200.0)
    assert "collection_stalled" in out
    assert out["heartbeat_ok"] is False
    assert any("采数停滞" in w for w in out["warnings"])


def test_pullback_no_heartbeat_still_flags_stall(sg):
    # pullback 没有每 tick 心跳（仅候选进入分支才评估），24h 无记录仍判停滞。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 31 * H, composite_score=50.0) for _ in range(42)]
    out = sg.grade_arm("pullback", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs, heartbeat_age_sec=None)
    assert "collection_stalled" in out
    assert "heartbeat_ok" not in out


# ── M16: 仅做多臂在宏观非多头期策略性不采数，不判停滞 ─────────────────────────

def test_pullback_macro_down_suppresses_stall(sg):
    # pullback 仅做多且 require_macro_uptrend：regime=down 时结构性不触发，
    # 近 24h 零写入是策略性不采数，不出停滞告警/横幅。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 31 * H, composite_score=50.0) for _ in range(42)]
    out = sg.grade_arm("pullback", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs, macro_regime="down")
    assert "collection_stalled" not in out
    assert out["macro_regime"] == "down"
    assert out["macro_blocks_collection"] is True
    assert not any("停采或写路径异常" in w for w in out.get("warnings", []))
    assert any("策略性不采数" in w and "非停采" in w for w in out["warnings"])


def test_pullback_macro_up_keeps_stall_when_no_records(sg):
    # 宏观是 up（本应有候选）却仍 24h 零写入 → 这才是真停滞，必须报警。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 31 * H, composite_score=50.0) for _ in range(42)]
    out = sg.grade_arm("pullback", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs, macro_regime="up")
    assert "collection_stalled" in out
    assert out["macro_blocks_collection"] is False
    assert any("采数停滞" in w for w in out["warnings"])


def test_pullback_macro_unknown_fails_open_to_stall(sg):
    # regime 探测失败传 None：fail-open，不退化为抑制，保持停滞以免掩盖故障。
    now = 1_700_000_000_000.0
    recs = [_rec(now - 31 * H, composite_score=50.0) for _ in range(42)]
    out = sg.grade_arm("pullback", "shadow", "p.jsonl", [24, 168],
                       now_ms=now, records=recs, macro_regime=None)
    assert "collection_stalled" in out
    assert "macro_regime" not in out



# ── M6: short-window outcomes_pending marker ─────────────────────────────────

def test_short_window_outcomes_pending_marker(sg):
    now = 1_700_000_000_000.0
    # Records inside 24h without outcomes; 168h window also has none here, so
    # check the per-window flag directly through window stats.
    recs = [_rec(now - H, would_block=True)]
    s24 = sg._window_stats(recs, "block", 24, now)
    assert s24["outcomes_pending"] is True
    s168 = sg._window_stats(
        [_rec(now - H, would_block=True, outcome="win")], "block", 168, now)
    assert s168["outcomes_pending"] is False  # flag only for sub-168h windows


# ── M9/M14: history source tagging + enriched slim snapshot ──────────────────

def test_slim_snapshot_source_tag_and_harm_fields(sg):
    d = {
        "generated_at": "2026-09-10 00:45 UTC",
        "windows_h": [168],
        "arms": [{
            "arm": "confidence_decay", "mode": "shadow", "kind": "change",
            "verdict": "COLLECTING", "backfill_rate": 0.195,
            "windows": [{"window_h": 168, "total": 3863, "hits": 985,
                         "decisions": 3863, "hit_rate": 0.255,
                         "mature_outcomes": 754, "outcome_wins": 340,
                         "outcome_losses": 414, "pnl_usd_sum": 0.0}],
        }],
        "real_baseline": {"real_closes": 19, "real_win_rate": 0.5263, "note": ""},
    }
    cron = sg._slim_snapshot(d, source="cron")
    assert cron["source"] == "cron" and cron["real_win_rate"] == 0.5263
    row = cron["arms"][0]
    assert row["outcome_wins"] == 340 and row["outcome_losses"] == 414
    assert row["harmful_rate"] == sg.pytest.approx(340 / 754, abs=0.001) \
        if False else abs(row["harmful_rate"] - 340 / 754) < 0.001
    manual = sg._slim_snapshot(d, source="manual")
    assert manual["source"] == "manual"


def test_read_history_filters_manual_snapshots(sg, tmp_path):
    import json
    hist = tmp_path / "h.jsonl"
    rows = []
    for i, src in enumerate(["cron", "manual", "cron"]):
        snap = {"ts": 1000 + i, "source": src, "arms": []}
        rows.append(snap)
    # legacy row without source → treated as cron
    rows.append({"ts": 1004, "arms": []})
    with open(hist, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    cron_only = sg.read_history(path=str(hist), source="cron")
    assert [r["ts"] for r in cron_only] == [1000, 1002, 1004]
    manual = sg.read_history(path=str(hist), source="manual")
    assert [r["ts"] for r in manual] == [1001]
    assert len(sg.read_history(path=str(hist))) == 4  # unfiltered


def _minimal_grade_dict(verdict="COLLECTING"):
    """最小可被 _slim_snapshot 接受的 collect_grades 载荷。"""
    return {
        "generated_at": "x",
        "windows_h": [168],
        "arms": [{
            "arm": "confidence_decay", "mode": "shadow", "kind": "change",
            "verdict": verdict, "backfill_rate": 0.2,
            "windows": [{"window_h": 168, "total": 100, "hits": 20,
                         "decisions": 100, "hit_rate": 0.2,
                         "mature_outcomes": 20, "outcome_wins": 5,
                         "outcome_losses": 15, "pnl_usd_sum": -1.0,
                         "outcomes_pending": False}],
        }],
        "real_baseline": {"real_closes": 19, "real_win_rate": 0.5},
    }


def test_cron_history_same_day_upsert_not_append(sg, tmp_path, monkeypatch):
    # M9 幂等：同一 UTC 自然日 cron 重跑只替换当日行，不堆叠多条。
    hist = tmp_path / "h.jsonl"
    day = 1_700_008_200_000.0
    d1 = _minimal_grade_dict()
    # _slim_snapshot 取 d['ts']？实际用 time.time；monkeypatch 其内部时间来源：
    # 直接在 append 后手动改写更稳——这里用 monkeypatch 冻结 time.time。
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: day / 1000.0)
    assert sg.append_history(d1, path=str(hist), source="cron")
    # 同日晚些时候重跑（同自然日，秒级不同）
    monkeypatch.setattr(_t, "time", lambda: (day + 5 * 3600_000) / 1000.0)
    d2 = _minimal_grade_dict("REVIEW")
    sg.append_history(d2, path=str(hist), source="cron")
    rows = sg.read_history(path=str(hist))
    assert len(rows) == 1, "同日 cron 必须去重为 1 条"
    assert rows[0]["arms"][0]["verdict"] == "REVIEW", "应保留最新载荷"


def test_cron_history_distinct_days_keep_both(sg, tmp_path, monkeypatch):
    import time as _t
    hist = tmp_path / "h.jsonl"
    day = 1_700_008_200_000.0
    monkeypatch.setattr(_t, "time", lambda: day / 1000.0)
    sg.append_history(_minimal_grade_dict(), path=str(hist), source="cron")
    monkeypatch.setattr(_t, "time", lambda: (day + 86_400_000) / 1000.0)
    sg.append_history(_minimal_grade_dict(), path=str(hist), source="cron")
    assert len(sg.read_history(path=str(hist))) == 2


def test_manual_history_not_deduped_against_cron(sg, tmp_path, monkeypatch):
    # manual 行不替换当日 cron 行，二者并存（趋势默认只读 cron）。
    import time as _t
    hist = tmp_path / "h.jsonl"
    day = 1_700_008_200_000.0
    monkeypatch.setattr(_t, "time", lambda: day / 1000.0)
    sg.append_history(_minimal_grade_dict(), path=str(hist), source="cron")
    sg.append_history(_minimal_grade_dict(), path=str(hist), source="manual")
    allrows = sg.read_history(path=str(hist))
    cron = sg.read_history(path=str(hist), source="cron")
    assert len(allrows) == 2 and len(cron) == 1


# ── 分母修正（2026-09-10）：harm rate denominator = hit-and-mature set ───────

def test_harm_rate_uses_hit_set_denominator(sg):
    # confidence_decay production profile: 754 mature overall but only the
    # would_block_gate=True rows are the arm's decisions. 340/754 = 45% on all
    # records; on the hit set it is much higher. Non-hit outcomes must not
    # dilute the rate.
    now = 1_700_000_000_000.0
    recs = []
    # Hit set: 200 mature, 45% harmful (90 win / 110 loss).
    for _ in range(90):
        recs.append(_rec(now, would_block_gate=True, outcome="win"))
    for _ in range(110):
        recs.append(_rec(now, would_block_gate=True, outcome="loss"))
    # Non-hit mature tail (the gate didn't act, outcomes irrelevant) +
    # non-mature records to mirror the real long tail.
    for _ in range(554):
        recs.append(_rec(now, would_block_gate=False, outcome="loss"))
    for _ in range(3109):
        recs.append(_rec(now, would_block_gate=False))
    out = sg.grade_arm("confidence_decay", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    # All-records win rate would be 90/754 = 12%; hit-set rate is 45%.
    assert out["hit_set_harmful_rate"] == sg.pytest.approx(0.45, abs=0.001) \
        if hasattr(sg, "pytest") else abs(out["hit_set_harmful_rate"] - 0.45) < 0.001
    assert out["harmful_rate_basis"] == "hit_set"
    assert out["verdict"] == sg.COLLECTING  # 45% grey zone, not PROMOTE
    assert "45%" in out["reason"]


def test_hit_set_below_min_falls_back_with_warning(sg):
    # Plenty of overall mature outcomes but only a few on hit rows → fall back
    # to all-records rate and warn the rate may understate harm.
    now = 1_700_000_000_000.0
    recs = [_rec(now, would_block=True, outcome="win") for _ in range(5)]
    recs += [_rec(now, would_block=False, outcome="loss") for _ in range(55)]
    out = sg.grade_arm("reentry_cap", "shadow", "p.jsonl", [168],
                       now_ms=now, records=recs)
    assert out["harmful_rate_basis"] == "all_records"
    assert any("命中集成熟样本仅 5" in w
               for w in out.get("warnings", []))
