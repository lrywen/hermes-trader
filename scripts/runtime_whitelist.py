"""P0-1 — single source of truth for scripts/ shipped in the runtime image.

Only the scripts with a proven IN-CONTAINER runtime caller are whitelisted.
Everything else under scripts/ (nine ``backtest_*`` research/replay scripts,
``backfill_*`` / calibration tools, and one-off ops helpers) stays out of the
production image. Scripts that run on the HOST (``backup_state.py``,
``calibrate_regime_thresholds.py``, the ``cron_*.sh`` / ``weekly_*.sh``
wrappers, ``deploy_prod.sh``) are deliberately excluded: they execute via the
host Python / ``docker exec`` and never need an in-image copy of themselves.

Call sites that justify each entry (verified 2026-09-18):
  * Process entrypoints (docker-compose command, k8s statefulset):
      trading_loop.py, ip_drift_watch.py, scheduler.py
  * scheduler.SCHEDULED_JOBS (in-container subprocess):
      clean_trades_raw.py, daily_report.py, pullback_shadow_daily.py,
      reconcile_selection_attribution.py, reconcile_ta_late_entry_shadow.py,
      reconcile_xs_reversal_shadow.py, reconcile_relax_tier_shadow.py,
      macro_regime_watch.py
  * Web UI spawn / lazy import (dashboard_routes/shadow_arms.py):
      regen_param_sweep.py, shadow_grade.py
  * Shared scripts/-local helper imported by two of the above:
      shadow_progress.py  (shadow_grade.py + reconcile_change_arms_shadow.py
      both `import shadow_progress`; it has no further scripts/-local deps)
  * Host cron wrappers that `docker exec` INTO the container:
      cron_reconcile.sh -> reconcile_fills.py, reconcile_change_arms_shadow.py,
        reconcile_pullback_shadow.py, reconcile_daily_extension_cap_shadow.py
        (xs_reversal / relax_tier already listed via scheduler)
      cron_shadow_grade.sh -> shadow_grade.py (already listed)
  * Deploy smoke (scripts/deploy_prod.sh `docker exec ... postdeploy_smoke.py`):
      postdeploy_smoke.py
  * One-shot in-container research runner (2026-10-23, U1):
      m1_ofi_eval.py  (M-1 OFI evaluator; NOT in the trading runtime path —
      shipped only because the host cannot read the /data docker volume, so
      the 2026-10-28 sample-out evaluation runs via `docker exec`)

tests/test_runtime_scripts_whitelist.py enforces that every entry exists, that
no research/backtest script is listed, and that the Dockerfile COPYs exactly
this set.
"""
from __future__ import annotations

RUNTIME_SCRIPTS: tuple[str, ...] = (
    # Process entrypoints.
    "trading_loop.py",
    "ip_drift_watch.py",
    "scheduler.py",
    # scheduler.SCHEDULED_JOBS.
    "clean_trades_raw.py",
    "reconcile_gate_outcomes.py",
    "daily_report.py",
    "reconcile_selection_attribution.py",
    "pullback_shadow_daily.py",
    "reconcile_ta_late_entry_shadow.py",
    "reconcile_xs_reversal_shadow.py",
    "reconcile_relax_tier_shadow.py",
    "macro_regime_watch.py",
    "prepare_canary_promotion.py",
    # Web UI spawn / lazy import.
    "regen_param_sweep.py",
    "shadow_grade.py",
    # Shared scripts/-local helper imported by shadow_grade.py and
    # reconcile_change_arms_shadow.py (`import shadow_progress`).
    "shadow_progress.py",
    # Host cron_reconcile.sh -> docker exec (not covered by scheduler jobs).
    "reconcile_fills.py",
    "reconcile_change_arms_shadow.py",
    "reconcile_pullback_shadow.py",
    "reconcile_daily_extension_cap_shadow.py",
    # D2 (2026-10-23): ignite_grace_cf outcome join. Its default path is
    # /data/risk_tuning_shadow.jsonl (the file the container writes).
    "reconcile_risk_tuning_shadow.py",
    # Audit 2026-10-11 shadow A/B arms.
    "reconcile_stop_confirm_shadow.py",
    "reconcile_weight_shadow.py",
    # M0 (2026-10-29): effective trigger-config canonical<->live drift audit.
    "audit_effective_config.py",
    # M1 (2026-10-29): universe-wide decision-shadow forward-return reconcile
    # (cron_reconcile.sh -> docker exec; read-only).
    "reconcile_decision_shadow.py",
    "replay_gate.py",
    "reconcile_binance_stream.py",
    "arm_edge_eval.py",
    "promotion_gate.py",
    "real_verdict_eval.py",
    "assemble_veto_evidence.py",
    # M-1 OFI evaluator (research tool, not in the trading runtime path);
    # runs one-shot via `docker exec` against the /data volume.
    "m1_ofi_eval.py",
    # Deploy smoke path.
    "postdeploy_smoke.py",
    # Deploy step 3d: in-container post-deploy shadow health check.
    "postdeploy_shadow_checks.py",
)
