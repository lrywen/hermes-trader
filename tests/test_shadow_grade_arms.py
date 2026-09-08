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
