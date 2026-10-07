#!/usr/bin/env python3
"""采集 Hyperliquid 永续历史资金费率（funding）到本地，供 carry/funding 研究。

为什么
------
M-2~M-10 与 V-01 已证明：币级"方向预测"在剥离市场 beta 后 edge 归零。funding
是少数**不依赖方向预测**的结构性收益来源（杠杆拥挤方向为持仓付费）。本脚本把
历史 funding 拉成可回溯面板，是 funding/carry 检验的前置数据。

数据
----
HL ``info`` 接口 ``fundingHistory``：每小时一条，字段
``{coin, fundingRate(分数, 8h口径年化前的每小时费率), premium, time(ms)}``；
单次最多返回 500 行（约 20 天），按 time 升序，需用末行 time+1 分页。

落盘
----
``${HERMES_DATA_DIR}/funding/{coin}.jsonl.gz``：每币一个 gzip 追加文件，
按 time 去重。**增量**：若文件已存在，从最后一条 time+1 继续，不重复拉取。

安全
----
只读公开端点、只写可再生成的 funding 目录；不加载私钥、不下单。--dry-run 只打印
计划。

用法
----
    python3 scripts/collect_funding.py --days 400 --coins 50
    python3 scripts/collect_funding.py --coin ETH BTC --days 730
    python3 scripts/collect_funding.py --days 400 --dry-run
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("HERMES_BACKTEST", "1")

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))

from hermes_trader.client.universe import get_universe  # noqa: E402

_API = "https://api.hyperliquid.xyz/info"
_PAGE = 500                 # fundingHistory 单次上限
_HOUR_MS = 3_600_000
_EARLIEST = 1_704_067_200_000     # 2024-01-01，作为回填下界兜底


def _post(payload: dict, *, retries: int = 3):
    body = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                _API, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))


def fetch_page(coin: str, start_ms: int) -> list[dict]:
    return _post({"type": "fundingHistory", "coin": coin,
                  "startTime": start_ms})


def select_coins(n: int, *, explicit=None, exclude_hip3=True) -> list[str]:
    if explicit:
        out = []
        for c in explicit:
            c = c.strip()
            if c and c not in out:
                out.append(c)
        return out
    perps = [m for m in get_universe() if m.get("type") == "perp"
             and not str(m.get("coin", "")).startswith("@")]
    if exclude_hip3:
        perps = [m for m in perps if ":" not in str(m.get("coin", ""))]
    perps.sort(key=lambda m: m.get("dayNtlVlm", 0) or 0, reverse=True)
    return [str(m["coin"]) for m in perps[:n]]


def funding_dir() -> Path:
    d = Path(os.environ.get("HERMES_DATA_DIR", "/data")) / "funding"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt") as f:
        return [json.loads(x) for x in f if x.strip()]


def _pull_range(coin, a, b, rows, have_times, *, on_page=None) -> int:
    """分页拉取 [a,b]（ms）内 funding，合并去重进 rows。返回新增条数。"""
    added = 0
    page_start = a
    while page_start <= b:
        page = fetch_page(coin, page_start)
        if not page:
            break
        for r in page:
            t = int(r["time"])
            if t < a or t > b:
                continue
            if t not in have_times:
                rows.append({"coin": coin,
                             "fundingRate": r.get("fundingRate"),
                             "premium": r.get("premium"),
                             "time": t})
                have_times.add(t)
                added += 1
        last_t = int(page[-1]["time"])
        if on_page:
            on_page(coin, last_t, len(page), added)
        if len(page) < _PAGE:
            break
        page_start = last_t + 1
    return added


def collect_coin(coin: str, start_ms: int, end_ms: int, *,
                 on_page=None) -> tuple[int, int]:
    """增量拉取 coin 的 funding（可向前回填、可向后更新），去重落盘。

    返回(已有非新增条数, 新增条数)。funding 不可变，故仅拉缺失区间：
    首次拉 [start,end]；已有则分别补 [start,min-1] 与 [max+1,end]。
    """
    path = funding_dir() / f"{coin}.jsonl.gz"
    rows = read_existing(path)
    have_times = {r["time"] for r in rows}
    before = len(rows)
    added = 0
    if not have_times:
        added += _pull_range(coin, start_ms, end_ms, rows, have_times,
                             on_page=on_page)
    else:
        lo, hi = min(have_times), max(have_times)
        if start_ms < lo:
            added += _pull_range(coin, start_ms, lo - 1, rows, have_times,
                                 on_page=on_page)
        if end_ms > hi:
            added += _pull_range(coin, hi + 1, end_ms, rows, have_times,
                                 on_page=on_page)
    rows.sort(key=lambda r: r["time"])
    tmp = path.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return before, added


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=400)
    ap.add_argument("--end", default="now")
    ap.add_argument("--coins", type=int, default=50)
    ap.add_argument("--coin", nargs="+", default=None)
    ap.add_argument("--include-hip3", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.end == "now":
        end_ms = int(time.time() * 1000)
    else:
        dt = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        end_ms = int(dt.timestamp() * 1000)
    start_ms = max(_EARLIEST, end_ms - int(args.days * 86_400_000))

    coins = select_coins(args.coins, explicit=args.coin,
                         exclude_hip3=not args.include_hip3)
    fdir = Path(os.environ.get("HERMES_DATA_DIR", "/data")) / "funding"

    if args.dry_run:
        print("=== collect_funding DRY RUN ===")
        print(f"range: {datetime.fromtimestamp(start_ms/1000, timezone.utc):%Y-%m-%d}"
              f" ~ {datetime.fromtimestamp(end_ms/1000, timezone.utc):%Y-%m-%d} UTC")
        print(f"coins({len(coins)}): {', '.join(coins)}")
        print(f"dir  : {fdir}")
        hours = (end_ms - start_ms) // _HOUR_MS
        print(f"~{hours} rows/coin x {len(coins)} coins "
              f"= ~{hours*len(coins)} rows; requests/coin ~{max(1,hours//_PAGE)}")
        return 0

    print(f"collecting funding {len(coins)} coins, "
          f"{args.days:g}d -> {fdir}")
    tot_exist = tot_added = 0
    for c in coins:
        try:
            exist, added = collect_coin(
                c, start_ms, end_ms,
                on_page=lambda cc, t, pn, ad: None)
            tot_exist += exist
            tot_added += added
            print(f"  {c:<12} total {exist + added:>6} (+{added:>5} new)")
        except Exception as e:
            print(f"  {c:<12} ERROR {e!r}")
        if args.sleep:
            time.sleep(args.sleep)
    print("=" * 60)
    print(f"funding rows: {tot_exist} existing + {tot_added} new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
