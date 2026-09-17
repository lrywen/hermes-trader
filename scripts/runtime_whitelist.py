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
      daily_report.py, pullback_shadow_daily.py,
      reconcile_ta_late_entry_shadow.py, reconcile_xs_reversal_shadow.py,
      reconcile_relax_tier_shadow.py, reconcile_short_only_shadow.py,
      macro_regime_watch.py
  * Web UI spawn / lazy import (dashboard_routes/shadow_arms.py):
      regen_param_sweep.py, shadow_grade.py
  * Shared scripts/-local helper imported by two of the above:
      shadow_progress.py  (shadow_grade.py + reconcile_change_arms_shadow.py
      both `import shadow_progress`; it has no further scripts/-local deps)
  * Host cron wrappers that `docker exec` INTO the container:
      cron_reconcile.sh -> reconcile_fills.py, reconcile_change_arms_shadow.py,
        reconcile_pullback_shadow.py, reconcile_daily_extension_cap_shadow.py,
        reconcile_early_breakout_shadow.py
        (xs_reversal / relax_tier already listed via scheduler)
      cron_shadow_grade.sh -> shadow_grade.py (already listed)
  * Deploy smoke (scripts/deploy_prod.sh `docker exec ... postdeploy_smoke.py`):
      postdeploy_smoke.py

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
    "daily_report.py",
    "pullback_shadow_daily.py",
    "reconcile_ta_late_entry_shadow.py",
    "reconcile_xs_reversal_shadow.py",
    "reconcile_relax_tier_shadow.py",
    "reconcile_short_only_shadow.py",
    "macro_regime_watch.py",
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
    "reconcile_early_breakout_shadow.py",
    # Deploy smoke path.
    "postdeploy_smoke.py",
)
