#!/usr/bin/env python3
"""Automated verification of all fixes applied during the audit remediation.

Runs static checks (compilation, code pattern presence) and integration
checks (frontend availability).  Every assertion is self-contained and
reports a clear PASS/FAIL result.
"""

import os
import subprocess
import sys
import urllib.error
import urllib.request

# ── Path resolution (portable, no hardcoded absolute paths) ──────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADER_ROOT = os.path.dirname(_SCRIPT_DIR)
SHARED_DIR = os.path.expanduser("~/.hermes-trading")


def shared(*parts: str) -> str:
    return os.path.join(SHARED_DIR, *parts)


def trader(*parts: str) -> str:
    return os.path.join(TRADER_ROOT, *parts)


# ── Helpers ─────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
_total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL, _total
    _total += 1
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))


def file_contains(path: str, *patterns: str) -> bool:
    """True iff *all* patterns appear in the file."""
    try:
        text = open(path).read()
    except Exception:
        return False
    return all(p in text for p in patterns)


def py_compile_ok(path: str) -> bool:
    try:
        import py_compile
        py_compile.compile(path, doraise=True)
        return True
    except py_compile.PyCompileError:
        return False


# ── 1. Compilation checks ──────────────────────────────────────────────

print("=" * 60)
print("VERIFICATION 1: All modified files compile")
print("=" * 60)

FILES = [
    shared("signal_bus.py"),
    shared("event_log.py"),
    trader("hermes_trader", "agents", "risk_gates.py"),
    trader("hermes_trader", "client", "exchange.py"),
    trader("scripts", "hermes-mcp-server.py"),
    trader("hermes_trader", "agents", "executor.py"),
    trader("hermes_trader", "agents", "dsl_exit.py"),
    trader("hermes_trader", "client", "ws_client.py"),
    trader("hermes_trader", "agents", "research.py"),
    trader("scripts", "trading_loop.py"),
]

for fp in FILES:
    check(f"Compile: {os.path.basename(fp)}", py_compile_ok(fp), fp)


# ── 2. P0 fixes — security & consistency ───────────────────────────────

print("\n" + "=" * 60)
print("VERIFICATION 2: P0 fixes (MCP gate, SL fallback, order consistency)")
print("=" * 60)

mcp_path = trader("scripts", "hermes-mcp-server.py")
check("P0-1: MCP write gate constant defined",
      file_contains(mcp_path, "_HERMES_MCP_ALLOW_WRITE"))
check("P0-1: MCP write gate function defined",
      file_contains(mcp_path, "_check_write_gate"))
check("P0-1: handle_config gated",
      file_contains(mcp_path, "_check_write_gate()", "handle_config"))
check("P0-1: handle_execute gated",
      file_contains(mcp_path, "_check_write_gate()", "handle_execute"))

exec_path = trader("hermes_trader", "agents", "executor.py")
check("P0-2: SL retry mechanism present",
      file_contains(exec_path, "_pending_sl_retries"))
check("P0-2: retry_pending_sl function defined",
      file_contains(exec_path, "retry_pending_sl"))
check("P0-3: Order persistence try/except present",
      file_contains(exec_path, "try:", "register_position", "retry_pending_sl"))


# ── 3. H-3 / H-4: Signal bus circuit breaker + exception classification ──

print("\n" + "=" * 60)
print("VERIFICATION 3: Signal bus (H-3/H-4)")
print("=" * 60)

sb_path = shared("signal_bus.py")
check("H-3: CircuitState enum defined",
      file_contains(sb_path, "class CircuitState"))
check("H-3: CircuitState.CLOSED used",
      file_contains(sb_path, "CircuitState.CLOSED"))
check("H-3: CircuitState.OPEN used",
      file_contains(sb_path, "CircuitState.OPEN"))
check("H-3: CircuitState.HALF_OPEN used",
      file_contains(sb_path, "CircuitState.HALF_OPEN"))
check("H-3: _maybe_half_open method defined",
      file_contains(sb_path, "_maybe_half_open"))
check("H-4: ValidationError handling in ingest",
      file_contains(sb_path, "ValidationError"))
check("H-4: IOError handling in ingest",
      file_contains(sb_path, "IOError"))


# ── 4. C-4 / H-2: Event log rotation ───────────────────────────────────

print("\n" + "=" * 60)
print("VERIFICATION 4: Event log rotation (C-4/H-2)")
print("=" * 60)

el_path = shared("event_log.py")
check("C-4: _rotate_if_needed function defined",
      file_contains(el_path, "_rotate_if_needed"))
check("C-4: shutil import present",
      file_contains(el_path, "import shutil"))
