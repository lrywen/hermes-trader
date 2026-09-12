"""pullback-long 旁路的跨进程评估心跳（M17 判活，2026-09-12）。

Why this exists
---------------
pullback 影子流只在「全部合取条件成立」时才往 pullback_shadow.jsonl 落一条
（side=long + 4h uptrend + macro=up + slow_burn>=2 + score>=30 +
非 fresh_impulse + RSI<65 + extension<2ATR），是极稀疏的事件型信号。宏观 regime
翻回 up 之后的过渡期，即使写路径完全健康，也可能数十小时没有合格候选 —— 仅用
事件流「24h 0 条」无法区分「闸门/写路径坏了」与「在跑但无合格候选」。

与 market_circuit_state 相同的跨进程模式：trading-loop 是唯一写者，每评估一次
旁路候选就原子重写一个小状态文件；评级器/web 只读 ts 判活。只有 /data 在两种
部署拓扑下都共享，默认路径放这里，可用 HERMES_PULLBACK_GATE_STATE_FILE 覆盖。

Contract
--------
* ``record_evaluation`` best-effort，绝不抛异常（心跳失败绝不能扰动交易循环）。
* 只证明「旁路评估路径在跑」，不改变任何下单/拦截决策（INERT 可观测性）。
* ``read_state`` best-effort，绝不抛异常；文件缺失/损坏/版本不支持返回 None。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from hermes_trader.agents.atomic_io import write_json_atomic

logger = logging.getLogger(__name__)

STATE_FILE = os.environ.get(
    "HERMES_PULLBACK_GATE_STATE_FILE", "/data/.pullback-gate.state"
)
_STATE_VERSION = 1


def record_evaluation(*,
                      coin: str,
                      macro_regime: str = "",
                      macro_up: bool = False,
                      score: float = 0.0,
                      slow_count: int = 0,
                      rsi4h: Optional[float] = None,
                      extension_atr: Optional[float] = None,
                      uptrend: bool = False,
                      admitted: bool = False,
                      path: Optional[str] = None) -> None:
    """Rewrite the heartbeat state file after one pullback-bypass evaluation.

    Called whenever the runner gate reaches the pullback-long bypass block for
    a non-structured long candidate (i.e. the base/overlay switches are on).
    The macro gate is evaluated in the same block, so the last non-firing reason
    is carried for diagnostics. Best-effort: any error is debug-logged.
    """
    target = path or STATE_FILE
    try:
        payload = {
            "version": _STATE_VERSION,
            "ts": time.time(),
            "coin": str(coin or "")[:32],
            "macro_regime": str(macro_regime or "")[:16],
            "macro_up": bool(macro_up),
            "score": round(float(score or 0.0), 4),
            "slow_count": int(slow_count or 0),
            "rsi4h": (round(float(rsi4h), 2)
                      if rsi4h is not None else None),
            "extension_atr": (round(float(extension_atr), 3)
                              if extension_atr is not None else None),
            "uptrend": bool(uptrend),
            "admitted": bool(admitted),
        }
        # Cheap, fully regenerable: atomic rename (no torn reads), no fsync —
        # same contract as market_circuit_state.
        write_json_atomic(target, payload, indent=None, fsync=False)
    except Exception as e:  # never perturb the trading loop
        logger.debug("[pullback_gate_state] heartbeat write failed: %s", e)


def read_state(path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the latest heartbeat payload, or ``None`` if unavailable.

    Never raises: missing file, corrupt JSON, non-dict content, or an
    unsupported future version all degrade to ``None`` so the grader applies
    its own absence/staleness handling.
    """
    target = path or STATE_FILE
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if int(data.get("version", 0)) > _STATE_VERSION:
            return None
        return data
    except (FileNotFoundError, ValueError, OSError, TypeError):
        return None
