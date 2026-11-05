import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from hermes_trader.validation.trial_ledger import (  # noqa: E402
    TrialLedger,
    TrialLedgerError,
    deflated_sharpe_from_ledger,
    dsr_trial_evidence,
)
from scripts import mint_live_gate_record as mint  # noqa: E402
from scripts import shadow_control_group as control  # noqa: E402
from scripts import shadow_grade as grade  # noqa: E402
from scripts import validate_outcome as outcome  # noqa: E402
from scripts import wfa_main_strategy as wfa  # noqa: E402

AS_OF = "2026-02-01T00:00:00Z"


def _record(trial_id, *, arm="arm_a", paradigm="daily_outcome", iteration=1,
            test_at="2026-01-02T00:00:00Z", source="preregistered", **overrides):
    legacy = source == "legacy_import"
    rec = {
        "ledger_version": 2,
        "trial_id": trial_id,
        "family": f"{arm}::{paradigm}",
        "arm": arm,
        "paradigm": paradigm,
        "iteration": iteration,
        "test_at": test_at,
        "recorded_at": test_at,
        "preregistration": {
            "status": "legacy_import" if legacy else "preregistered",
            "registered_at": None if legacy else "2026-01-01T00:00:00Z",
            "prospective": not legacy,
        },
        "source": source,
        "status": "active",
        "provenance": {"adr": "docs/adr/test.md", "research": "research/test.json"},
    }
    rec.update(overrides)
    return rec


def _write_ledger(tmp_path, records, name="ledger.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _evidence(tmp_path, records, *, arm="arm_a", paradigm="daily_outcome",
              as_of=AS_OF):
    return dsr_trial_evidence(
        arm=arm, paradigm=paradigm, as_of=as_of,
        ledger_path=_write_ledger(tmp_path, records))


def test_family_count_is_arm_by_paradigm_and_as_of(tmp_path):
    records = [
        _record("a1"),
        _record("a2", iteration=2, test_at="2026-01-03T00:00:00Z",
                source="legacy_import"),
        _record("other-paradigm", paradigm="shadow_forward"),
        _record("other-arm", arm="arm_b"),
    ]
    path = _write_ledger(tmp_path, records)
    early = dsr_trial_evidence(
        arm="arm_a", paradigm="daily_outcome",
        as_of="2026-01-02T23:59:59Z", ledger_path=path)
    assert early["n_trials"] == 1
    assert early["legacy_n_trials"] == 0
    later = dsr_trial_evidence(
        arm="arm_a", paradigm="daily_outcome", as_of=AS_OF, ledger_path=path)
    assert later["n_trials"] == 2
    assert later["legacy_n_trials"] == 1
    assert later["preregistered_n_trials"] == 1
    assert later["promotion_eligible"] is True
    assert dsr_trial_evidence(
        arm="arm_a", paradigm="shadow_forward", as_of=AS_OF,
        ledger_path=path)["n_trials"] == 1
    assert dsr_trial_evidence(
        arm="arm_b", paradigm="daily_outcome", as_of=AS_OF,
        ledger_path=path)["n_trials"] == 1


def test_legacy_counts_but_cannot_alone_support_promotion(tmp_path):
    records = [
        _record(f"legacy-{i:02d}", iteration=i, source="legacy_import")
        for i in range(1, 17)
    ]
    evidence = _evidence(tmp_path, records)
    assert evidence["n_trials"] == 16
    assert evidence["legacy_n_trials"] == 16
    assert evidence["preregistered_n_trials"] == 0
    assert evidence["promotion_eligible"] is False
    assert evidence["promotion_block"] == "legacy_import_only"


def test_duplicate_id_and_active_iteration_fail_closed(tmp_path):
    with pytest.raises(TrialLedgerError) as exc:
        _evidence(tmp_path, [_record("same"), _record("same", iteration=2)])
    assert exc.value.code == "duplicate_trial_id"
    with pytest.raises(TrialLedgerError) as exc:
        _evidence(tmp_path, [_record("one"), _record("two")])
    assert exc.value.code == "duplicate_iteration"
    with pytest.raises(TrialLedgerError) as exc:
        _evidence(tmp_path, [
            _record("one"), _record("two", supersedes="unrelated")])
    assert exc.value.code == "duplicate_iteration"


