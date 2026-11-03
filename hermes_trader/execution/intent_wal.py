"""P3 下单意图 WAL（write-ahead log）+ 崩溃恢复扫描。

融合文档偏离⑤：下单意图在进程崩溃窗口内不可恢复。本模块在每次订单状态
迁移时向 `${HERMES_DATA_DIR:-/data}/order-intent-wal.jsonl` 追加一条记录，
使崩溃重启后能重建"哪些 cloid 处于未决状态（INTENT/SUBMITTED/UNKNOWN）"。

WAL 是 append-only JSONL，每行一条状态迁移：
    {"cloid":..., "coin":..., "from":..., "to":..., "ts":..., ...}
首行（INTENT 记录）额外携带完整上下文字段（side/is_buy/size/limit_px/mid_px/
mode/analysis_id），供崩溃恢复时重建 OrderIntent 并对账。

崩溃恢复：:func:`load_pending` 全量扫描 WAL，按 cloid 重放迁移，返回所有
处于未决状态的 OrderIntent。调用方（executor 启动路径）对每个未决 intent 调
reconcile_order_fill 决定是补登成交还是标记失败。

写盘策略：追加 + flush + os.fsync（意图 WAL 是资金安全关键路径，宁可慢
不可丢）。单进程内用模块锁串行化追加；跨进程由 executor 的 _ENTRY_LOCK
保证同一时刻只有一个下单者，故 WAL 写不会交错。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from .order_fsm import OrderIntent, OrderState, PENDING

_DATA_DIR = Path(os.environ.get("HERMES_DATA_DIR", "/data"))
DEFAULT_WAL = _DATA_DIR / "order-intent-wal.jsonl"

_LOCK = threading.Lock()
_WAL_PATH: Path = DEFAULT_WAL


def set_wal_path(path: os.PathLike | str) -> None:
    """测试/备用部署重定向 WAL 位置。"""
    global _WAL_PATH
    _WAL_PATH = Path(path)


def wal_path() -> Path:
    return _WAL_PATH


def _append(rec: dict, path: os.PathLike | str | None = None) -> None:
    # path 显式传入时写到该路径（恢复/演练用）；否则写模块全局 WAL。
    # 不在这里改全局 _WAL_PATH——恢复一个旁路 WAL 时绝不能把运行中实盘
    # intent 的后续写入也重定向走（参数穿透，而非 set_wal_path 全局切换）。
    target = Path(path) if path is not None else _WAL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False)
    with _LOCK:
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def record_intent(intent: OrderIntent,
                  path: os.PathLike | str | None = None) -> None:
    """下单前：写入 INTENT 记录（含完整上下文）。必须在 place_hl_order 之前。"""
    rec = intent.to_dict()
    rec["to"] = OrderState.INTENT.value
    rec["from"] = None
    rec["ts"] = intent.created_ms
    _append(rec, path)


def record_transition(intent: OrderIntent, rec: dict,
                      path: os.PathLike | str | None = None) -> None:
    """状态迁移后：追加迁移记录。rec 来自 OrderIntent.transition 返回值。"""
    _append(rec, path)


def _replay(path: Path) -> dict[str, OrderIntent]:
    """按 cloid 重放 WAL，重建每个 intent 的最新状态。"""
    intents: dict[str, OrderIntent] = {}
    if not path.exists():
        return intents
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            cloid = rec.get("cloid")
            if not cloid:
                continue
            to = rec.get("to")
            if cloid not in intents:
                # 首条记录（INTENT）携带完整上下文；若 WAL 被截断首条缺失，
                # 用最小字段占位，状态仍能被后续迁移推进。
                try:
                    intents[cloid] = OrderIntent.from_dict(rec)
                except Exception:
                    continue
            it = intents[cloid]
            if to and to != it.state.value:
                try:
                    it.state = OrderState(to)
                    it.updated_ms = int(rec.get("ts", it.updated_ms))
                    for k in ("order_id", "avg_px", "total_sz"):
                        if rec.get(k) is not None:
                            setattr(it, k, rec[k])
                    if rec.get("reason"):
                        it.reason = rec["reason"]
                except ValueError:
                    continue
    return intents


def load_pending(path: Optional[os.PathLike | str] = None) -> list[OrderIntent]:
    """崩溃恢复：返回 WAL 中所有处于未决状态的 intent（按创建时间升序）。"""
    p = Path(path) if path else _WAL_PATH
    intents = _replay(p)
    pending = [it for it in intents.values() if it.state in PENDING]
    pending.sort(key=lambda it: it.created_ms)
    return pending


def load_all(path: Optional[os.PathLike | str] = None) -> dict[str, OrderIntent]:
    """返回 WAL 中全部 intent（含终态），供审计/对账。"""
    p = Path(path) if path else _WAL_PATH
    return _replay(p)
