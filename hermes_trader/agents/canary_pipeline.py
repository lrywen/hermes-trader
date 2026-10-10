"""R4: semi-automated canary promotion pipeline.

The shadow -> live bridge was previously a hard, fully-manual wall: an
operator had to mint a B-13 acceptance record by hand with no system help, so
strategies sat in SHADOW indefinitely. This module adds the *prepare* half of
a semi-automated pipeline:

1. gather the multi-dimensional evidence for an arm (statistical robustness,
   economic thickness after conservative costs, realistic-fill result,
   orderability, and a tested kill-switch);
2. when every pre-registered gate clears, produce a **pending** promotion
   record carrying the full evidence snapshot;
3. an operator must still explicitly approve it. Nothing here ever writes
   ``mode=LIVE`` and nothing mints the executable gate — the human keeps the
   final one-vote veto (plan §5.3 #2).

The pending records live in a small JSONL file (one row per run, deduped per
arm), deliberately separate from the single ``live_acceptance_gate.json`` that
actually authorizes LIVE. Pure evaluation helpers are side-effect free and
unit-testable.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

# Record format version for the pending-promotion row.
RECORD_VERSION = 1
PENDING = "PENDING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"

# Status values the pipeline ever emits. It never emits an executable verdict.
_STATES = (PENDING, APPROVED, REJECTED)


def default_pipeline_path() -> str:
    """Resolve the pending-promotion log path (env override for tests)."""
    return os.environ.get(
        "HERMES_CANARY_PIPELINE_FILE",
        os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"),
                     "canary_promotion_pending.jsonl"),
    )


# Multi-dimensional promotion gates (plan §4.2 / §4.3). These are the SAME
# conservative dimensions a human is asked to check, encoded so the system can
# prepare (not force) the promotion. Overridable per call.
DEFAULT_GATES: dict[str, Any] = {
    "min_mature_outcomes": 60,       # independent mature counterfactuals
    "min_independent_days": 20,      # distinct UTC days with evidence
    "min_net_edge_bps": 6.0,         # conservative-cost net edge (thickness)
    "min_bb_ci_lo_bps": 0.0,         # block-bootstrap 95% CI lower bound
    "min_dsr_p": 0.95,               # deflated Sharpe (multi-test corrected)
    "max_pbo": 0.5,                  # probability of backtest overfitting
    "max_wfr": 0.5,                  # R5: WFA OOS efficiency must EXCEED this
    "require_realistic_fill_positive": True,  # net positive after slippage
    "require_killswitch_tested": True,
    "require_orderable": True,
}


def evaluate_gates(evidence: dict[str, Any],
                   gates: Optional[dict[str, Any]] = None
                   ) -> dict[str, Any]:
    """Pure gate evaluation. Returns ``{passed, checks}`` with one boolean per
    dimension plus the human-readable reason for every failed check.

    ``evidence`` is expected to carry (missing keys fail their check rather
    than raising — an evidence gap must never be auto-promoted):

      mature_outcomes, independent_days, net_edge_bps, bb_ci_lo_bps, dsr_p,
      pbo, wfr, realistic_fill_positive, killswitch_tested, orderable
    """
    g = dict(DEFAULT_GATES)
    if gates:
        g.update(gates)

    def num(key: str) -> Optional[float]:
        v = evidence.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"gate": name, "passed": bool(ok), "detail": detail})

    v = num("mature_outcomes")
    add("sample_size", v is not None and v >= g["min_mature_outcomes"],
        f"mature_outcomes={v} >= {g['min_mature_outcomes']}")

    v = num("independent_days")
    add("independence", v is not None and v >= g["min_independent_days"],
        f"independent_days={v} >= {g['min_independent_days']}")

    v = num("net_edge_bps")
    add("economic_thickness", v is not None and v >= g["min_net_edge_bps"],
        f"net_edge_bps={v} >= {g['min_net_edge_bps']}")

    v = num("bb_ci_lo_bps")
    add("bootstrap_ci", v is not None and v > g["min_bb_ci_lo_bps"],
        f"bb_ci_lo_bps={v} > {g['min_bb_ci_lo_bps']}")

    v = num("dsr_p")
    add("deflated_sharpe", v is not None and v >= g["min_dsr_p"],
        f"dsr_p={v} >= {g['min_dsr_p']}")

    v = num("pbo")
    add("pbo", v is not None and v < g["max_pbo"],
        f"pbo={v} < {g['max_pbo']}")

    v = num("wfr")
    # R5 walk-forward efficiency: must strictly EXCEED the threshold.
    add("walk_forward", v is not None and v > g["max_wfr"],
        f"wfr={v} > {g['max_wfr']}")

    if g["require_realistic_fill_positive"]:
        ok = bool(evidence.get("realistic_fill_positive"))
        add("realistic_fill", ok,
            f"net positive after slippage={ok}")

    if g["require_killswitch_tested"]:
        ok = bool(evidence.get("killswitch_tested"))
        add("killswitch_tested", ok,
            f"kill-switch live-tested={ok}")

    if g["require_orderable"]:
        ok = bool(evidence.get("orderable"))
        add("orderable", ok,
            f"account meets min-order/target size={ok}")

    passed = all(c["passed"] for c in checks)
    return {"passed": passed, "checks": checks}


def build_pending_record(
    arm: str, evidence: dict[str, Any], *,
    gates: Optional[dict[str, Any]] = None,
    config_sha256: Optional[str] = None,
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Construct a PENDING promotion record for an arm.

    Only built when every gate clears; otherwise raises ValueError (an
    unqualified arm must never be queued for approval). The record is inert —
    it carries evidence and asks a human to approve; it cannot be traded on.
    """
    res = evaluate_gates(evidence, gates)
    if not res["passed"]:
        failed = [c["gate"] for c in res["checks"] if not c["passed"]]
        raise ValueError(
            f"refusing to queue arm={arm}: gates not met: {failed}")
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    return {
        "version": RECORD_VERSION,
        "state": PENDING,
        "arm": arm,
        "evidence": evidence,
        "checks": res["checks"],
        "config_sha256": config_sha256,
        "created_ms": now_ms,
        "decided_ms": None,
        "operator": None,
    }