def test_superseding_correction_respects_as_of(tmp_path):
    original = _record("original", test_at="2026-01-02T00:00:00Z")
    correction = _record("correction", test_at="2026-01-03T00:00:00Z",
                         supersedes="original")
    path = _write_ledger(tmp_path, [original, correction])
    early = dsr_trial_evidence(
        arm="arm_a", paradigm="daily_outcome",
        as_of="2026-01-02T23:59:59Z", ledger_path=path)
    assert early["trial_ids"] == ["original"]
    later = dsr_trial_evidence(
        arm="arm_a", paradigm="daily_outcome", as_of=AS_OF, ledger_path=path)
    assert later["trial_ids"] == ["correction"]
    assert later["n_trials"] == 1


def test_retracted_and_superseded_records_do_not_count(tmp_path):
    records = [
        _record("active", iteration=1),
        _record("retracted", iteration=2, status="retracted", retracted=True),
        _record("old", iteration=3, status="superseded", superseded_by="new"),
        _record("new", iteration=4),
    ]
    evidence = _evidence(tmp_path, records)
    assert evidence["trial_ids"] == ["active", "new"]
    assert evidence["n_trials"] == 2


def test_invalid_preregistration_is_rejected(tmp_path):
    forged = _record("forged", source="legacy_import")
    forged["preregistration"]["registered_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(TrialLedgerError) as exc:
        _evidence(tmp_path, [forged])
    assert exc.value.code == "legacy_preregistration_forged"

    late = _record("late", test_at="2025-12-31T00:00:00Z")
    with pytest.raises(TrialLedgerError) as exc:
        _evidence(tmp_path, [late])
    assert exc.value.code == "preregistration_after_test"


def test_missing_unreadable_unknown_as_of_and_coverage_fail_closed(tmp_path):
    with pytest.raises(TrialLedgerError) as exc:
        dsr_trial_evidence(
            arm="arm_a", paradigm="daily_outcome", as_of=AS_OF,
            ledger_path=tmp_path / "missing.jsonl")
    assert exc.value.code == "ledger_missing"

    bad = tmp_path / "bad.jsonl"
    bad.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(TrialLedgerError) as exc:
        dsr_trial_evidence(
            arm="arm_a", paradigm="daily_outcome", as_of=AS_OF, ledger_path=bad)
    assert exc.value.code == "ledger_unreadable"

    path = _write_ledger(tmp_path, [_record("one")])
    with pytest.raises(TrialLedgerError) as exc:
        dsr_trial_evidence(
            arm="arm_a", paradigm="daily_outcome", as_of=None, ledger_path=path)
    assert exc.value.code == "as_of_required"
    with pytest.raises(TrialLedgerError) as exc:
        dsr_trial_evidence(
            arm="unknown", paradigm="daily_outcome", as_of=AS_OF,
            ledger_path=path)
    assert exc.value.code == "family_not_covered"
    with pytest.raises(TrialLedgerError) as exc:
        dsr_trial_evidence(
            arm="arm_a", paradigm="daily_outcome",
            as_of="2025-01-01T00:00:00Z", ledger_path=path)
    assert exc.value.code == "family_empty_as_of"


def test_wfa_and_r5_never_fall_back_to_default_16(tmp_path):
    series = [4.0, 5.0, 3.0, 6.0] * 6
    missing = tmp_path / "absent.jsonl"
    stats = wfa.main_strategy_wfa(series, n_trials=16, as_of=AS_OF,
                                  ledger_path=missing)
    assert stats["verdict"] == "fail_closed"
    assert stats["dsr_p"] is None
    assert stats["n_trials"] is None
    assert stats["dsr_error"]["code"] == "ledger_missing"

    records = [{"ts": i * 86_400_000, "pnl_pct": v}
               for i, v in enumerate(series + [4.0] * 7)]
    r5 = grade.r5_robustness(records, n_trials=16, arm="arm_a", as_of=AS_OF,
                             ledger_path=missing)
    assert r5["verdict"] == "fail_closed"
    assert r5["dsr_p"] is None
    assert r5["n_trials"] is None
    assert r5["dsr_error"]["code"] == "ledger_missing"


def test_validate_partial_family_coverage_is_fail_closed(tmp_path):
    source = tmp_path / "trades.jsonl"
    with source.open("w", encoding="utf-8") as fh:
        for arm in ("filt", "filt_ra"):
            for day in range(60):
                fh.write(json.dumps({
                    "type": "trade", "arm": arm, "entry_t": day * 86_400_000,
                    "notional": 10000, "pnl_net": 5.0 + day % 3,
                }) + "\n")
    path = _write_ledger(tmp_path, [_record("filt", arm="filt")])
    result = outcome.evaluate(str(source), ("filt", "filt_ra"), 100, 1,
                              as_of=AS_OF, ledger_path=path, output_path=None)
    assert result["verdict"] == "fail_closed"
    assert result["arms"][0]["dsr_error"] is None
    assert result["arms"][1]["dsr_p"] is None
    assert result["arms"][1]["dsr_error"]["code"] == "family_not_covered"
    assert result["promotion_eligible"] is False


def _mint_result(tmp_path, source):
    path = _write_ledger(tmp_path, [_record("candidate", arm="filt", source=source)])
    evidence = deflated_sharpe_from_ledger(
        observed_sharpe=4.0, n_obs=60, arm="filt", paradigm="daily_outcome",
        as_of=AS_OF, ledger_path=path)
    result = {
        "verdict": "POSITIVE_EDGE_POSSIBLE", "as_of": AS_OF,
        "arms": [{"arm": "filt", "days": 60, "sharpe": 4.0,
                  "dsr_p": round(evidence["dsr_p"], 4),
                  "bb_positive": True, "bb_ci": [1.0, 2.0]}],
    }
    return result, path


def test_mint_recomputes_evidence_and_rejects_legacy_only(tmp_path):
    result, path = _mint_result(tmp_path, "preregistered")
    selected = mint._select_positive_arm(result, ledger_path=path)
    assert selected["trial_evidence"]["promotion_eligible"] is True
    assert selected["n_trials"] == 1

    legacy_tmp = tmp_path / "legacy"
    legacy_tmp.mkdir()
    legacy_result, legacy_path = _mint_result(legacy_tmp, "legacy_import")
    with pytest.raises(SystemExit) as exc:
        mint._select_positive_arm(legacy_result, ledger_path=legacy_path)
    assert "legacy_import_only" in str(exc.value)


def test_mint_preserves_structured_ledger_error(tmp_path):
    result = {"verdict": "POSITIVE_EDGE_POSSIBLE", "arms": [{
        "arm": "filt", "days": 24, "sharpe": 1.0, "dsr_p": 0.99,
        "bb_positive": True, "bb_ci": [1.0, 2.0],
    }]}
    with pytest.raises(SystemExit) as exc:
        mint._select_positive_arm(
            result, as_of=AS_OF, ledger_path=tmp_path / "missing.jsonl")
    payload = json.loads(str(exc.value).split("refusing to mint: ", 1)[1])
    assert payload["error"]["code"] == "dsr_fail_closed"
    assert payload["error"]["details"]["errors"][0]["code"] == "ledger_missing"


def test_seed_ledger_is_strict_and_main_without_traceable_trials_fails_closed():
    ledger = TrialLedger.load_strict()
    records = ledger.load(required=True, strict=True)
    assert len(records) == 6
    assert not any(t["family"] == "main::wfa_main_strategy" for t in records)
    stats = wfa.main_strategy_wfa(
        [4.0, 5.0, 3.0, 6.0] * 6, as_of=AS_OF, ledger=ledger)
    assert stats["verdict"] == "fail_closed"
    assert stats["dsr_p"] is None
    assert stats["n_trials"] is None
    assert stats["dsr_error"]["code"] == "family_not_covered"
    assert all(t["preregistration"]["registered_at"] is None for t in records
               if t["preregistration"]["status"] == "legacy_import")


def test_control_group_covers_registry_and_separates_ineligible_from_no_data(
        tmp_path, monkeypatch):
    paths = {}
    for arm in ("daily_extension_cap", "market_circuit", "xs_reversal", "unknown"):
        paths[arm] = tmp_path / f"{arm}.jsonl"
        paths[arm].write_text("", encoding="utf-8")
    paths["daily_extension_cap"].write_text(
        json.dumps({"blocked": True, "pnl_pct": 2.0, "macro_regime": "up"}) + "\n" +
        json.dumps({"blocked": False, "pnl_pct": -1.0, "macro_regime": "up"}) + "\n",
        encoding="utf-8")
    registry = [
        (arm, "test_block", "TEST_SHADOW_FILE", f"{arm}.jsonl",
         "shadow_log_path", f"{arm}_mode")
        for arm in ("daily_extension_cap", "market_circuit", "xs_reversal", "unknown")
    ]
    monkeypatch.setattr(control.sp, "ARMS", registry)
    monkeypatch.setattr(control, "_agent_config", lambda: {})
    monkeypatch.setattr(
        control.sp, "_arm_mode",
        lambda cfg, blk, env, label="", mode_key="": (
            "off" if label == "market_circuit" else "shadow"))
    monkeypatch.setattr(
        control.sp, "_arm_path",
        lambda cfg, blk, env, default, path_key="shadow_log_path", label="": str(
            paths.get(label, tmp_path / f"{label}.jsonl")))
    candidate_path = tmp_path / "candidate.jsonl"
    candidate_path.write_text(
        json.dumps({"is_candidate": True, "pnl_pct": 3.0}) + "\n" +
        json.dumps({"is_candidate": False, "pnl_pct": 1.0}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(control, "_CANDIDATE_PATH", str(candidate_path))
    report = control.evaluate_control_group(n_boot=50, seed=1)
    rows = {row["arm"]: row for row in report["arms"]}
    assert set(report["covered_registered_arms"]) == {
        entry[0] for entry in control.sp.ARMS}
    assert rows["daily_extension_cap"]["status"] == "ok"
    assert rows["daily_extension_cap"]["hit_n"] == 1
    assert rows["market_circuit"]["status"] == "not_eligible"
    assert rows["market_circuit"]["hit_n"] is None
    assert rows["unknown"]["status"] == "not_eligible"
    assert rows["unknown"]["hit_n"] is None
    assert rows["xs_reversal"]["status"] == "no_data"
    assert rows["xs_reversal"]["hit_n"] == 0
    assert rows["spread_observe"]["status"] == "not_eligible"
    assert rows["weight_shadow"]["status"] == "superseded"
    assert rows["weight_shadow"]["generates_candidates"] is False
    assert rows["weight_shadow"]["min_composite"] == 40
    assert rows["candidate_gate_shadow"]["status"] == "no_data"
    assert rows["candidate_gate_shadow"]["reason"] == "baseline_unavailable"
    assert rows["candidate_gate_shadow"]["hit_n"] == 0
    assert rows["candidate_gate_shadow"]["baseline_n"] == 0
    assert rows["candidate_gate_shadow"]["skipped_n"] == 2
    assert rows["candidate_gate_shadow"]["min_composite"] == 40


def test_control_group_reads_production_candidate_schema_without_baseline(
        tmp_path, monkeypatch):
    candidate_path = tmp_path / "candidate_gate_shadow.jsonl"
    candidate_path.write_text(
        json.dumps({
            "ts": 1_762_000_000_000, "coin": "BTC", "composite_score": 42.0,
            "band_lo": 40, "band_hi": 45, "shadow": True,
            "verdict": "LONG", "fwd72_return": 3.0,
        }) + "\n" +
        json.dumps({"band_lo": 45, "shadow": True, "fwd72_return": 9.0}) + "\n" +
        json.dumps({"band_lo": 40, "shadow": False, "fwd72_return": 8.0}) + "\n" +
        json.dumps({"band_lo": "bad", "shadow": True, "fwd72_return": 7.0}) + "\n" +
        json.dumps({"band_lo": 40, "shadow": True}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(control.sp, "ARMS", [])
    monkeypatch.setattr(control, "_agent_config", lambda: {})
    monkeypatch.setattr(control, "_CANDIDATE_PATH", str(candidate_path))

    report = control.evaluate_control_group(n_boot=10, seed=1)
    row = next(r for r in report["arms"] if r["arm"] == "candidate_gate_shadow")
    assert row["status"] == "no_data"
    assert row["reason"] == "baseline_unavailable"
    assert row["min_composite"] == 40
    assert row["hit_n"] == 1
    assert row["hit"] == [3.0]
    assert row["baseline_n"] == 0
    assert row["baseline"] == []
    assert row["skipped_n"] == 4
