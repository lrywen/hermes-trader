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

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

_BURST_SEC = 60.0

# P0（2026-10-26）：单日落盘全站约 1.6GB，即便放 worker 线程，单次解析仍以
# 分钟计。用非阻塞锁串行化重负载扫描：已有扫描在跑时，新请求快速 503，而不是
# 在默认线程池里堆起多个全量解析任务把 CPU/IO 压满。
_SCAN_LOCK = asyncio.Lock()


def _scan_in_progress_response() -> "JSONResponse | None":
    if _SCAN_LOCK.locked():
        return JSONResponse(
            {"detail": "microstructure 扫描正在进行，请稍后重试（单次解析全站落盘，耗时较长）"},
            status_code=503,
            headers={"Retry-After": "30"},
        )
    return None


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


def _aggregate_coin(coin: str, days: int, *, now_ms: float,
                    series_limit: int) -> dict | None:
    """流式重算单币 CVD 点数/末值与近窗口 aggression。

    P0 修复（2026-10-26）：旧实现先把整天 JSONL（单日可达 ~20MB/币、全站
    1.6GB）全量 ``json.loads`` 进 list 再排序、并为每条成交建一个 tuple，
    单次 overview 即把 server 进程顶到 cgroup 内存上限并阻塞事件循环。这里
    改为逐行扫描、O(1) 聚合：除有界的 series 尾部外不驻留任何成交。

    返回 None 表示该币在窗口内无落盘成交（HTTP 层据此回 404）。
    """
    target = coin.upper() + ".jsonl"
    cvd = 0.0
    n_points = 0
    net_burst = 0.0
    gross_burst = 0.0
    burst_cutoff = now_ms - _BURST_SEC * 1000.0
    last_ts: float | None = None
    # 仅在调用方显式要 series 时维护有界尾部（环形覆盖），否则恒为空。
    tail: list[tuple[float, float]] = []
    keep_tail = series_limit > 0
    # 目录最新在前；反向遍历得到时间正序，便于尾部即最近成交。
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
                    t = json.loads(line)
                except ValueError:
                    continue  # 容忍行尾撕裂，永不因此报错
                size = float(t.get("sz", 0.0) or 0.0)
                if size <= 0:
                    continue
                signed = size * (1.0 if t.get("side") == "B" else -1.0)
                cvd += signed
                n_points += 1
                ts_ms = float(t.get("time", 0.0))
                last_ts = ts_ms
                if keep_tail:
                    tail.append((ts_ms, round(cvd, 6)))
                    if len(tail) > series_limit:
                        # 有界：丢弃最旧项，内存占用恒定为 series_limit。
                        tail.pop(0)
                if ts_ms >= burst_cutoff:
                    net_burst += signed
                    gross_burst += abs(size)
    if n_points == 0:
        return None
    aggression = (net_burst / gross_burst) if gross_burst > 0 else None
    if aggression is not None:
        aggression = round(max(-1.0, min(1.0, aggression)), 4)
    return {
        "cvd_points": n_points,
        "cvd_last": round(cvd, 6),
        "aggression": aggression,
        "last_trade_ms": last_ts,
        "last_trade_age_s": round((now_ms - last_ts) / 1000.0, 2)
                            if last_ts is not None else None,
        "cvd_series_tail": tail,
    }


def _overview(days: int, *, include_series: bool,
              series_limit: int) -> dict:
    """同步阻塞部分：在 worker 线程内逐币流式聚合，绝不入事件循环线程。"""
    now_ms = time.time() * 1000.0
    eff_limit = series_limit if include_series else 0
    items: list[dict] = []
    for coin in _list_coins(days):
        agg = _aggregate_coin(coin, days, now_ms=now_ms, series_limit=eff_limit)
        if agg is not None:
            items.append({"coin": coin, **agg})
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "trades_capture_jsonl",
        "raw_dir": _raw_dir(),
        "days": days,
        "tracked_coins": len(items),
        "coins": items,
    }


def register_microstructure_debug_routes(app: FastAPI) -> None:
    # P0（2026-10-26）：此端点会扫读全天落盘成交，曾在无鉴权 + 事件循环内
    # 同步读 1.6GB 时把整个 server HTTP 层顶死。鉴权采用 loopback-or-operator：
    # 容器内健康检查/本机诊断放行；任何经反代的远端调用必须带 operator token。
    # 与 /metrics 同级，比 LAN 宽放的 require_operator_or_internal 更严，因为
    # nginx 反代过来的 socket peer 是其私网网桥 IP，不能据此当"内网"信任。
    from hermes_trader.dashboard import require_operator_or_loopback

    @app.get(
        "/api/dashboard/debug/microstructure",
        dependencies=[Depends(require_operator_or_loopback)],
    )
    async def microstructure_overview(
        days: int = Query(1, ge=1, le=2, description="扫描最近N天落盘（上限2，防重负载）"),
        include_series: bool = Query(False),
        series_limit: int = Query(20, ge=0, le=200),
    ) -> JSONResponse:
        """已落盘成交流/CVD 概览（只读；阻塞 I/O 放 worker 线程，不堵事件循环）。"""
        busy = _scan_in_progress_response()
        if busy is not None:
            return busy
        async with _SCAN_LOCK:
            payload = await asyncio.to_thread(
                _overview, days, include_series=include_series,
                series_limit=series_limit)
        return JSONResponse(payload)

    @app.get(
        "/api/dashboard/debug/microstructure/{coin}",
        dependencies=[Depends(require_operator_or_loopback)],
    )
    async def microstructure_coin(
        coin: str,
        days: int = Query(1, ge=1, le=2),
        series_limit: int = Query(100, ge=0, le=2000),
    ) -> JSONResponse:
        """单币落盘成交重算的 CVD/aggression 明细（只读；I/O 放 worker 线程）。"""
        busy = _scan_in_progress_response()
        if busy is not None:
            return busy
        now_ms = time.time() * 1000.0
        async with _SCAN_LOCK:
            agg = await asyncio.to_thread(
                _aggregate_coin, coin, days,
                now_ms=now_ms, series_limit=series_limit)
        if agg is None:
            return JSONResponse(
                {"coin": coin, "tracked": False, "days": days,
                 "note": "最近时间窗内无该币落盘成交（未订阅或未开启trades_capture）"},
                status_code=404)
        return JSONResponse({"tracked": True, "coin": coin, "days": days, **agg})
