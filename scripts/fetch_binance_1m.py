#!/usr/bin/env python3
"""从 Binance 数据归档域拉取真实 1m K线并落盘（离线缓存，无鉴权）。

为什么单独写：
  当前网络阻断 api.binance.com / fapi.binance.com，但数据归档域
  data-api.binance.vision 可直连，返回与现货 api/v3/klines 完全一致的结构。
  标准库 urllib 即可（不依赖 SMART_PROXY 设置）。仅用于研究/影子标定，
  不参与任何实盘下单。

输出布局与 logs/binance_klines_cache 现有文件一致：
  list[[open_ms, o, h, l, c, volume, close_ms, quote_vol, trades, ...], ...]
  排序去重、可选按时间范围裁剪。

用法：
  python scripts/fetch_binance_1m.py BTCUSDT --days 60 \
      --out logs/binance_klines_cache/BTCUSDT_1m.json
  python scripts/fetch_binance_1m.py BTCUSDT ETHUSDT SOLUSDT --days 45
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

BASE = "https://data-api.binance.vision/api/v3/klines"
PAGE_LIMIT = 1000
# 实测该域限频较紧（每千根约 5–7s）；保守 sleep，429/418 时指数退避。
PAGE_SLEEP_S = 0.8
MAX_RETRY = 5


def _get_page(symbol: str, start_ms: int, limit: int = PAGE_LIMIT) -> list[list]:
    url = (f"{BASE}?symbol={symbol}&interval=1m"
           f"&startTime={start_ms}&limit={limit}")
    delay = 2.0
    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            status = getattr(exc, "code", None)
            if status in (429, 418) or attempt < MAX_RETRY - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"unreachable retry loop for {symbol}@{start_ms}")


def fetch_range(symbol: str, start_ms: int, end_ms: int,
                progress: bool = True) -> list[list]:
    """分页拉取 [start_ms, end_ms) 的 1m K线，按 open_ms 去重排序。"""
    out: dict[int, list] = {}
    cursor = start_ms
    pages = 0
    while cursor < end_ms:
        page = _get_page(symbol, cursor)
        if not page:
            break
        for row in page:
            out[int(row[0])] = row
        last_open = int(page[-1][0])
        pages += 1
        if progress and pages % 10 == 0:
            done = last_open - start_ms
            pct = 100.0 * done / max(1, end_ms - start_ms)
            print(f"  {symbol} pages={pages} bars={len(out)} {pct:5.1f}%",
                  flush=True)
        if len(page) < PAGE_LIMIT:
            break
        nxt = last_open + 60_000
        if nxt <= cursor:  # 防御：不前进则退出
            break
        cursor = nxt
        time.sleep(PAGE_SLEEP_S)
    rows = [out[k] for k in sorted(out) if start_ms <= k < end_ms]
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="+", help="如 BTCUSDT ETHUSDT SOLUSDT")
    ap.add_argument("--days", type=float, default=45.0, help="回溯天数")
    ap.add_argument("--end", default=None,
                    help="结束时间 YYYY-MM-DD（默认当前 UTC）")
    ap.add_argument("--out-dir", default="logs/binance_klines_cache")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在文件，否则跳过")
    args = ap.parse_args(argv)

    end_dt = (dt.datetime.strptime(args.end, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc) if args.end
        else dt.datetime.now(dt.timezone.utc))
    end_ms = int(end_dt.timestamp() * 1000)
    start_ms = end_ms - int(args.days * 86400 * 1000)
    os.makedirs(args.out_dir, exist_ok=True)

    for sym in args.symbols:
        path = os.path.join(args.out_dir, f"{sym}_1m.json")
        if os.path.exists(path) and not args.force:
            print(f"[skip] {path} exists (use --force)")
            continue
        print(f"[fetch] {sym} 1m  "
              f"{dt.datetime.utcfromtimestamp(start_ms/1000)} -> "
              f"{dt.datetime.utcfromtimestamp(end_ms/1000)}")
        t0 = time.time()
        rows = fetch_range(sym, start_ms, end_ms)
        if not rows:
            print(f"  ERROR: no rows for {sym}")
            continue
        # 连续性体检（1m 应严格 60000ms 步进）
        gaps = sum(1 for i in range(1, len(rows))
                   if int(rows[i][0]) - int(rows[i - 1][0]) != 60_000)
        with open(path, "w") as fh:
            json.dump(rows, fh, separators=(",", ":"))
        print(f"  saved {len(rows)} bars -> {path} gaps={gaps} "
              f"secs={round(time.time()-t0,1)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
