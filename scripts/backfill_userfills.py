#!/usr/bin/env python3
"""从 Hyperliquid userFills 重建丢失的真实成交并补录到独立 JSONL。

用途：当本地所有日志都无法保留某段真实成交（memory 达上限裁剪、session-log
轮转删除、events.jsonl 路径迁移）时，用本脚本从交易所 ``userFillsByTime``
拉取该钱包真实成交，规范化后追加到独立的 append-only 文件
（默认 /data/userfills-backfill.jsonl）。该文件：

  * 不参与 session-log 轮转、不受 memory 保留上限影响；
  * 按 events.jsonl 的嵌套形态 {event, timestamp(ISO), payload} 存储，dashboard
    直接复用 outcome 的 execute/close parser 与开平仓配对（见 dashboard.
    _read_backfill_lines）。

成交方向以 userFills 的 ``dir`` 字段为准（Open Long / Close Long / Open Short /
Close Short），它比 side(B/A) 更直接。``closedPnl`` 为该平仓扣除平仓手续费后的
已实现盈亏；开仓手续费在 close 记录里按名义 2.5bps 估算并入 fee，使净额口径与
正常 DSL 平仓一致。

幂等：以 userFills 的 tid 作为唯一键，已存在于补录文件的成交不会重复写入；
默认 dry-run，需显式 --commit 才落盘。

例：
    python scripts/backfill_userfills.py \
        --start 2026-08-20 --end 2026-08-27 --commit
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.request

_API_URL = os.environ.get("HL_API_URL", "https://api.hyperliquid.xyz/info")
_BACKFILL_PATH = os.environ.get("HERMES_USERFILLS_BACKFILL_FILE", "/data/userfills-backfill.jsonl")

_OPEN_DIRS = {"Open Long": "long", "Open Short": "short"}
_CLOSE_DIRS = {"Close Long": "long", "Close Short": "short"}
_ENTRY_FEE_FRAC = 0.00025  # taker 2.5bps，开仓手续费估算


def _post(body: dict) -> list[dict]:
    req = urllib.request.Request(
        _API_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected userFillsByTime response: {data!r}")
    return data


def _parse_date(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_fills(user: str, start_ms: int, end_ms: int) -> list[dict]:
    """拉取窗口内全部成交（userFillsByTime 单次有上限，分页拉取）。"""
    out: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _post({"type": "userFillsByTime", "user": user,
                       "startTime": cursor, "endTime": end_ms})
        if not batch:
            break
        out.extend(batch)
        last = max(int(f["time"]) for f in batch)
        if len(batch) < 2000 or last <= cursor:
            break
        cursor = last + 1
    # tid 全局唯一去重（分页边界可能重叠）。
    dedup = {f["tid"]: f for f in out}
    return sorted(dedup.values(), key=lambda f: int(f["time"]))


def _open_record(f: dict, side: str) -> dict:
    px = float(f["px"])
    sz = float(f["sz"])
    notional = round(px * sz, 6)
    return {
        "event": "execute",
        "timestamp": _iso(int(f["time"])),
        "payload": {
            "executed": True,
            "coin": f["coin"], "side": side,
            "entry_px": px, "size_usd": notional,
            "oid": int(f["oid"]),
        },
        "_tid": f["tid"],
        "_time": int(f["time"]),
    }


def _close_record(f: dict, side: str) -> dict:
    px = float(f["px"])
    sz = float(f["sz"])
    notional = round(px * sz, 6)
    closed_pnl = float(f["closedPnl"])
    close_fee = float(f["fee"])
    entry_fee = round(notional * _ENTRY_FEE_FRAC, 6)
    net_usd = round(closed_pnl - entry_fee, 6)
    # 成交口径的净收益率（净额/名义×100×杠杆）。userFills 不带杠杆，按 1 处理使
    # 百分比=净美元/名义；dashboard 缺杠杆时本就按非杠杆口径展示这些小测试单。
    realized_pct = round(net_usd / notional * 100.0, 6) if notional else None
    return {
        "event": "close",
        "timestamp": _iso(int(f["time"])),
        "payload": {
            "coin": f["coin"], "side": side,
            "closed_at": int(f["time"]),
            "exit_px": px,
            "notional_usd": notional,
            "realized_pnl_pct": realized_pct,
            "realized_pnl_usd": net_usd,
            "gross_pnl_usd": round(closed_pnl + close_fee, 6),
            "fee_usd": round(close_fee + entry_fee, 6),
            "close_source": "exchange_userfills_backfill",
            "close_oid": int(f["oid"]),
        },
        "_tid": f["tid"],
        "_time": int(f["time"]),
    }


def normalize(fills: list[dict]) -> list[dict]:
    records: list[dict] = []
    for f in fills:
        d = f.get("dir")
        if d in _OPEN_DIRS:
            records.append(_open_record(f, _OPEN_DIRS[d]))
        elif d in _CLOSE_DIRS:
            records.append(_close_record(f, _CLOSE_DIRS[d]))
    records.sort(key=lambda r: r["_time"])
    return records


def _existing_tids(path: str) -> set:
    tids: set = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("_tid") is not None:
                    tids.add(rec["_tid"])
    except FileNotFoundError:
        pass
    return tids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user", default=os.environ.get("HYPERLIQUID_WALLET_ADDRESS"),
                    help="钱包地址，默认读 HYPERLIQUID_WALLET_ADDRESS")
    ap.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD (UTC)")
    ap.add_argument("--out", default=_BACKFILL_PATH, help="补录文件路径")
    ap.add_argument("--commit", action="store_true", help="真正写入；默认 dry-run")
    args = ap.parse_args()
    if not args.user:
        ap.error("缺少钱包地址：--user 或设置 HYPERLIQUID_WALLET_ADDRESS")

    fills = fetch_fills(args.user, _parse_date(args.start), _parse_date(args.end))
    records = normalize(fills)
    existing = _existing_tids(args.out)
    fresh = [r for r in records if r["_tid"] not in existing]

    opens = sum(1 for r in fresh if r["event"] == "execute")
    closes = sum(1 for r in fresh if r["event"] == "close")
    print(f"窗口成交 {len(fills)} 笔；规范化开 {opens} / 平 {closes}；"
          f"已存在 {len(records) - len(fresh)}；待写入 {len(fresh)}")

    if not args.commit:
        print("dry-run：未落盘。确认无误后加 --commit 执行。")
        return 0
    if fresh:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"已追加 {len(fresh)} 条到 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
