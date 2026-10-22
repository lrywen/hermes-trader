"""Offline tests for reconcile_risk_tuning_shadow (no network).

Covers: candle simulation grading of block proposals (winner vs loser),
real-close join + leverage comparison, and stop_tuning wider-cap sim, with
_http_post and the memory file stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_risk_tuning",
    _REPO / "scripts" / "reconcile_risk_tuning_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_risk_tuning"] = mod
_spec.loader.exec_module(mod)


class _C:
    def __init__(self, t, o, h, l, c):
        self.t, self.o, self.h, self.l, self.c = t, o, h, l, c


def _iso(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _candles(entry_ts_iso: str, prices):
    """Build 1h candles; entry falls on the first bar at/after entry_ts."""
    t0 = datetime.fromisoformat(entry_ts_iso.replace("Z", "+00:00"))
    base = int(t0.timestamp() * 1000) - 3600_000
    out = []
    px = prices[0]
    for i, p in enumerate(prices):
        out.append(_C(base + i * 3600_000, px, max(px, p), min(px, p), p))
        px = p
    return out


_DSL = {"max_loss_pct": 1.0, "protect_pct": 2.5, "retrace_threshold": 0.5}


def _serve(cands):
    """Adapt a hand-built _C list to the raw candleSnapshot payload shape
    served by the stubbed _http_post (absolute open times preserved)."""
    rows = [{"t": c.t, "o": str(c.o), "h": str(c.h), "l": str(c.l),
             "c": str(c.c), "v": "1"} for c in cands]
    return lambda path, payload, *a, **k: rows


def test_block_proposal_graded_as_loser(monkeypatch):
    entry = 100.0
    # price immediately drops through the 1% stop → counter-factual loss
    cands = _candles(_iso(48), [100, 99.5, 98.5, 97])
    monkeypatch.setattr(mod, "_http_post", _serve(cands))
    r = {"timestamp": _iso(48), "rule": "breakout_score_floor",
         "coin": "AAA", "side": "long",
         "detail": {"entry_px": entry}, "outcome": None}
    ok = mod.grade_record(r, _DSL, {})
    assert ok is True
    assert r["outcome"] == "blocked_a_loser"
    assert r["pnl_pct"] < 0


def test_block_proposal_graded_as_winner(monkeypatch):
    entry = 100.0
    # climbs to +4% then holds above trailing → counter-factual gain
    cands = _candles(_iso(48), [100, 101, 102, 103, 104, 103.9, 103.8])
    monkeypatch.setattr(mod, "_http_post", _serve(cands))
    r = {"timestamp": _iso(48), "rule": "per_coin_cooldown",
         "coin": "BBB", "side": "long",
         "detail": {"entry_px": entry}, "outcome": None}
    ok = mod.grade_record(r, _DSL, {})
    assert ok is True
    assert r["outcome"] == "blocked_a_winner"
    assert r["pnl_pct"] > 0


def test_leverage_tier_real_close_comparison(tmp_path, monkeypatch):
    sig_iso = _iso(48)
    sig_ms = int(datetime.fromisoformat(
        sig_iso.replace("Z", "+00:00")).timestamp() * 1000)
    # real close -0.83% spot within join window
    mem = {"closes": [{"coin": "ZEC", "closed_at": sig_ms + 3600_000,
                       "spot_pct": -0.83, "realized_pnl_usd": -0.22,
                       "leverage": 10}]}
    mf = tmp_path / "mem.json"
    mf.write_text(json.dumps(mem))
    monkeypatch.setattr(mod, "MEMORY_FILE", str(mf))
    closes = mod._load_real_closes()

    r = {"timestamp": sig_iso, "rule": "leverage_tier", "coin": "ZEC",
         "side": "long",
         "detail": {"live_leverage": 10, "proposed_leverage": 5,
                    "entry_px": 1252.4}, "outcome": None}
    ok = mod.grade_record(r, _DSL, closes)
    assert ok is True
    assert r["outcome"] == "deleverage_avoids_loss"
    # 5x must show a smaller absolute loss than 10x
    assert abs(r["proposed_pnl_pct"]) < abs(r["live_pnl_pct"])
    assert r["live_pnl_pct"] < 0


def test_stop_tuning_wider_cap_no_network_flags(tmp_path, monkeypatch):
    # With no adverse bars before a recovery, wider cap turns green vs the
    # tight live cap that stops out.
    sig = _iso(48)
    # live cap 0.8%: low of bar1 = 99.0 => -1.0% stops the live sim;
    # wider 1.5% cap survives; price then rallies to +3%.
    cands = []
    t0 = int(datetime.fromisoformat(sig.replace("Z", "+00:00")).timestamp()*1000)
    series = [(100, 100), (99.0, 100), (101, 101), (102.5, 102.5), (103, 103)]
    for i, (lo, c) in enumerate(series):
        cands.append(_C(t0 + i*3600_000, c, c, lo, c))
    monkeypatch.setattr(mod, "_http_post", _serve(cands))

    cfg = {"max_loss_pct": 0.8, "protect_pct": 2.5, "retrace_threshold": 0.5}
    r = {"timestamp": sig, "rule": "stop_tuning", "coin": "ADA",
         "side": "long", "would": "survive_wider_cap",
         "detail": {"entry_px": 100.0, "live_spot_cap_pct": 0.8,
                    "candidate_max_loss_pct": 1.5}, "outcome": None}
    ok = mod.grade_record(r, cfg, {})
    assert ok is True
    assert "wider_cap_sim" in r
    assert r["wider_cap_sim"]["pnl_pct"] > r["live_cap_sim"]["pnl_pct"]


# ── exit_outcome record-only protect A/B aggregation (2026-10-21) ─────────

def _exit_row(regime, reason, spot, roe, mfe, live_p, cand_p,
              earlier, unlock):
    return {"rule": "exit_outcome", "coin": "X", "side": "long",
            "exit_reason": reason,
            "detail": {"entry_regime": regime, "realized_spot_pct": spot,
                       "realized_roe_pct": roe, "mfe_spot_pct": mfe,
                       "live_protect_pct": live_p,
                       "candidate_protect_pct": cand_p,
                       "protect_arms_earlier": earlier,
                       "protect_would_unlock_phase2": unlock}}


def test_regime_bucket_avoids_non_trend_substring_trap():
    # "non_trend" contains the substring "trend"; prefix classification must
    # still land it in the non_trend bucket.
    assert mod._regime_bucket("uptrendMomentum") == "trend"
    assert mod._regime_bucket("downtrendMomentum") == "trend"
    assert mod._regime_bucket("trend_ride") == "trend"
    assert mod._regime_bucket("non_trend") == "non_trend"
    assert mod._regime_bucket("neutral") == "non_trend"
    assert mod._regime_bucket("chop") == "non_trend"
    assert mod._regime_bucket("") == "non_trend"


def test_exit_outcome_summary_buckets_and_tags(capsys):
    records = [
        # trend: one would-unlock (peak 1.8 < live 2.5, >= cand 1.5)
        _exit_row("uptrendMomentum", "stale_flat_timeout", -0.4, -1.2, 1.8,
                  2.5, 1.5, True, True),
        # trend: already armed live (winner, floor breach)
        _exit_row("downtrendMomentum", "floor_breach", 3.0, 9.0, 4.0,
                  2.5, 1.5, True, False),
        # non_trend: one would-unlock at the 1.0 candidate
        _exit_row("neutral", "stale_flat_timeout", -0.2, -0.6, 1.2,
                  1.5, 1.0, True, True),
        # non_trend: never arms
        _exit_row("chop", "max_loss", -1.6, -4.8, 0.5,
                  1.5, 1.0, False, False),
    ]
    mod._summarize_exit_outcomes(records)
    out = capsys.readouterr().out
    assert "real closes recorded = 4" in out
    assert "[trend]" in out and "[non_trend]" in out
    # each bucket has 2 rows
    assert "[trend] live protect=2.5  candidate protect=1.5" in out
    assert "[non_trend] live protect=1.5  candidate protect=1.0" in out
    # the two stale timeouts are flagged as would-unlock phase-2
    assert "stale_flat_timeout" in out
    stale_line = [ln for ln in out.splitlines()
                  if ln.strip().startswith("stale_flat_timeout")][0]
    assert stale_line.rstrip().endswith("would-unlock phase2: 2")


def test_exit_outcome_summary_inert_without_candidate(capsys):
    records = [_exit_row("trend", "floor_breach", 1.0, 3.0, 3.0,
                         2.5, None, False, False)]
    mod._summarize_exit_outcomes(records)
    out = capsys.readouterr().out
    assert "real closes recorded = 1" in out
    # no candidate -> no arms/unlock split printed
    assert "arms earlier" not in out


def test_exit_outcome_summary_empty(capsys):
    mod._summarize_exit_outcomes([])
    assert "no real-close rows recorded yet" in capsys.readouterr().out


# ── D2 ignite_grace_cf: snapshot/exit pairing + pre-registered gate ───────

def _iso_s(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


_BASE_S = int(datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def _cf_row(coin, entry_s, regime, cf_roe, lev=3):
    """Snapshot at candidate 45-min grace, still unignited."""
    spot = cf_roe / lev
    return {"timestamp": _iso_s(entry_s + 45 * 60),
            "rule": "ignite_grace_cf", "coin": coin, "side": "long",
            "would": "earlier_ignite_exit",
            "detail": {"entry_ts": float(entry_s),
                       "entry_iso": _iso_s(entry_s),
                       "entry_regime": regime,
                       "regime_bucket": mod._regime_bucket(regime),
                       "candidate_grace_minutes": 45.0,
                       "cf_close_min": 45.0,
                       "cf_unrealized_spot_pct": spot,
                       "cf_unrealized_roe_pct": float(cf_roe),
                       "was_ignited": False,
                       "entry_px": 100.0,
                       "mark_px": 100.0 + spot,
                       "leverage": lev}}


def _d2_exit_row(coin, entry_s, hold_min, regime, real_roe, reason,
                 lev=3, include_entry_ts=True):
    d = {"entry_regime": regime,
         "realized_spot_pct": real_roe / lev,
         "realized_roe_pct": float(real_roe),
         "mfe_spot_pct": 0.5, "leverage": lev}
    if include_entry_ts:
        d["entry_ts"] = float(entry_s)
        d["entry_iso"] = _iso_s(entry_s)
    return {"timestamp": _iso_s(entry_s + hold_min * 60),
            "rule": "exit_outcome", "coin": coin, "side": "long",
            "exit_reason": reason, "detail": d}


def _pair_records(n, cf_roe=2.0, real_roe=-4.0, regime="neutral",
                  reason="stale_flat_timeout", lev=3, block_step=900):
    recs = []
    for i in range(n):
        entry_s = _BASE_S + i * block_step
        recs.append(_cf_row(f"C{i:02d}", entry_s, regime, cf_roe, lev))
        recs.append(_d2_exit_row(f"C{i:02d}", entry_s, 90, regime,
                                 real_roe, reason, lev))
    return recs


def test_d2_no_snapshots(capsys):
    mod._summarize_ignite_grace_cf([])
    out = capsys.readouterr().out
    assert "no ignite_grace_cf snapshots recorded yet" in out


def test_d2_net_fee_both_arms_keeps_delta_invariant():
    # 5 bps round trip on notional -> 0.05% * leverage ROE points per arm.
    assert mod._net_roe_pct(2.0, 3) == 2.0 - 0.15
    assert mod._net_roe_pct(-4.0, 3) == -4.0 - 0.15
    # identical leverage on both arms -> paired delta is fee-invariant
    assert (mod._net_roe_pct(2.0, 10) - mod._net_roe_pct(-4.0, 10)) == 6.0


def test_d2_exact_entry_ts_join_and_paired_delta(capsys):
    entry_s = _BASE_S
    records = [_cf_row("AAA", entry_s, "neutral", 2.0, lev=3),
               _d2_exit_row("AAA", entry_s, 90, "neutral", -4.0,
                            "stale_flat_timeout", lev=3)]
    mod._summarize_ignite_grace_cf(records)
    out = capsys.readouterr().out
    assert "snapshots=1  joined_pairs=1" in out
    assert "maturing(open,<8h)=0  unmatched_gap=0" in out
    # gross delta 6.0 ROE points; fee nets out of the paired difference
    assert "mean= +6.00 pp" in out
    # real median hold 90 min vs cf 45 min
    assert "real median= 90.0" in out


def test_d2_nearest_fallback_for_legacy_exit_without_entry_ts(capsys):
    entry_s = _BASE_S
    cf = _cf_row("AAA", entry_s, "chop", 1.0, lev=2)
    ex = _d2_exit_row("AAA", entry_s, 120, "chop", -2.0,
                      "stale_flat_timeout", lev=2, include_entry_ts=False)
    mod._summarize_ignite_grace_cf([cf, ex])
    out = capsys.readouterr().out
    assert "joined_pairs=1" in out
    assert "unmatched_gap=0" in out


def test_d2_maturing_and_gap_when_no_exit(capsys):
    recent_entry = int(datetime.now(timezone.utc).timestamp()) - 45 * 60
    recent_cf = _cf_row("NEW", recent_entry, "neutral", 0.5)
    old_entry = int(datetime.now(timezone.utc).timestamp()) - 48 * 3600
    old_cf = _cf_row("OLD", old_entry, "neutral", -1.0)
    mod._summarize_ignite_grace_cf([recent_cf, old_cf])
    out = capsys.readouterr().out
    assert "joined_pairs=0" in out
    assert "maturing(open,<8h)=1" in out
    assert "unmatched_gap=1" in out


def test_d2_one_exit_consumed_once_second_snapshot_is_gap(capsys):
    entry_s = _BASE_S
    # two snapshots (same coin/side/entry_ts, e.g. guard failed) share one exit
    cf1 = _cf_row("AAA", entry_s, "neutral", 1.0)
    cf2 = _cf_row("AAA", entry_s, "neutral", 1.0)
    ex = _d2_exit_row("AAA", entry_s, 90, "neutral", -3.0,
                      "stale_flat_timeout")
    mod._summarize_ignite_grace_cf([cf1, cf2, ex])
    out = capsys.readouterr().out
    assert "snapshots=2  joined_pairs=1" in out
    assert "unmatched_gap=1" in out


def test_d2_trend_only_pairs_render_no_verdict_without_divzero(capsys):
    # trend pairs are excluded; with zero non_trend pairs the report must not
    # crash on improve-rate division by zero.
    mod._summarize_ignite_grace_cf(_pair_records(2, regime="uptrendMomentum"))
    out = capsys.readouterr().out
    assert "2 pairs bucketed trend" in out
    assert "no paired non_trend exits yet" in out


def test_d2_below_min_pairs_prints_no_verdict(capsys):
    mod._summarize_ignite_grace_cf(_pair_records(5))
    out = capsys.readouterr().out
    assert "NO VERDICT: only 5/30 pairs" in out
    assert "block bootstrap" not in out
    assert "[WAIT] n=5 >= 30" in out


def test_d2_floor_breach_giveup_cluster_counted(capsys):
    entry_a = _BASE_S
    entry_b = _BASE_S + 900
    # floor_breach where the REAL arm beats the cf arm -> foregone give-up
    cf_give = _cf_row("GIV", entry_a, "neutral", 1.0)
    ex_give = _d2_exit_row("GIV", entry_a, 90, "neutral", 5.0,
                           "floor_breach")
    # floor_breach where cf still wins -> NOT part of the give-up cluster
    cf_ok = _cf_row("OKC", entry_b, "neutral", 4.0)
    ex_ok = _d2_exit_row("OKC", entry_b, 90, "neutral", 2.0,
                         "floor_breach")
    mod._summarize_ignite_grace_cf([cf_give, ex_give, cf_ok, ex_ok])
    out = capsys.readouterr().out
    assert "floor_breach give-up cluster: 1 pairs" in out
    # foregone = (5-0.15) - (1-0.15) = 4.0 pp
    assert "foregone sum=+4.00 pp" in out


def test_d2_block_bootstrap_deterministic_seed():
    pairs = _pair_records(10)
    # rebuild pair deltas like the summarizer (join already validated above)
    pp = [{"entry_ts": r["detail"]["entry_ts"], "delta_net_roe": 6.0}
          for r in pairs if r["rule"] == "ignite_grace_cf"]
    a = mod._block_bootstrap_mean_ci(pp, 500, mod.D2_BOOT_SEED)
    b = mod._block_bootstrap_mean_ci(pp, 500, mod.D2_BOOT_SEED)
    assert a == b
    # same-entry-block pairs must resample together: one block -> exact mean
    same_block = [{"entry_ts": _BASE_S, "delta_net_roe": 6.0},
                  {"entry_ts": _BASE_S + 30, "delta_net_roe": -2.0}]
    lo, hi = mod._block_bootstrap_mean_ci(same_block, 100, mod.D2_BOOT_SEED)
    assert lo == hi == 2.0


def test_wilcoxon_upper_tail_direction():
    # H1 median delta > 0: all-positive must be significant (upper tail);
    # all-negative must not. Earlier code used the lower tail and inverted.
    p_pos = mod._wilcoxon_one_sided([1.0] * 30)
    p_neg = mod._wilcoxon_one_sided([-1.0] * 30)
    assert p_pos is not None and p_pos < 0.05
    assert p_neg is not None and p_neg > 0.95
    assert mod._wilcoxon_one_sided([0.0, 0.0]) is None


def test_d2_all_gates_pass_at_n30(capsys):
    mod._summarize_ignite_grace_cf(_pair_records(30))
    out = capsys.readouterr().out
    assert "[non_trend] n=30" in out
    assert "improve rate=100.0%" in out
    assert "95% CI" in out
    assert "one-sided paired Wilcoxon p" in out
    assert "shorter under cf: YES" in out
    assert out.count("[PASS]") == 6
    assert "ALL GATES PASS" in out


def test_d2_gate_not_met_with_mixed_sample(capsys):
    recs = []
    for i in range(30):
        entry_s = _BASE_S + i * 900
        real_roe = 3.0 if i % 2 == 0 else -3.0  # 15 worse / 15 improved
        recs += [_cf_row(f"M{i:02d}", entry_s, "neutral", 0.0),
                 _d2_exit_row(f"M{i:02d}", entry_s, 90, "neutral",
                              real_roe, "stale_flat_timeout")]
    mod._summarize_ignite_grace_cf(recs)
    out = capsys.readouterr().out
    assert "pairs improved=15  worse=15" in out
    assert "GATE NOT MET" in out
    assert "[PASS] n=30 >= 30" in out
    assert "[WAIT] majority of pairs improved" in out
