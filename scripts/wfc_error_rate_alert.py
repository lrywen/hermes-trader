#!/usr/bin/env python3
"""WF-C：按天统计 ERROR 行数并在超阈时告警（只读）。

依据改造方案 §4.2：正常日 ERROR 个位~几十，2026-10-23 出现约 958 行峰值（含
Max-retries、funding timeout、429 退避）。本脚本扫描会话日志（容器
/data/session-log.jsonl 或 --file 指定的主机副本），按 UTC 天聚合并对
"今天/最近 N 天"做阈值判定，超阈则：打印 ALERT 并以退出码 2 返回，便于挂 cron
/监控。纯只读，不改任何系统状态。

日志行可能是 JSON（优先取 level/ts/message）或纯文本（按子串归类，时间取行首
ISO 时间戳，无法解析则计入 unknown-day）。

用法：
  python scripts/wfc_error_rate_alert.py                      # 默认扫 /data/session-log.jsonl
  python scripts/wfc_error_rate_alert.py --file logs/x.log --warn 50 --days 7
  echo "$?"   # 0=正常 2=超阈 3=文件缺失
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from typing import Optional

DEFAULT_PATHS = ("/data/session-log.jsonl",)
CATEGORY_PATTERNS = {
    "max_retries": re.compile(r"max[- ]?retries|retry", re.I),
    "funding_timeout": re.compile(r"funding.*timeout|timeout.*funding", re.I),
    "rate_limit_429": re.compile(r"\b429\b|rate.?limit|backoff|too many requests", re.I),
    "feed_stale": re.compile(r"stale|陈旧|feed", re.I),
}
# 行首 ISO8601（2026-10-23T08:11:42 或 2026-10-23 08:11:42）
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}:\d{2}")


def _is_error(line: str, obj: Optional[dict]) -> bool:
    if obj is not None:
        lvl = str(obj.get("level") or obj.get("severity") or "").upper()
        if lvl == "ERROR":
            return True
    return '"level": "ERROR"' in line or '"level":"ERROR"' in line or \
        re.search(r"\bERROR\b", line) is not None


def _day_of(line: str, obj: Optional[dict]) -> str:
    if obj is not None:
        for k in ("ts", "timestamp", "time", "@timestamp"):
            v = obj.get(k)
            if isinstance(v, (int, float)) and v > 0:
                ms = v if v > 1e12 else v * 1000
                return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
            if isinstance(v, str):
                m = _TS_RE.search(v)
                if m:
                    return m.group(1)
    m = _TS_RE.search(line[:40])
    return m.group(1) if m else "unknown-day"


def scan(path: str) -> tuple[Counter, Counter, int]:
    by_day = Counter()
    cat_day: dict[str, Counter] = {}
    total = 0
    with open(path, errors="replace") as fh:
        for line in fh:
            if not _is_error(line, _try_json(line)):
                continue
            total += 1
            day = _day_of(line, _try_json(line))
            by_day[day] += 1
            for name, pat in CATEGORY_PATTERNS.items():
                if pat.search(line):
                    cat_day.setdefault(day, Counter())[name] += 1
    return by_day, cat_day, total


def _try_json(line: str) -> Optional[dict]:
    line = line.strip()
    if line.startswith("{"):
        try:
            o = json.loads(line)
            return o if isinstance(o, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=None)
    ap.add_argument("--warn", type=int, default=100,
                    help="单日 ERROR 行数告警阈值（默认 100）")
    ap.add_argument("--days", type=int, default=1,
                    help="评估最近 N 个 UTC 日（默认 1=今天）")
    args = ap.parse_args(argv)

    path = args.file or next((p for p in DEFAULT_PATHS
                              if _exists(p)), None)
    if not path or not _exists(path):
        print(json.dumps({"status": "no_log",
                          "searched": [args.file] if args.file else list(DEFAULT_PATHS)},
                         ensure_ascii=False))
        return 3

    by_day, cat_day, total = scan(path)
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    recent = [(d, by_day[d]) for d in sorted(by_day, reverse=True)[:args.days]]
    peak_day, peak_n = (max(by_day.items(), key=lambda kv: kv[1])
                        if by_day else ("none", 0))
    today_n = by_day.get(today, 0)
    breaches = [d for d, n in recent if d != "unknown-day" and n > args.warn]

    payload = {
        "file": path,
        "warn_per_day": args.warn,
        "window_days": args.days,
        "today_utc": today,
        "errors_today": today_n,
        "recent_daily_errors": dict(recent),
        "peak_day": peak_day, "peak_errors": peak_n,
        "total_errors_in_file": total,
        "recent_categories": {d: dict(cat_day.get(d, {})) for d, _ in recent},
        "breaching_days": breaches,
        "status": "ALERT" if breaches else "OK",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if breaches else 0


def _exists(p: str) -> bool:
    import os
    return bool(p) and os.path.exists(p)


if __name__ == "__main__":
    sys.exit(main())
