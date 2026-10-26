"""P0 regression (Audit 2026-10-26): microstructure debug 端点。

旧实现三连缺陷：无鉴权、在 ``async def`` 内同步读全天 ~1.6GB JSONL 阻塞
事件循环、无条件为每条成交建 tuple 把 server 顶到 cgroup 内存上限。修复后：

  * 两端点挂载 ``require_operator_or_loopback``（远端无 token → 401）；
  * 阻塞聚合走 ``asyncio.to_thread``，逐行流式 O(1) 聚合（除有界 series
    尾部外不驻留成交），include_series=False 时不返回任何序列点；
  * days 上限收紧到 2（le=2）。
"""
import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader.dashboard_routes import microstructure_debug as msd
from hermes_trader.dashboard import require_operator_or_loopback

_OP_TOKEN = "test-op-secret-p0-msd"


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    raw = tmp_path / "trades-raw"
    raw.mkdir()
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    return raw


def _write_day(raw, coin, rows, *, day=None):
    import datetime
    day = day or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    d = raw / f"date={day}"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{coin}.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture()
def client(data_dir, monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    app = FastAPI()
    msd.register_microstructure_debug_routes(app)
    return TestClient(app, raise_server_exceptions=False)


def test_routes_have_loopback_or_operator_gate():
    app = FastAPI()
    msd.register_microstructure_debug_routes(app)
    for route in app.routes:
        if "debug/microstructure" in getattr(route, "path", ""):
            dep_names = [d.call.__name__ for d in route.dependant.dependencies]
            assert "require_operator_or_loopback" in dep_names, route.path


def test_streaming_aggregator_is_constant_memory(data_dir):
    now = time.time() * 1000.0
    rows = [
        {"coin": "BTC", "side": "B", "px": 100, "sz": 2, "time": now - 3000},
        {"coin": "BTC", "side": "A", "px": 100, "sz": 1, "time": now - 2000},
        {"coin": "BTC", "side": "B", "px": 100, "sz": 3, "time": now - 1000},
    ]
    _write_day(data_dir, "BTC", rows)
    agg = msd._aggregate_coin("BTC", 1, now_ms=now, series_limit=0)
    assert agg is not None
    assert agg["cvd_points"] == 3
    assert agg["cvd_last"] == 4.0  # +2 -1 +3
    assert agg["cvd_series_tail"] == []  # include_series=False → 空序列
    # 有界尾部：只要最近 N 个点
    bounded = msd._aggregate_coin("BTC", 1, now_ms=now, series_limit=2)
    assert len(bounded["cvd_series_tail"]) == 2
    assert bounded["cvd_series_tail"][-1][1] == 4.0
    # 无落盘 → None（HTTP 层 404）
    assert msd._aggregate_coin("XXX", 1, now_ms=now, series_limit=0) is None


def test_overview_loopback_allowed_and_no_series(client, data_dir):
    now = time.time() * 1000.0
    _write_day(data_dir, "BTC", [
        {"coin": "BTC", "side": "B", "px": 1, "sz": 1, "time": now - 500}])
    # TestClient 的 peer 是 loopback → 无需 token 即放行
    r = client.get("/api/dashboard/debug/microstructure")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tracked_coins"] == 1
    assert body["coins"][0]["cvd_series_tail"] == []


def test_days_above_cap_rejected(client):
    r = client.get("/api/dashboard/debug/microstructure?days=3")
    assert r.status_code == 422


def test_missing_coin_returns_404(client):
    r = client.get("/api/dashboard/debug/microstructure/GHOST")
    assert r.status_code == 404


def test_concurrent_scan_short_circuits_to_503():
    """锁被占用时，重负载请求快速返回 503（Retry-After），不排队堆叠线程。"""
    import asyncio

    async def scenario():
        assert msd._scan_in_progress_response() is None  # 空闲
        async with msd._SCAN_LOCK:
            busy = msd._scan_in_progress_response()
            assert busy is not None
            assert busy.status_code == 503
            assert busy.headers.get("retry-after") == "30"
        assert msd._scan_in_progress_response() is None  # 释放后恢复

    asyncio.run(scenario())


class _FakeRequest:
    """仅承载鉴权依赖读取的字段：无凭据 + 远端私网 peer。"""

    def __init__(self, peer_host):
        self._peer = peer_host
        self.headers = {}

    @property
    def client(self):
        host = self._peer
        return type("C", (), {"host": host})()


def test_remote_non_loopback_without_token_rejected(monkeypatch):
    # 经 nginx 反代时 socket peer 是其私网网桥 IP；无 token 必须 401，
    # 不能像 LAN 宽放规则那样放行（否则 viewer 即可触发重负载端点）。
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    with pytest.raises(Exception) as ei:
        require_operator_or_loopback(_FakeRequest("172.18.0.5"))
    assert getattr(ei.value, "status_code", None) == 401


def test_loopback_without_token_allowed(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    assert require_operator_or_loopback(_FakeRequest("127.0.0.1")) is None
