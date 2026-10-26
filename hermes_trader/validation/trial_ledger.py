"""Stage 0 — append-only experiment ledger (research validation framework).

Full-review plan (2026-10-25, P0): every hypothesis test must be registered
with its frozen caliber and the headline diagnostics in ONE place, so that
selection inflation (trying 118 candidates and reporting only the best) is
visible rather than reconstructed from memory. Each trial is one JSON line:

    hypothesis / caliber / n / net bps / leave-one-coin worst / day-block CI /
    top concentration / multiplicity (m, p_raw, p_bonferroni) / verdict.

The ledger is append-only on disk (records are never rewritten; corrections
are appended as a new record with ``supersedes``). The family size ``m`` for
Bonferroni is the running count of trials sharing the same ``family`` key,
captured at append time so the correction cannot be retroactively changed.

Pure standard library. No trading side effects; safe to use from offline
research and tests (point ``path`` at a tmp file).
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

LEDGER_VERSION = 1


def bonferroni(p_raw: float, family_size: int) -> float:
    """Bonferroni-corrected p: min(1, m * p). NaN p stays NaN."""
    if not math.isfinite(p_raw) or family_size < 1:
        return float("nan")
    return min(1.0, float(family_size) * p_raw)


@dataclass
class Trial:
    hypothesis: str
    caliber: str                      # frozen signal/horizon/cost definition
    n: int
    net_bps: float                    # mean net bps per entry (after cost)
    leave_one_coin_worst_bps: float = float("nan")
    ci_low_bps: float = float("nan")
    ci_high_bps: float = float("nan")
    top1_concentration: float = float("nan")
    p_raw: float = float("nan")
    family: str = "default"
    family_size_at_test: int = 1
    verdict: str = "inconclusive"    # reject_h0 / fail_reject / invalid
    day_split: str = ""              # e.g. "2026-09-28..2026-10-10|..2026-10-25"
    # Compact, ledger-safe gate detail so a cron run that keeps only the ledger
    # can still answer "which bucket failed and at what threshold". MUST stay
    # small (no per-day/per-entry maps): e.g. {"vol_group": {"ic": {...},
    # "thresholds": {...}, "consistent": bool}, "fixed_effect": {...}}.
    gates_summary: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    supersedes: Optional[str] = None
    ts_ms: int = 0
    trial_id: str = ""

    def p_bonferroni(self) -> float:
        return bonferroni(self.p_raw, self.family_size_at_test)


class TrialLedger:
    """Append-only JSONL ledger for one research family/thread."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _next_id(self, count: int) -> str:
        return f"T{count + 1:04d}"

    def load(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def family_size(self, family: str) -> int:
        """Running count of prior trials in the family (the multiplicity m)."""
        return sum(1 for r in self.load() if r.get("family") == family)

    def append(self, trial: Trial) -> dict[str, Any]:
        """Validate, stamp id/timestamp and corrected p, append one line."""
        records = self.load()
        family = trial.family or "default"
        m = trial.family_size_at_test if trial.family_size_at_test > 1 \
            else self.family_size(family) + 1
        trial.family = family
        trial.family_size_at_test = m
        if not trial.ts_ms:
            trial.ts_ms = int(time.time() * 1000)
        trial.trial_id = trial.trial_id or self._next_id(len(records))

        rec = asdict(trial)
        rec["p_bonferroni"] = trial.p_bonferroni()
        rec["ledger_version"] = LEDGER_VERSION
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return rec
