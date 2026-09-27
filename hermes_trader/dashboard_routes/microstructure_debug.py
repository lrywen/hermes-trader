"""只读 Microstructure debug 路由（基于 trades 落盘文件）。

架构背景：实时成交由 trading_loop 进程（perception + WS mids）订阅并写入其
**进程内** microstructure 单例；HTTP/dashboard 由 server 进程提供，二者内存
不共享，server 无法直接读到 trading_loop 的 CVD。本路由改为读取 WS 的公共
trades 原始落盘（trades_capture，JSONL），在 server 进程内重算，跨进程解耦。

落盘路径：
    ${HERMES_DATA_DIR:-/data}/trades-raw/date=YYYY-MM-DD/COIN.jsonl
每行一条原始成交：{coin, side(B=主动买/A=主动卖), px, sz, time(ms)}。

端点：
  * GET /api/dashboard/debug/microstructure        — 全部已落盘币概览
  * GET /api/dashboard/debug/microstructure/{coin} — 单币明细

只读红线（INERT）：只读取 JSONL，不写入、不重置、不下单；输出仅计数与归一化
数值，匿名安全，不含任何密钥或账户信息。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

_BURST_SEC = 60.0


def _raw_dir() -> str:
    return os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"), "trades-raw")


def _day_dirs(days: int) -> list[str]:
    """返回最近 days 天（含今天，UTC）的 date= 目录，最新在前。"""
    base = _raw_dir()
    out: list[str] = []
    now = int(time.time())
    for k in range(max(1, int(days))):
        day = datetime.fromtimestamp(now - k * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(base, f"date={day}")
        if os.path.isdir(path):
            out.append(path)
    return out


def _list_coins(days: int) -> list[str]:
    coins: set[str] = set()
    for d in _day_dirs(days):
        for name in os.listdir(d):
            if name.endswith(".jsonl"):
                coins.add(name[:-6].upper())
    return sorted(coins)


def _read_trades(coin: str, days: int) -> list[dict]:
    """按时间正序读取该币最近 days 天落盘成交。"""
    rows: list[dict] = []
    target = coin.upper() + ".jsonl"
    # 目录最新在前；反向遍历后整体反转，得到时间正序。
    for d in reversed(_day_dirs(days)):
        path = os.path.join(d, target)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # 容忍行尾撕裂，永不因此报错
    rows.sort(key=lambda t: float(t.get("time", 0.0)))
    return rows


def _derive(trades: list[dict], *, now_ms: float,
            series_limit: int) -> dict:
    """从落盘成交重算 CVD 点数/末值与近窗口 aggression。"""
    cvd = 0.0
    series: list[tuple[float, float]] = []
    net_burst = 0.0
    gross_burst = 0.0
    burst_cutoff = now_ms - _BURST_SEC * 1000.0
    for t in trades:
        size = float(t.get("sz", 0.0) or 0.0)
        if size <= 0:
            continue
        signed = size * (1.0 if t.get("side") == "B" else -1.0)
        cvd += signed
        ts_ms = float(t.get("time", 0.0))
        series.append((ts_ms, cvd))
        if ts_ms >= burst_cutoff:
            net_burst += signed
            gross_burst += abs(size)
    aggression = (net_burst / gross_burst) if gross_burst > 0 else None
    if aggression is not None:
        aggression = round(max(-1.0, min(1.0, aggression)), 4)
    last_ts = series[-1][0] if series else None
    return {
        "cvd_points": len(series),
        "cvd_last": round(cvd, 6),
        "aggression": aggression,
        "last_trade_ms": last_ts,
        "last_trade_age_s": round((now_ms - last_ts) / 1000.0, 2)
                            if last_ts is not None else None,
        "cvd_series_tail": series[-series_limit:] if series_limit > 0 else [],
    }


def register_microstructure_debug_routes(app: FastAPI) -> None:
    @app.get("/api/dashboard/debug/microstructure")
    async def microstructure_overview(
        days: int = Query(1, ge=1, le=30, description="扫描最近N天落盘"),
        include_series: bool = Query(False),
        series_limit: int = Query(20, ge=0, le=200),
    ) -> JSONResponse:
        """已落盘成交流/CVD 概览（只读、匿名安全）。"""
        now_ms = time.time() * 1000.0
        coins = _list_coins(days)
        items: list[dict] = []
        for coin in coins:
            trades = _read_trades(coin, days)
            items.append({"coin": coin, **_derive(
                trades, now_ms=now_ms,
                series_limit=series_limit if include_series else 0)})
        return JSONResponse({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "trades_capture_jsonl",
            "raw_dir": _raw_dir(),
            "days": days,
            "tracked_coins": len(items),
            "coins": items,
        })

    @app.get("/api/dashboard/debug/microstructure/{coin}")
    async def microstructure_coin(
        coin: str,
        days: int = Query(1, ge=1, le=30),
        series_limit: int = Query(100, ge=0, le=2000),
    ) -> JSONResponse:
        """单币落盘成交重算的 CVD/aggression 明细（只读、匿名安全）。"""
        trades = _read_trades(coin, days)
        if not trades:
            return JSONResponse(
                {"coin": coin, "tracked": False, "days": days,
                 "note": "最近时间窗内无该币落盘成交（未订阅或未开启trades_capture）"},
                status_code=404)
        return JSONResponse({
            "tracked": True, "coin": coin, "days": days,
            **_derive(trades, now_ms=time.time() * 1000.0,
                      series_limit=series_limit),
        })