def read_records(path: Optional[str] = None) -> list[dict[str, Any]]:
    """Read all pipeline rows (empty list when the file is absent)."""
    path = path or default_pipeline_path()
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return out


def _arm_already_pending(records: list[dict[str, Any]], arm: str,
                         config_sha256: Optional[str]) -> bool:
    """True when an open PENDING record already exists for this arm/config,
    so daily re-runs don't queue duplicate approval requests."""
    for r in records:
        if r.get("arm") != arm or r.get("state") != PENDING:
            continue
        if config_sha256 is None or r.get("config_sha256") == config_sha256:
            return True
    return False


def append_record(record: dict[str, Any], *, path: Optional[str] = None,
                  ) -> bool:
    """Append a prepared record unless an equivalent PENDING one exists.

    Returns True if appended, False if deduplicated. Atomic append (open in
    append mode under the process lock is sufficient for JSONL; callers
    serialize runs through the scheduler).
    """
    path = path or default_pipeline_path()
    existing = read_records(path)
    if _arm_already_pending(existing, record.get("arm"),
                            record.get("config_sha256")):
        return False
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False,
                            separators=(",", ":")) + "\n")
    return True


def mark_decision(arm: str, state: str, *, operator: str,
                  path: Optional[str] = None,
                  now_ms: Optional[int] = None) -> bool:
    """Operator decision on the open PENDING record for an arm.

    Records the human verdict (APPROVED / REJECTED) but this function itself
    never writes config or mints the executable gate — approval simply flips
    the request state so a separate, explicitly-confirmed mint step can run.
    Returns True if a pending record was updated.
    """
    if state not in (APPROVED, REJECTED):
        raise ValueError(f"decision must be {APPROVED}/{REJECTED}, got {state}")
    path = path or default_pipeline_path()
    records = read_records(path)
    changed = False
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    for r in records:
        if r.get("arm") == arm and r.get("state") == PENDING:
            r["state"] = state
            r["operator"] = operator
            r["decided_ms"] = now_ms
            changed = True
    if not changed:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False,
                                separators=(",", ":")) + "\n")
    os.replace(tmp, path)
    return True
