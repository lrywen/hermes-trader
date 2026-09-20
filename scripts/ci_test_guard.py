#!/usr/bin/env python3
"""P1-6' CI test baseline guard.

Two cheap guards so later refactors cannot silently DROP existing
verification or quietly blow up the offline suite's runtime:

  * count (hard gate)    — collect (not run) the offline test suite and fail
                           if the number of selected tests drops below
                           MIN_OFFLINE_TESTS. Collection takes ~3s and needs
                           no network, so it catches a missing test file, a
                           broken collection, or an over-broad deselect
                           without re-running the suite.

  * walltime (warn-only) — read a pytest JUnit XML (produced with
                           ``--junitxml=...``) and WARN (exit 0) when the
                           whole offline suite's wall time exceeds
                           WARN_TOTAL_WALLTIME_S. It never fails CI: GitHub
                           hosted runners are shared and noisy (cold cache /
                           contention), so a hard wall-time gate would be a
                           source of false reds; the warning surfaces a real
                           regression to a human who can confirm locally.

Both baselines are pinned as constants here (single source of truth) and
carry the date + machine class they were sampled on. Bumping them is an
explicit, reviewed edit — the whole point is that they do not move by
accident.

This script is a CI/host tool only: it is intentionally NOT on the
scripts/runtime_whitelist.py and therefore is not copied into the runtime
image.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

# Baseline sampled 2026-09-18 on the dev host (Linux x86_64, uv/pytest 8):
# `pytest --collect-only -q` reports 4089 offline tests (14 online/live
# deselected by the default addopts). The guard allows the count to GROW
# freely but never to SHRINK below this floor.
#
# W0 M0 冻结门（2026-09-19）上抬至实测值 4380（P4 统一回测内核 + 数据层 +
# P5-1bd 工具链新增测试落库后）。上抬后 CI 不再容忍净删 287 个测试不报警。
#
# W2 收尾（2026-09-20）上抬至实测值 4564：B-1a-改 stop_model parity（155）、
# B-2 portfolio maxc（19）、B-4 逐币半价差（4）、B-5 成本口径（4）、guard 路径
# 守卫（2）等本批新增测试落库；全量离线回归 4564 passed / 0 failed（422.9s）。
#
# W3/W5 收尾（2026-09-20）上抬至实测值 4592：A-3 实盘 reason parity +4、
# B-12/B-13 LIVE 启动验收门 live_gate +24（test_live_gate_b12_b13）；
# 全量离线回归 4592 passed / 0 failed（376.8s）。
#
# C-7 收尾（2026-09-20）上抬至实测值 4597：1h 出场周期参数化守卫
# test_c7_exit_interval +5（PIT 去重/墙钟标定/5m 默认等价）；1h 评估裁定 NO-GO。
# 全量离线回归 4597 passed / 0 failed（402.4s）。
#
# W6 收尾（2026-09-21）上抬至实测值 4610：B-6 币池一致性脚本、B-7/B-10
# source-gated 启动守卫 test_b_startup_source_guards +6、D-7 外部平仓聚合回填
# test_d7_reconcile_aggregate +7（分批 TP ∑PnL/sz 加权 px），B-8/B-11 为日志/
# 弃用标记、B-9 notional 建模经查已在 executor/bt_ra_exch 实现；+13。
MIN_OFFLINE_TESTS = 4610

# Whole offline-suite wall-time warning ceiling, in seconds.
# Local reference: ~360s on the dev host (2026-09-18). GitHub hosted runners
# are slower and shared, so the warn threshold is deliberately generous at
# ~2.5x. Warn-only; never fails the build (see module docstring).
WARN_TOTAL_WALLTIME_S = 900.0

_COLLECT_SUMMARY = re.compile(r"(\d+)\s+tests?\s+collected", re.IGNORECASE)
_COLLECT_DESELECT = re.compile(r"(\d+)\s+deselected", re.IGNORECASE)


def _run_collect() -> tuple[int, str]:
    """Return (selected offline test count, raw summary tail)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True, text=True, check=False,
    )
    tail = (proc.stdout or "") + (proc.stderr or "")
    collected = selected = None
    m_all = _COLLECT_SUMMARY.search(tail)
    if m_all:
        collected = int(m_all.group(1))
    m_des = _COLLECT_DESELECT.search(tail)
    deselected = int(m_des.group(1)) if m_des else 0
    # pytest prints "N tests collected (M deselected)": N is already the
    # post-deselect selected count. Older/newer phrasing may say "N/M tests
    # collected (M deselected)". Prefer the post-deselect figure.
    m_sel = re.search(r"(\d+)\s*/\s*\d+\s+tests?\s+collected", tail,
                      re.IGNORECASE)
    if m_sel:
        selected = int(m_sel.group(1))
    elif collected is not None:
        selected = collected
    if proc.returncode != 0 and selected is None:
        # Collection error (import failure / syntax error) — that is itself a
        # hard regression; surface it.
        sys.stderr.write(tail[-4000:] + "\n")
    return (selected if selected is not None else 0), tail


def guard_count() -> int:
    count, _tail = _run_collect()
    if count < MIN_OFFLINE_TESTS:
        sys.stderr.write(
            f"\n[ci-guard] FAIL: offline test count {count} < baseline "
            f"{MIN_OFFLINE_TESTS} (sampled 2026-09-18). A test file may be "
            f"missing, collection broken, or the deselect marker over-broad. "
            f"Do not lower the baseline silently; restore the tests or edit "
            f"MIN_OFFLINE_TESTS in scripts/ci_test_guard.py in a reviewed "
            f"commit that justifies the change.\n"
        )
        return 1
    print(f"[ci-guard] OK: offline test count {count} >= baseline "
          f"{MIN_OFFLINE_TESTS}")
    return 0


def guard_walltime(junit_path: str) -> int:
    try:
        tree = ET.parse(junit_path)
    except (FileNotFoundError, ET.ParseError) as exc:
        print(f"[ci-guard] walltime SKIP: cannot read {junit_path} ({exc})")
        return 0
    root = tree.getroot()
    # pytest writes a top-level <testsuites><testsuite time="...">; with a
    # single suite the root may itself be <testsuite>. Sum suite wall times.
    suites = root.findall(".//testsuite")
    if not suites and root.tag == "testsuite":
        suites = [root]
    total = 0.0
    for ts in suites:
        try:
            total += float(ts.get("time", "0") or 0.0)
        except ValueError:
            pass
    if total <= 0:
        print("[ci-guard] walltime SKIP: no suite time in JUnit XML")
        return 0
    if total > WARN_TOTAL_WALLTIME_S:
        # Warn-only: noisy shared runners mean this is not a hard failure.
        print(f"::warning::[ci-guard] offline suite wall time {total:.1f}s "
              f"exceeds warn threshold {WARN_TOTAL_WALLTIME_S:.0f}s "
              f"(baseline ~360s sampled 2026-09-18 on dev host). Confirm "
              f"locally that a change did not regress performance.")
    else:
        print(f"[ci-guard] walltime OK: {total:.1f}s <= "
              f"{WARN_TOTAL_WALLTIME_S:.0f}s warn threshold")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("count", help="hard-fail if offline test count < baseline")
    wt = sub.add_parser("walltime", help="warn if suite wall time exceeds cap")
    wt.add_argument("--junitxml", required=True, help="path to pytest JUnit XML")
    args = parser.parse_args(argv)
    if args.cmd == "count":
        return guard_count()
    if args.cmd == "walltime":
        return guard_walltime(args.junitxml)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
