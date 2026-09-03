#!/usr/bin/env python3
"""P3-14: egress IP drift watchdog (stdlib only — never imports trading code).

Residential-broadband egress IPs can change when the ISP rotates the lease or
the tunnel re-establishes. A silent change breaks exchange API key whitelists
and outbound rate-limit budgets; webhook callbacks may also start arriving
from an unexpected peer. This watchdog is a pure *observability* sidecar:

  * Every ``HERMES_IP_DRIFT_CHECK_S`` seconds (default 300) it fetches the
    current egress IP from a primary source (api.ipify.org) and, on failure,
    falls back to a secondary source (ifconfig.me).
  * The last-seen IP is persisted to ``HERMES_IP_DRIFT_STATE_FILE``
    (default ``/data/ip_drift_state.json``).
  * On a confirmed change it logs ``[ip-drift] egress IP changed: old=… new=…``
    at WARNING level and appends one ``ip_drift`` record to the authoritative
    events.jsonl (path ``HERMES_EVENTS_FILE`` / ``HERMES_IP_DRIFT_EVENTS_FILE``,
    default ``/data/events.jsonl``). The record is written through the SAME
    sha-256 hash-chain format as ``hermes_trader.event_log`` so it verifies
    with ``event_log.verify_chain()`` — the duplication is intentional: this
    process must not import any trading module.
  * Query failures are tolerated silently; only after
    ``HERMES_IP_DRIFT_FAIL_WARN_AFTER`` consecutive failures (default 3) is a
    single WARNING logged, so transient network blips never spam the log.

Master switch: ``HERMES_IP_DRIFT_WATCH=1`` enables the daemon. It defaults to
OFF (``0``); deployments opt in once the sidecar is wired up.

Hard rule: this script NEVER blocks or alters trading. An IP change is a
signal for humans — automatic trading cutover is a risk decision that stays
out of scope (see docs/advanced-optimization-roadmap.md §6).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl  # type: ignore  # POSIX only
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows/dev
    fcntl = None  # type: ignore
    _HAVE_FCNTL = False

logger = logging.getLogger("ip_drift_watch")

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})

# Primary returns JSON {"ip": "..."}; secondary returns the raw IP as text.
PRIMARY_URL = "https://api.ipify.org?format=json"
SECONDARY_URL = "https://ifconfig.me/ip"
REQUEST_TIMEOUT_S = 10.0

# Loose shape check — accepts dotted v4 and colon v6 without pulling in ipaddress
# parsing semantics differences; malformed strings from a captive portal are
# treated as a failed query rather than a "new IP".
_IP_RE = re.compile(r"^[0-9a-fA-F\.:]+$")

_GENESIS_HASH = ""


def _env(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE_TOKENS


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def watch_enabled() -> bool:
    """Master switch; OFF by default so nothing runs until a deployment opts in."""
    return _env_bool("HERMES_IP_DRIFT_WATCH", False)


def check_interval_s() -> int:
    return _env_int("HERMES_IP_DRIFT_CHECK_S", 300)


def fail_warn_after() -> int:
    return _env_int("HERMES_IP_DRIFT_FAIL_WARN_AFTER", 3)


def state_path() -> Path:
    return Path(_env("HERMES_IP_DRIFT_STATE_FILE", "/data/ip_drift_state.json"))


def events_path() -> Path:
    # Honour HERMES_EVENTS_FILE first so the sidecar always tracks the same
    # events.jsonl the trading loop writes; the dedicated override exists for
    # tests / isolated canary runs.
    return Path(
        _env(
            "HERMES_IP_DRIFT_EVENTS_FILE",
            os.environ.get("HERMES_EVENTS_FILE", "") or "/data/events.jsonl",
        )
    )


def _http_get(url: str, timeout: float = REQUEST_TIMEOUT_S) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-ip-drift/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace").strip()


def _valid_ip(raw: str) -> bool:
    return bool(raw) and len(raw) <= 45 and bool(_IP_RE.match(raw))


def fetch_ip() -> Optional[str]:
    """Return the current egress IP, or None if BOTH sources failed.

    A malformed/non-IP body (e.g. a captive-portal login page) counts as a
    failure of that source and triggers the fallback.
    """
    try:
        body = _http_get(PRIMARY_URL)
        try:
            ip = str(json.loads(body).get("ip", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            ip = ""
        if _valid_ip(ip):
            return ip
    except Exception as e:
        logger.debug("[ip-drift] primary source failed: %s", e)
    try:
        ip = _http_get(SECONDARY_URL).strip()
        if _valid_ip(ip):
            return ip
    except Exception as e:
        logger.debug("[ip-drift] secondary source failed: %s", e)
    return None


def load_state(path: Optional[Path] = None) -> dict[str, Any]:
    p = path or state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(state: dict[str, Any], path: Optional[Path] = None) -> bool:
    p = path or state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError as e:
        logger.warning("[ip-drift] state persist failed: %s", e)
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_body(rec: dict[str, Any]) -> bytes:
    """Mirrors hermes_trader.event_log._canonical_body (kept in sync by tests)."""
    return json.dumps(
        {
            "event": rec.get("event"),
            "trace_id": rec.get("trace_id"),
            "timestamp": rec.get("timestamp"),
            "payload": rec.get("payload"),
            "seq": rec.get("seq"),
            "prev_hash": rec.get("prev_hash"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_hash(rec: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_body(rec)).hexdigest()


def _tail_chain(path: Path) -> tuple[int, str]:
    """Return (seq, hash) of the last chained record in the events file.

    Matches event_log._chain_tail's fallback semantics: a missing/empty file
    or one containing only unchained records starts a fresh genesis run.
    """
    tail: Optional[dict[str, Any]] = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("hash") and rec.get("seq") is not None:
                    tail = rec
    except OSError:
        return 0, _GENESIS_HASH
    if tail is not None:
        return int(tail.get("seq", 0)), str(tail.get("hash", ""))
    return 0, _GENESIS_HASH


@contextlib.contextmanager
def _events_lock(path: Path):
    """flock on ``<events>.lock`` so the watchdog and the trading loop never
    interleave an append with event_log's rotation. Best-effort: if the sidecar
    cannot be opened (read-only volume) the append proceeds lockless."""
    fd = None
    if _HAVE_FCNTL:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = open(f"{path}.lock", "a+")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        except OSError:
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                fd.close()


def append_ip_drift_event(old_ip: str, new_ip: str, path: Optional[Path] = None) -> bool:
    """Append one chained ``ip_drift`` record to events.jsonl. Best-effort;
    never raises — observability must not take down the watchdog itself."""
    p = path or events_path()
    rec = {
        "event": "ip_drift",
        "trace_id": "",
        "timestamp": _now_iso(),
        "payload": {"old_ip": old_ip, "new_ip": new_ip},
    }
    try:
        with _events_lock(p):
            seq, prev_hash = _tail_chain(p)
            rec["seq"] = seq + 1
            rec["prev_hash"] = prev_hash
            rec["hash"] = _record_hash(rec)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
        return True
    except OSError as e:
        logger.warning("[ip-drift] events.jsonl append failed: %s", e)
        return False


def check_once(*, state_file: Optional[Path] = None, events_file: Optional[Path] = None) -> Optional[bool]:
    """Run one drift check. Returns:

    * True  — IP changed (warning logged + event appended + state updated),
    * False — IP unchanged (or first observed; state seeded),
    * None  — both sources failed (state untouched).

    Exposed as a function (not just a loop body) so tests and manual ops can
    drive single checks.
    """
    sp = state_file or state_path()
    ep = events_file or events_path()
    state = load_state(sp)
    old_ip = str(state.get("ip") or "")

    ip = fetch_ip()
    if ip is None:
        return None

    if old_ip and old_ip != ip:
        logger.warning("[ip-drift] egress IP changed: old=%s new=%s", old_ip, ip)
        append_ip_drift_event(old_ip, ip, path=ep)

    state.update(
        {
            "ip": ip,
            "updated_at": _now_iso(),
            "source": "scripts/ip_drift_watch.py",
        }
    )
    save_state(state, path=sp)
    return bool(old_ip and old_ip != ip)


def run_forever() -> None:
    """Daemon loop. Exits immediately (with a notice) when the switch is off."""
    if not watch_enabled():
        logger.info(
            "[ip-drift] disabled (HERMES_IP_DRIFT_WATCH!=1); set it to 1 to enable"
        )
        return
    interval = check_interval_s()
    warn_after = fail_warn_after()
    logger.info(
        "[ip-drift] watchdog enabled: check every %ss, warn after %s consecutive failures",
        interval,
        warn_after,
    )
    consecutive_failures = 0
    while True:
        try:
            changed = check_once()
        except Exception as e:
            logger.debug("[ip-drift] check raised: %s", e)
            changed = None
        if changed is None:
            consecutive_failures += 1
            if consecutive_failures == warn_after:
                logger.warning(
                    "[ip-drift] %s consecutive egress-IP query failures; "
                    "network down or both IP sources unreachable",
                    consecutive_failures,
                )
        else:
            consecutive_failures = 0
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HERMES_IP_DRIFT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
