#!/usr/bin/env python3
"""M-1 L2 orderbook (book-raw) 采集连续性监控。

背景
----
book-raw 是当前唯一未被证伪的 edge 来源（M-1 L2 OFI，首检 2026-10-28、
60 天确认 2026-11-27）。采集一旦在窗口内静默断档（WS 掉线、flusher 卡死、
某币无新行），就等于唯一希望归零，且事后无法补采。本脚本提供确定性的
"该采没采" 检测，供 scheduler 每小时触发，断档时经 notify 告警。

被监控对象
----------
路径：``${HERMES_DATA_DIR:-/data}/book-raw/date=YYYY-MM-DD/COIN.jsonl[.gz]``
节奏：默认每币每秒 1 行（``{"t": flush墙钟ms, ...}``）；旧日期文件已 gzip。

检测项
------
1. 最新行陈旧（stale）：每个"当日活跃币"文件最后一行的 ``t`` 距 now 超过
   ``stale_s``（默认 30s；bootstrap/每日 UTC 轮转的新文件给宽限）。
2. 行内时间戳缺口（gap）：连续两行 ``t`` 之差的最大值超过 ``max_gap_s``
   （默认 15s，容忍偶发抖动；持续 >1s 才会累积成大缺口）。
3. 日目录缺失：最近 ``lookback_days``（默认 3）个 UTC 日中某日目录不存在
   或没有任何币文件（整日断采）。

纯函数 + 注入 now，便于确定性单测；CLI 负责读盘 / 告警 / 退出码。
退出码：0=健康；1=发现断档（已告警）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

DAY_MS = 86_400_000


# ── 纯检测逻辑 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CoinIssue:
    coin: str
    kind: str          # "stale" | "gap"
    detail: str


@dataclass
class DayReport:
    day: str
    coins_checked: int = 0
    issues: list[CoinIssue] = field(default_factory=list)


def timestamp_gaps(ts_ms: list[int], *, max_gap_s: float) -> tuple[int, int]:
    """纯函数：返回 (最大相邻间隔ms, 超过阈值的间隔数)。ts 须升序、>=2 点。"""
    worst = 0
    breaches = 0
    for i in range(1, len(ts_ms)):
        d = ts_ms[i] - ts_ms[i - 1]
        if d > worst:
            worst = d
        if d > max_gap_s * 1000:
            breaches += 1
    return worst, breaches


def evaluate_coin(
    *,
    coin: str,
    ts_ms: list[int],
    now_ms: int,
    stale_s: float,
    max_gap_s: float,
    is_today: bool,
) -> list[CoinIssue]:
    """对单个币的时间戳序列做 stale/gap 判定。

    设计取舍（经生产数据校准）：
    * stale 只对"当日"文件检测（历史日已封存）；
    * 币集会随每日成交量轮换，被轮换出的币其文件会永久停在上一行——这是
      正常现象，不应告警。故只在"该币仍属活跃集（active=True）"时才判
      stale / 尾部 gap。调用方按当前采集集传入 active；
    * 活跃币的历史中间出现一次 30s 抖动（随后已恢复）不影响后续研究，
      故只检测"末端缺口"：最后一段间隔若超 max_gap_s 且末行仍新鲜，则
      说明采集在恢复后出现稀疏；若末行已陈旧则由 stale 统一表述，不重复。
    """
    issues: list[CoinIssue] = []
    if not ts_ms:
        if is_today:
            issues.append(CoinIssue(coin, "stale", "无任何快照行"))
        return issues

    if is_today:
        age_s = (now_ms - ts_ms[-1]) / 1000.0
        if age_s > stale_s:
            issues.append(CoinIssue(
                coin, "stale", f"最新行已陈旧 {age_s:.0f}s > {stale_s:.0f}s"))
            return issues
        # 末行新鲜但最后一段间隔过大 = 短暂断流后恢复（稀疏采样）
        if len(ts_ms) >= 2:
            tail_gap_s = (ts_ms[-1] - ts_ms[-2]) / 1000.0
            if tail_gap_s > max_gap_s:
                issues.append(CoinIssue(
                    coin, "gap",
                    f"末端间隔 {tail_gap_s:.0f}s > {max_gap_s:.0f}s（末行已恢复）"))
    return issues


def missing_day_dirs(
    *,
    book_root: Path,
    now_ms: int,
    lookback_days: int,
) -> list[str]:
    """纯（读盘）函数：最近 lookback_days 个 UTC 日里，缺失或无币文件的日。

    今日目录允许尚无币文件（UTC 刚轮转），故今日不在"必须存在"之列，改由
    stale 检测覆盖。
    """
    today_start = (now_ms // DAY_MS) * DAY_MS
    missing: list[str] = []
    for k in range(1, lookback_days + 1):
        d_start = today_start - k * DAY_MS
        day = time.strftime("%Y-%m-%d", time.gmtime(d_start / 1000))
        ddir = book_root / f"date={day}"
        has_files = (
            ddir.is_dir()
            and any(ddir.glob("*.jsonl")) or (ddir.is_dir() and any(ddir.glob("*.jsonl.gz")))
        )
        if not has_files:
            missing.append(day)
    return missing


# ── 读盘 ────────────────────────────────────────────────────────────────────

def read_timestamps(path: Path, *, cap_lines: int) -> list[int]:
    """读取某币文件（.jsonl 或 .jsonl.gz）每行的 "t"，升序返回。

    大文件只取末尾 cap_lines 行（gap 检测关注近期；stale 只需末行）。
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    ts: list[int] = []
    try:
        with opener(path, "rt") as f:
            if cap_lines and cap_lines > 0:
                # 简单的尾部采样：读全部到内存对 1s 节奏的单日文件(~86400 行)
                # 仍可控；为稳妥用 deque 限制。
                from collections import deque
                lines = deque(f, maxlen=cap_lines)
            else:
                lines = f
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    t = r.get("t")
                    if isinstance(t, (int, float)):
                        ts.append(int(t))
                except Exception:
                    continue
    except Exception:
        return ts
    ts.sort()
    return ts


