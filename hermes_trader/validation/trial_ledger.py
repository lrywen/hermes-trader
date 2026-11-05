"""Append-only trial ledger and fail-closed DSR multiplicity evidence.

The original Stage-0 interface (``TrialLedger(path).append/load/family_size``)
is retained for operational bookkeeping.  DSR callers use the strict loader,
which requires an explicit ``arm``, ``paradigm``, ``iteration``, ``test_at`` and
``preregistration`` for every record.  Historical imports count towards the
Bonferroni family size but are not prospective evidence for promotion.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

LEDGER_VERSION = 2
LEDGER_PATH_ENV = "HERMES_TRIAL_LEDGER_PATH"
DEFAULT_TRIAL_LEDGER_PATH = Path(__file__).with_name("trials_ledger.jsonl")
_ACTIVE = "active"
_RETIRED_STATUSES = frozenset(("retracted", "superseded"))
_PREREGISTERED = frozenset(("preregistered", "pre_registered", "prospective"))
_LEGACY = "legacy_import"


def bonferroni(p_raw: float, family_size: int) -> float:
    """Bonferroni-corrected p: min(1, m * p). NaN p stays NaN."""
    if not math.isfinite(p_raw) or family_size < 1:
        return float("nan")
    return min(1.0, float(family_size) * p_raw)


@dataclass
class Trial:
    hypothesis: str
    caliber: str
    n: int
    net_bps: float
    leave_one_coin_worst_bps: float = float("nan")
    ci_low_bps: float = float("nan")
    ci_high_bps: float = float("nan")
    top1_concentration: float = float("nan")
    p_raw: float = float("nan")
    family: str = "default"
    family_size_at_test: int = 1
    verdict: str = "inconclusive"
    day_split: str = ""
    gates_summary: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    supersedes: Optional[str] = None
    ts_ms: int = 0
    trial_id: str = ""
    # DSR schema fields are optional here to keep Stage-0 callers compatible;
    # the strict DSR loader requires them in every on-disk record it consumes.
    arm: str = ""
    paradigm: str = ""
    iteration: str = ""
    test_at: str = ""
    recorded_at: str = ""
    preregistration: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    status: str = "active"
    provenance: Any = None
    retracted: bool = False
    superseded_by: str = ""

    def __post_init__(self) -> None:
        if self.arm and self.paradigm and self.family in ("", "default"):
            self.family = family_key(self.arm, self.paradigm)

    def p_bonferroni(self) -> float:
        return bonferroni(self.p_raw, self.family_size_at_test)


class TrialLedgerError(ValueError):
    """Machine-readable ledger failure; DSR callers must not substitute a count."""

    def __init__(self, code: str, message: str, *, path: str = "",
                 details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "details": self.details,
        }


def family_key(arm: str, paradigm: str) -> str:
    """The multiplicity family is the cross product arm x paradigm."""
    arm, paradigm = str(arm or "").strip(), str(paradigm or "").strip()
    if not arm or not paradigm:
        raise TrialLedgerError(
            "family_unknown", "both arm and paradigm are required",
            details={"arm": arm, "paradigm": paradigm},
        )
    return f"{arm}::{paradigm}"


def resolve_ledger_path(path: Optional[os.PathLike[str] | str] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get(LEDGER_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_TRIAL_LEDGER_PATH


def _parse_time(value: Any, *, code: str, field: str,
                path: str = "") -> datetime:
    if value is None or value == "":
        raise TrialLedgerError(code, f"{field} is required", path=path,
                               details={"field": field})
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a timestamp")
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise ValueError("non-finite timestamp")
            seconds = float(value)
            if abs(seconds) > 100_000_000_000:  # epoch milliseconds
                seconds /= 1000.0
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        else:
            text = str(value).strip()
            if not text:
                raise ValueError("empty timestamp")
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("timezone is required")
            dt = dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise TrialLedgerError(
            code, f"invalid {field}: {exc}", path=path,
            details={"field": field, "value": str(value)},
        ) from exc
    return dt


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_family(record: dict[str, Any], *, strict: bool,
                   path: str = "") -> str:
    arm = str(record.get("arm") or "").strip()
    paradigm = str(record.get("paradigm") or "").strip()
    family = str(record.get("family") or "").strip()
    if arm and paradigm:
        expected = family_key(arm, paradigm)
        if family and family != expected:
            raise TrialLedgerError(
                "family_mismatch", "family must equal arm::paradigm", path=path,
                details={"trial_id": record.get("trial_id"), "family": family,
                         "expected_family": expected},
            )
        return expected
    if strict:
        raise TrialLedgerError(
            "family_unknown", "DSR records require arm and paradigm", path=path,
            details={"trial_id": record.get("trial_id"), "family": family},
        )
    return family


def _status(record: dict[str, Any], *, path: str = "") -> str:
    status = str(record.get("status") or _ACTIVE).strip().lower()
    if record.get("retracted") is True:
        status = "retracted"
    if record.get("superseded_by"):
        status = "superseded"
    if status not in (_ACTIVE, *_RETIRED_STATUSES):
        raise TrialLedgerError(
            "trial_status_unknown", f"unsupported trial status: {status}", path=path,
            details={"trial_id": record.get("trial_id"), "status": status},
        )
    return status


def _references(record: dict[str, Any], preregistration: dict[str, Any]) -> list[str]:
    values: list[Any] = [preregistration.get("reference"),
                         preregistration.get("references"),
                         record.get("provenance"), record.get("reference")]
    refs: list[str] = []
    def visit(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            refs.append(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key in ("reference", "references", "adr", "research", "git",
                        "commit", "url", "path"):
                if key in value:
                    visit(value[key])
    for value in values:
        visit(value)
    return refs


def _traceable_reference(refs: Iterable[str]) -> bool:
    for ref in refs:
        lowered = ref.lower()
        if (lowered.startswith(("docs/adr/", "docs/research/", "research/", "git:"))
                or "adr-" in lowered or "research/" in lowered):
            return True
        # A full commit id is an immutable git provenance reference.
        if len(ref) >= 40 and all(c in "0123456789abcdefABCDEF" for c in ref):
            return True
    return False


def _preregistration(record: dict[str, Any], *, test_at: datetime,
                     strict: bool, path: str = "") -> dict[str, Any]:
    raw = record.get("preregistration")
    if isinstance(raw, str):
        raw = {"status": raw}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TrialLedgerError(
            "preregistration_unknown", "preregistration must be an object",
            path=path, details={"trial_id": record.get("trial_id")},
        )
    source = str(record.get("source") or "").strip().lower()
    status = str(raw.get("status") or raw.get("kind") or source or "").strip().lower()
    if not status and not strict:
        return {"status": "unspecified", "prospective": False,
                "registered_at": None, "references": []}
    if not status:
        raise TrialLedgerError(
            "preregistration_unknown", "preregistration status is required",
            path=path, details={"trial_id": record.get("trial_id")},
        )
    refs = _references(record, raw)
    registered_raw = raw.get("registered_at", raw.get("registered_at_utc"))
    if status == _LEGACY or source == _LEGACY:
        if registered_raw not in (None, "") or raw.get("prospective") is True:
            raise TrialLedgerError(
                "legacy_preregistration_forged",
                "legacy_import cannot claim a prospective registration timestamp",
                path=path, details={"trial_id": record.get("trial_id")},
            )
        if strict and not _traceable_reference(refs):
            raise TrialLedgerError(
                "legacy_provenance_missing",
                "legacy_import requires ADR, git, or research provenance",
                path=path, details={"trial_id": record.get("trial_id")},
            )
        return {"status": _LEGACY, "prospective": False,
                "registered_at": None, "references": refs}
    if status not in _PREREGISTERED:
        raise TrialLedgerError(
            "preregistration_unknown", f"unsupported preregistration status: {status}",
            path=path, details={"trial_id": record.get("trial_id"), "status": status},
        )
    registered = _parse_time(
        registered_raw, code="preregistration_time_unknown",
        field="preregistration.registered_at", path=path,
    )
    if registered > test_at:
        raise TrialLedgerError(
            "preregistration_after_test",
            "prospective registration must not post-date the trial",
            path=path, details={"trial_id": record.get("trial_id"),
                                "registered_at": _iso(registered),
                                "test_at": _iso(test_at)},
        )
    if strict and not _traceable_reference(refs):
        raise TrialLedgerError(
            "preregistration_reference_missing",
            "prospective registration requires ADR, git, or research provenance",
            path=path, details={"trial_id": record.get("trial_id")},
        )
    return {"status": "preregistered", "prospective": True,
            "registered_at": _iso(registered), "references": refs}


def _validate_records(records: list[dict[str, Any]], *, strict: bool,
                      path: str = "") -> None:
    seen_ids: dict[str, int] = {}
    seen_iterations: dict[tuple[str, str], int] = {}
    for index, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise TrialLedgerError("trial_invalid", "trial must be a JSON object",
                                   path=path, details={"index": index})
        trial_id = str(rec.get("trial_id") or "").strip()
        if strict and not trial_id:
            raise TrialLedgerError("trial_id_missing", "trial_id is required",
                                   path=path, details={"index": index})
        if trial_id:
            if trial_id in seen_ids:
                raise TrialLedgerError(
                    "duplicate_trial_id", f"duplicate trial_id: {trial_id}", path=path,
                    details={"trial_id": trial_id, "first_index": seen_ids[trial_id],
                             "duplicate_index": index},
                )
            seen_ids[trial_id] = index
        family = _record_family(rec, strict=strict, path=path)
        if strict and not str(rec.get("iteration") or "").strip():
            raise TrialLedgerError(
                "iteration_missing", "iteration is required", path=path,
                details={"trial_id": trial_id, "family": family},
            )
        status = _status(rec, path=path)
        if strict:
            test_at = _parse_time(rec.get("test_at"), code="trial_as_of_unknown",
                                  field="test_at", path=path)
            _preregistration(rec, test_at=test_at, strict=True, path=path)
            key = (family, str(rec.get("iteration")).strip())
            # A correction may retain the iteration only when it explicitly
            # supersedes the immediately preceding row (or that row is retired).
            if key in seen_iterations and status == _ACTIVE:
                previous = records[seen_iterations[key]]
                if (_status(previous, path=path) == _ACTIVE
                        and str(rec.get("supersedes") or "")
                        != str(previous.get("trial_id") or "")):
                    raise TrialLedgerError(
                        "duplicate_iteration",
                        "duplicate active arm/paradigm/iteration", path=path,
                        details={"family": family, "iteration": key[1],
                                 "trial_id": trial_id},
                    )
            seen_iterations[key] = index


class TrialLedger:
    """Append-only JSONL records; DSR reads use :meth:`load_strict`."""

    def __init__(self, path: str | os.PathLike[str],
                 records: Optional[list[dict[str, Any]]] = None):
        self.path = str(path)
        self._records = records

    def _next_id(self, count: int) -> str:
        return f"T{count + 1:04d}"

    def append(self, trial: Trial) -> dict[str, Any]:
        """Validate, stamp id/timestamp and corrected p, append one line."""
        records = self.load()
        family = trial.family or "default"
        m = (trial.family_size_at_test if trial.family_size_at_test > 1
             else self.family_size(family) + 1)
        trial.family = family
        trial.family_size_at_test = m
        if not trial.ts_ms:
            trial.ts_ms = int(time.time() * 1000)
        trial.trial_id = trial.trial_id or self._next_id(len(records))
        rec = asdict(trial)
        rec["p_bonferroni"] = trial.p_bonferroni()
        rec["ledger_version"] = LEDGER_VERSION
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return rec

    def load(self, *, required: bool = False,
             strict: bool = False) -> list[dict[str, Any]]:
        if self._records is not None:
            records = [dict(rec) for rec in self._records]
            if strict:
                _validate_records(records, strict=True, path=self.path)
            return records
        if not os.path.exists(self.path):
            if required or strict:
                raise TrialLedgerError(
                    "ledger_missing", "trial ledger does not exist", path=self.path,
                )
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TrialLedgerError(
                "ledger_unreadable", f"cannot read trial ledger: {exc}", path=self.path,
            ) from exc
        if strict:
            _validate_records(records, strict=True, path=self.path)
        return records

    @classmethod
    def load_strict(cls, path: Optional[str | os.PathLike[str]] = None) -> "TrialLedger":
        resolved = resolve_ledger_path(path)
        ledger = cls(str(resolved))
        ledger._records = ledger.load(required=True, strict=True)
        return ledger

    def family_size(self, family: str = "default") -> int:
        return sum(1 for r in self.load() if r.get("family", "default") == family)

    def _selected(self, *, family: str, as_of: Any,
                  require_as_of: bool = True) -> list[dict[str, Any]]:
        if as_of is None or as_of == "":
            if require_as_of:
                raise TrialLedgerError(
                    "as_of_required", "an explicit as_of is required for DSR",
                    path=self.path, details={"family": family},
                )
            cutoff = datetime.max.replace(tzinfo=timezone.utc)
        else:
            cutoff = _parse_time(as_of, code="as_of_invalid", field="as_of",
                                 path=self.path)
        records = self.load(required=True, strict=True)
        known_family = any(_record_family(rec, strict=True, path=self.path) == family
                           for rec in records)
        if not known_family:
            raise TrialLedgerError(
                "family_not_covered", "no ledger coverage for arm x paradigm family",
                path=self.path, details={"family": family, "as_of": _iso(cutoff)},
            )
        superseded_ids = {
            str(rec.get("supersedes")) for rec in records
            if (rec.get("supersedes")
                and _record_family(rec, strict=True, path=self.path) == family
                and _status(rec, path=self.path) == _ACTIVE
                and _parse_time(rec.get("test_at"), code="trial_as_of_unknown",
                                field="test_at", path=self.path) <= cutoff)
        }
        selected: list[dict[str, Any]] = []
        for rec in records:
            if _record_family(rec, strict=True, path=self.path) != family:
                continue
            test_at = _parse_time(rec.get("test_at"), code="trial_as_of_unknown",
                                  field="test_at", path=self.path)
            if test_at > cutoff:
                continue
            status = _status(rec, path=self.path)
            if status != _ACTIVE or str(rec.get("trial_id")) in superseded_ids:
                continue
            enriched = dict(rec)
            enriched["family"] = family
            enriched["_test_at"] = _iso(test_at)
            enriched["_preregistration"] = _preregistration(
                rec, test_at=test_at, strict=True, path=self.path,
            )
            selected.append(enriched)
        if not selected:
            raise TrialLedgerError(
                "family_empty_as_of", "family has no active trials at as_of",
                path=self.path, details={"family": family, "as_of": _iso(cutoff)},
            )
        return selected

    def family_size_at_test(self, family: str = "default", as_of: Any = None,
                            *, arm: str = "", paradigm: str = "") -> int:
        if arm or paradigm:
            family = family_key(arm, paradigm)
        return len(self._selected(family=family, as_of=as_of))

    def dsr_evidence(self, *, arm: str, paradigm: str,
                     as_of: Any) -> dict[str, Any]:
        family = family_key(arm, paradigm)
        cutoff = _parse_time(as_of, code="as_of_required", field="as_of",
                             path=self.path)
        selected = self._selected(family=family, as_of=cutoff)
        prospective = [r for r in selected if r["_preregistration"]["prospective"]]
        legacy = [r for r in selected if r["_preregistration"]["status"] == _LEGACY]
        ledger_bytes = json.dumps(self.load(required=True, strict=True),
                                  ensure_ascii=False, sort_keys=True).encode("utf-8")
        return {
            "schema": "dsr_trial_evidence.v1",
            "arm": str(arm), "paradigm": str(paradigm), "family": family,
            "as_of": _iso(cutoff), "n_trials": len(selected),
            "legacy_n_trials": len(legacy),
            "preregistered_n_trials": len(prospective),
            "promotion_eligible": bool(prospective),
            "promotion_block": None if prospective else "legacy_import_only",
            "trial_ids": [str(r.get("trial_id")) for r in selected],
            "legacy_trial_ids": [str(r.get("trial_id")) for r in legacy],
            "preregistered_trial_ids": [str(r.get("trial_id")) for r in prospective],
            "ledger_path": self.path,
            "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        }


def dsr_trial_evidence(*, arm: str, paradigm: str, as_of: Any,
                       ledger_path: Optional[str | os.PathLike[str]] = None,
                       ledger: Optional[TrialLedger] = None) -> dict[str, Any]:
    """Return strict family evidence or raise ``TrialLedgerError`` (never 16)."""
    if ledger is not None and ledger_path is not None:
        raise TrialLedgerError(
            "ledger_source_ambiguous", "pass either ledger or ledger_path, not both",
        )
    selected = ledger if ledger is not None else TrialLedger.load_strict(ledger_path)
    return selected.dsr_evidence(arm=arm, paradigm=paradigm, as_of=as_of)


def deflated_sharpe_from_ledger(*, observed_sharpe: float, n_obs: int,
                                arm: str, paradigm: str, as_of: Any,
                                ledger_path: Optional[str | os.PathLike[str]] = None,
                                ledger: Optional[TrialLedger] = None,
                                require_preregistered: bool = False) -> dict[str, Any]:
    """Compute DSR with ledger-owned multiplicity and attach its provenance."""
    evidence = dsr_trial_evidence(
        arm=arm, paradigm=paradigm, as_of=as_of,
        ledger_path=ledger_path, ledger=ledger,
    )
    if require_preregistered and not evidence["promotion_eligible"]:
        raise TrialLedgerError(
            "legacy_import_only",
            "legacy trials may inform DSR but cannot alone support promotion",
            path=evidence["ledger_path"], details={"family": evidence["family"],
                                                   "as_of": evidence["as_of"]},
        )
    from .significance import deflated_sharpe_prob
    evidence["dsr_p"] = deflated_sharpe_prob(
        observed_sharpe=observed_sharpe, n_obs=n_obs,
        n_trials=evidence["n_trials"],
    )
    evidence["n_obs"] = int(n_obs)
    return evidence
