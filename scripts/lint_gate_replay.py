#!/usr/bin/env python3
"""T-22 闸门有效性自检：在历史提交上重放 ruff lint 闸门。

闸门是验证一切其他工作的元工具，它的有效性必须被持续验证（否则会像
FND-01 那样静默退化几百个提交而无人察觉）。本脚本把"在历史提交上重放
lint 闸门"做成可重复执行的检查：

* 对每个固定的历史提交，在临时 git worktree（detached）中检出该提交的
  完整树，用当前 ruff 二进制按【该提交自身的 pyproject.toml 配置】跑
  ``ruff check``，统计错误数 —— 与采样基线逐位比对。
* 基线（2026-09-25 在 dev host 实测，ruff 见 .venv）：
    cf5245e : 0 errors（P1-2 显式 ruff 契约 + noqa baseline，绿灯）
    a38df9e : 164 errors（契约刚引入、历史债尚未清的红灯快照）
    HEAD    : 0 errors（当前主干必须保持绿）

为什么这能抓住"闸门被人为削弱"：a38df9e 是一个【已知必须红】的冻结快照。
若有人通过放宽规则集 / 删除检查来削弱闸门，这个历史红灯会随之转绿或错误
数变化，自检立刻失败 —— 削弱 lint 闸门无法再悄无声息地让历史红变白。
反之 cf5245e/HEAD 必须保持绿：若有人【收紧】规则到连历史绿灯都过不了，
同样会被捕获。两个方向都被钉住。

只读取向、用独立临时 worktree，不触碰也不污染当前工作区与索引。

用法：
  python scripts/lint_gate_replay.py [--ruff PATH] [--keep-worktree]
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# (提交, 期望 ruff 错误数, 说明)。HEAD 占位在运行时解析。
EXPECTATIONS = [
    ("cf5245e", 0, "P1-2 显式 ruff 契约：必须绿"),
    ("a38df9e", 164, "契约引入后的红灯快照：必须仍为 164（防削弱）"),
    ("HEAD", 0, "当前主干：必须绿"),
]

_CHECK_DIRS = ("hermes_trader", "scripts", "tests")
_ERR_COUNT = re.compile(r"Found (\d+) errors?", re.IGNORECASE)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args],
        capture_output=True, text=True, check=check,
    )


def _ruff_errors(ruff: str, worktree: Path) -> int:
    """在 worktree（含该提交自身 pyproject.toml）跑 ruff，返回错误总数。"""
    proc = subprocess.run(
        [ruff, "check", *(str(worktree / d) for d in _CHECK_DIRS)],
        capture_output=True, text=True, check=False,
    )
    tail = (proc.stdout or "") + (proc.stderr or "")
    if "All checks passed" in tail:
        return 0
    m = _ERR_COUNT.search(tail)
    if m is None:
        # 既没绿也没给出计数 —— ruff 本身崩了，视作硬失败而非 0。
        raise RuntimeError(f"ruff 无法解析结果:\n{tail[-2000:]}")
    return int(m.group(1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ruff", default=str(_REPO / ".venv" / "bin" / "ruff"),
                    help="ruff 可执行文件路径（默认 .venv/bin/ruff）")
    ap.add_argument("--keep-worktree", action="store_true",
                    help="调试用：保留临时 worktree（默认自动清理）")
    args = ap.parse_args()

    if not Path(args.ruff).exists():
        sys.stderr.write(f"[lint-replay] 找不到 ruff：{args.ruff}\n")
        return 2

    failures: list[str] = []
    base = tempfile.mkdtemp(prefix="t22-lint-replay-")

    try:
        for ref, expected, note in EXPECTATIONS:
            wt = Path(base) / ref.replace("/", "_")
            _git("worktree", "add", "--detach", str(wt), ref)
            try:
                got = _ruff_errors(args.ruff, wt)
            finally:
                if not args.keep_worktree:
                    _git("worktree", "remove", "--force", str(wt), check=False)
            status = "OK" if got == expected else "FAIL"
            print(f"[lint-replay] {status}: {ref:<8s} errors={got:<4d} "
                  f"期望={expected:<4d} {note}")
            if got != expected:
                failures.append(f"{ref}: got {got}, expected {expected}")
    finally:
        _git("worktree", "prune", check=False)
        if not args.keep_worktree:
            shutil.rmtree(base, ignore_errors=True)

    if failures:
        sys.stderr.write(
            "\n[lint-replay] 自检失败：lint 闸门的历史行为发生变化。\n"
            "  若红灯快照(a38df9e)转绿/计数下降 ⇒ 闸门可能被削弱；\n"
            "  若绿灯点(cf5245e/HEAD)变红 ⇒ 规则可能被收紧到破坏基线。\n"
            "  请勿修改本脚本里的基线数字来强行变绿；先恢复闸门，再在\n"
            "  评审通过的提交中更新 EXPECTATIONS 并写明理由。\n"
            + "".join(f"  - {f}\n" for f in failures))
        return 1

    print("[lint-replay] 全部通过：历史红灯仍红、绿灯仍绿，闸门有效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