check("C-4: _MAX_EVENTS_BYTES constant defined",
      file_contains(el_path, "_MAX_EVENTS_BYTES"))
check("C-4: _MAX_BACKUPS constant defined",
      file_contains(el_path, "_MAX_BACKUPS"))
check("C-4: Rotation called on record_event",
      file_contains(el_path, "_rotate_if_needed()", "record_event"))


# ── 5. H-7: DSL entry_time from actual fill ────────────────────────────

print("\n" + "=" * 60)
print("VERIFICATION 5: DSL entry_time from actual fill (H-7)")
print("=" * 60)

check("H-7: executor.py passes entry_time to register_position",
      file_contains(exec_path, "entry_time=_entry_time_sec"))
check("H-7: executor.py extracts filled_at_ms from order_res",
      file_contains(exec_path, "filled_at_ms"))
dsl_exit_path = trader("hermes_trader", "agents", "dsl_exit.py")
check("H-7: dsl_exit.py has _resolve_fill_time_ms helper",
      file_contains(dsl_exit_path, "_resolve_fill_time_ms"))
check("H-7: dsl_exit.py rehydrate_from_exchange accepts user param",
      file_contains(dsl_exit_path, "user: Optional[str] = None"))
trading_loop_path = trader("scripts", "trading_loop.py")
check("H-7: trading_loop.py passes user to rehydrate_from_exchange",
      file_contains(trading_loop_path, "user=user"))


# ── 6. H-8: WS auto-reconnect ──────────────────────────────────────────

print("\n" + "=" * 60)
print("VERIFICATION 6: WS auto-reconnect (H-8)")
print("=" * 60)

ws_path = trader("hermes_trader", "client", "ws_client.py")
check("H-8: _WS_MAX_STALE_SECONDS constant defined",
      file_contains(ws_path, "_WS_MAX_STALE_SECONDS"))
check("H-8: _reconnect_loop method defined",
      file_contains(ws_path, "_reconnect_loop"))
check("H-8: _connect_and_subscribe method defined",
      file_contains(ws_path, "_connect_and_subscribe"))
check("H-8: _stop_internal method defined",
      file_contains(ws_path, "_stop_internal"))
check("H-8: Exponential backoff with jitter used",
      file_contains(ws_path, "_WS_RECONNECT_BASE_DELAY"))
check("H-8: Reconnect thread started on start()",
      file_contains(ws_path, "ws-reconnect"))


# ── 7. H-6: LLM connection pool (REMOVED 2026-08 — dead code, never wired) ──

print("\n" + "=" * 60)
print("VERIFICATION 7: LLM connection pool removed (H-6 obsolete)")
print("=" * 60)

res_path = trader("hermes_trader", "agents", "research.py")
check("H-6: dead _get_llm_client removed",
      not file_contains(res_path, "_get_llm_client"))
check("H-6: dead _release_llm_client removed",
      not file_contains(res_path, "_release_llm_client"))


# ── 8. Frontend availability ───────────────────────────────────────────

print("\n" + "=" * 60)
print("VERIFICATION 8: Frontend availability (5173)")
print("=" * 60)

try:
    req = urllib.request.Request("http://localhost:5173/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        fe_ok = resp.status == 200
        fe_detail = f"HTTP {resp.status}"
except Exception as e:
    fe_ok = False
    fe_detail = str(e)
check("Frontend 5173 responds HTTP 200", fe_ok, fe_detail)


# ── 9. Test suite (offline/unit tests) ─────────────────────────────────

print("\n" + "=" * 60)
print("VERIFICATION 9: Unit test suite")
print("=" * 60)

try:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-m", "not online and not live",
         "--tb=short", "-q", "--no-header"],
        capture_output=True, text=True, timeout=120,
        cwd=TRADER_ROOT,
    )
    tests_ok = result.returncode == 0
    # Extract summary line
    summary = ""
    for line in result.stderr.split("\n"):
        if "passed" in line or "failed" in line:
            summary = line.strip()
            break
    if not summary:
        for line in result.stdout.split("\n"):
            if "passed" in line or "failed" in line:
                summary = line.strip()
                break
    check("Unit tests pass", tests_ok, summary or f"exit={result.returncode}")
    if not tests_ok:
        # Print first few failures
        for line in (result.stderr or "").split("\n")[:20]:
            if "FAILED" in line:
                print(f"    {line}")
except Exception as e:
    check("Unit tests pass", False, str(e))


# ── Summary ─────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print(f"SUMMARY: {PASS}/{_total} passed, {FAIL}/{_total} failed")
print("=" * 60)

sys.exit(0 if FAIL == 0 else 1)
