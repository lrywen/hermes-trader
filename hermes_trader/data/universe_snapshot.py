"""Point-in-time universe 快照层（横截面去幸存者偏差）。

live ``client.universe.get_universe`` 只返回*当前*流动市场（含每个币的
funding / openInterest / dayNtlVlm），TTL 120s。直接拿"现在的币池"回测历史
会同时引入两种偏差：

* **幸存者偏差**——已退市的币不在当前 universe 里，回测凭空少了它们的成交；
* **前视偏差**——funding/OI 是选币打分（``agents.coin_select``）的输入，若用
  现在的值推断历史时刻，等价于把未来信息泄露进当时的决策。

本层把每次取到的 universe 按时间戳 append-only 落盘，回测/研究再通过
:func:`snapshot_as_of` 取回"在指定时刻已经可见的最近一版"，从而让币池与
funding/OI 都遵守 point-in-time 契约。与 ``historical_candles`` 同属只读数据
层，永不参与下单或实时决策；写入 best-effort，失败仅降级、不抛异常。

存储格式（按 UTC 天分文件，便于按日归档/清理）：
    ``<data_dir>/universe-snapshots/date=YYYY-MM-DD.jsonl``
每行一个快照：``{"ts": <epoch_ms>, "markets": [ {coin,type,dex,funding,
openInterest,dayNtlVlm,...}, ... ]}``。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DIRNAME = "universe-snapshots"
# 快照落盘的最小间隔（秒）。universe TTL 为 120s，默认同一 UTC 日内只在距上一
# 个已落盘快照超过该间隔时才追加，避免每轮扫描都重复写入几乎相同的一版。
_MIN_INTERVAL_S = float(os.environ.get("HERMES_UNIVERSE_SNAPSHOT_MIN_INTERVAL_S", "300"))


def _snapshot_dir(data_dir: Optional[str] = None) -> Path:
    base = data_dir or os.environ.get("HERMES_DATA_DIR") or "/data"
    return Path(base) / _DIRNAME


def _file_for_ts(ts_ms: float, data_dir: Optional[str] = None) -> Path:
    day = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000.0))
    return _snapshot_dir(data_dir) / f"date={day}.jsonl"


def _compact_market(asset: dict[str, Any]) -> dict[str, Any]:
    """只保留回测币池/选币因子真正需要的字段，去掉内部诊断键，控制快照体积。"""
    keys = (
        "coin", "type", "dex", "maxLeverage", "szDecimals",
        "dayNtlVlm", "dayBaseVlm", "openInterest", "funding",
        "prevDayPx", "markPx", "midPx",
    )
    return {k: asset.get(k) for k in keys}


def record_universe_snapshot(
    markets: list[dict[str, Any]],
    *,
    ts_ms: Optional[float] = None,
    data_dir: Optional[str] = None,
    min_interval_s: Optional[float] = None,
) -> Optional[Path]:
    """把一版 universe append-only 落盘。best-effort，失败返回 None、不抛异常。

    Args:
        markets: ``get_universe`` 返回的市场列表（按成交额排序）。
        ts_ms: 快照时刻，默认当前 UTC（epoch ms）。
        data_dir: 覆盖数据目录（默认经 HERMES_DATA_DIR 解析）。
        min_interval_s: 距同一文件内上一条快照小于该间隔则跳过写入；None 用默认值。

    Returns 写入的文件路径；因间隔未写或失败时返回 None。
    """
    try:
        ts_ms = float(ts_ms if ts_ms is not None else time.time() * 1000.0)
        gap = _MIN_INTERVAL_S if min_interval_s is None else float(min_interval_s)
        path = _file_for_ts(ts_ms, data_dir)

        # 去重：读取当天文件最后一条的 ts，间隔不足则跳过。
        if path.exists() and gap > 0:
            last_ts: Optional[float] = None
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    tail = min(size, 8192)
                    f.seek(size - tail)
                    lines = f.read().decode("utf-8", "ignore").splitlines()
                for ln in reversed(lines):
                    ln = ln.strip()
                    if ln:
                        last_ts = json.loads(ln).get("ts")
                        break
            except Exception:
                last_ts = None
            if last_ts is not None and (ts_ms - float(last_ts)) / 1000.0 < gap:
                return None

        row = {
            "ts": ts_ms,
            "markets": [_compact_market(a) for a in markets],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # append-only：单行 JSON 一次性 write，OS 对常规管道写入保证原子性，
        # 并发读取不会看到半行。
        line = json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return path
    except Exception as e:  # 快照失败永不影响主循环
        logger.warning(f"[universe-snapshot] record skipped: {e}")
        return None


def snapshot_as_of(
    as_of_ms: float,
    *,
    data_dir: Optional[str] = None,
    max_lookback_days: int = 10,
) -> Optional[dict[str, Any]]:
    """返回在 ``as_of_ms`` 时刻已可见的最近一版快照（point-in-time）。

    从 as_of 当天起向前最多回查 ``max_lookback_days`` 个 UTC 日，在每个文件里
    取 ``ts <= as_of_ms`` 的最后一条。找不到返回 None。
    """
    for d in range(max_lookback_days + 1):
        # 用 epoch 反推日期（减 d 天），正确处理跨年。
        probe_ms = as_of_ms - d * 86400_000
        path = _file_for_ts(probe_ms, data_dir)
        if not path.exists():
            continue
        best: Optional[dict[str, Any]] = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    row = json.loads(ln)
                    if float(row.get("ts", 0)) <= as_of_ms:
                        best = row
        except Exception:
            best = None
        if best is not None:
            return best
    return None