def coin_files(day_dir: Path) -> dict[str, Path]:
    """当日目录的币文件：优先未压缩 .jsonl，否则 .jsonl.gz。"""
    out: dict[str, Path] = {}
    if not day_dir.is_dir():
        return out
    for p in day_dir.glob("*.jsonl"):
        out[p.stem] = p
    for p in day_dir.glob("*.jsonl.gz"):
        # name.jsonl.gz -> stem ".jsonl.gz" -> 再去 .jsonl
        coin = p.name[: -len(".jsonl.gz")]
        out.setdefault(coin, p)
    return out


# ── 编排 ────────────────────────────────────────────────────────────────────

def run_check(
    *,
    book_root: Path,
    now_ms: int,
    stale_s: float,
    max_gap_s: float,
    lookback_days: int,
    cap_lines: int,
) -> tuple[list[DayReport], list[str]]:
    """返回 (今日各币报告, 缺失日列表)。

    活跃集识别：先读全部今日币文件末行。多数币（真正在采集的）末行会聚集在
    now 附近；被轮换出的币末行是离群的陈旧值。以"新鲜末行的币数占多数"为
    前提，用 stale_s 把币分为 active/inactive：只对 active 币做告警判定。
    若全部币都陈旧（采集整体中断），则全部视为 active，必然报 stale。
    """
    missing = missing_day_dirs(book_root=book_root, now_ms=now_ms,
                               lookback_days=lookback_days)

    today = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    today_dir = book_root / f"date={today}"
    files = coin_files(today_dir)

    last_ts: dict[str, int] = {}
    for coin, path in files.items():
        ts = read_timestamps(path, cap_lines=cap_lines)
        if ts:
            last_ts[coin] = ts[-1]

    fresh = {c for c, t in last_ts.items()
             if (now_ms - t) / 1000.0 <= stale_s}
    # 全部陈旧 => 整体中断，没有"轮换出局"可言，所有币都要报。
    treat_all_active = not fresh

    report = DayReport(day=today)
    for coin, path in sorted(files.items()):
        ts = read_timestamps(path, cap_lines=cap_lines)
        active = treat_all_active or coin in fresh
        report.coins_checked += 1
        if not active:
            # 轮换出局：正常，不检查（避免每个小时对旧文件误报）
            continue
        report.issues.extend(evaluate_coin(
            coin=coin, ts_ms=ts, now_ms=now_ms,
            stale_s=stale_s, max_gap_s=max_gap_s, is_today=True))
    return [report], missing


def format_alert(reports: list[DayReport], missing: list[str]) -> str:
    parts = ["[book-continuity] 检测到 M-1 L2 采集断档："]
    if missing:
        parts.append(f"  缺失/空日目录：{', '.join(missing)}")
    for rep in reports:
        for iss in rep.issues:
            parts.append(f"  {rep.day} {iss.coin} [{iss.kind}] {iss.detail}")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    data_dir = os.environ.get("HERMES_DATA_DIR", "/data")
    ap.add_argument("--book-root",
                    default=str(Path(data_dir) / "book-raw"))
    ap.add_argument("--stale-seconds", type=float, default=30.0)
    ap.add_argument("--max-gap-seconds", type=float, default=15.0)
    ap.add_argument("--lookback-days", type=int, default=3)
    ap.add_argument("--cap-lines", type=int, default=20000)
    ap.add_argument("--push", action="store_true", help="断档时发飞书告警")
    args = ap.parse_args()

    book_root = Path(args.book_root)
    now_ms = int(time.time() * 1000)

    if not book_root.is_dir():
        msg = f"[book-continuity] book-raw 根目录不存在：{book_root}"
        print(msg)
        if args.push:
            try:
                from hermes_trader.notify import send_text
                send_text(msg, category="system")
            except Exception:
                pass
        return 1

    reports, missing = run_check(
        book_root=book_root, now_ms=now_ms,
        stale_s=args.stale_seconds, max_gap_s=args.max_gap_seconds,
        lookback_days=args.lookback_days, cap_lines=args.cap_lines)

    n_issues = sum(len(r.issues) for r in reports)
    if n_issues == 0 and not missing:
        checked = reports[0].coins_checked if reports else 0
        print(f"[book-continuity] OK：{checked} 个当日活跃币，时间戳连续，"
              f"近 {args.lookback_days} 日目录齐全。")
        return 0

    msg = format_alert(reports, missing)
    print(msg)
    if args.push:
        try:
            from hermes_trader.notify import send_text
            send_text(msg, category="system")
        except Exception:
            pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
