"""危险方向（fail-open）配置漂移检测 —— 审查闭环的「防复发」一半。

前五轮复核都在**检测** canonical↔live 配置漂移，但只靠人工每轮跑，导致一个
反复出现的问题：修复动作自身（或容器外改配置）会引入下一轮才被人工发现的新
漂移（「修复自噬」）。本模块把「危险方向判据」固化为可在测试中调用的纯函数：

  对每个 risk-relevant 叶子，给定 canonical 值与 live 值，判定 canonical 是否
  比 live **更宽松**（即配置丢键深合并回落到 canonical 时会放宽保护）。判据不是
  通用数值大小（无法知道哪边危险），而是按叶子登记的语义：

    * ``"hi_danger"``：值越大越宽松（如止损宽度、notional 上限）；
    * ``"lo_danger"``：值越小越宽松（如置信度门槛、保证金下限）；
    * ``"armed_bool"``：bool 为 True 时代表「武装某放宽动作」（如 boost），
      canonical True 而 live False 即危险方向。

  仅登记「放宽开仓/削弱保护」方向；收紧方向（canonical 更保守）不算危险，故不
  在此表。未登记的叶子默认不判危险（惰性/中性/路径类）。

``find_dangerous_divergences`` 返回每个危险漂移的 {leaf, canonical, live}。
配套测试断言其为空：以后任何引入新危险方向的改动当次即红，不再需要人工复核
兜底。
"""
from __future__ import annotations

from collections.abc import Mapping

# 叶子危险语义表。仅登记「canonical 可能比 live 宽松」的 risk-relevant 叶。
# 已对齐 live 的叶子保留在表中，以防止未来回归（不是因为当前不一致）。
LEAF_DANGER_KIND: dict[str, str] = {
    # ── 已收口（保持监控，防回归）──
    "max_total_notional_pct": "hi_danger",
    "min_available_margin_pct": "lo_danger",
    "daily_giveback_min_peak_usd": "lo_danger",
    "daily_giveback_halt_pct": "hi_danger",
    "counter_regime_min_conf": "lo_danger",
    "max_crypto_long_correlated": "hi_danger",
    "min_history_bars": "lo_danger",
    "chop_min_conf": "lo_danger",
    "chop_min_score": "lo_danger",
    "own_gap_demote_pct": "lo_danger",
    "runner_entry_gate.bypass_sidestep_overrides": "armed_bool",
    "runner_entry_gate.min_composite": "lo_danger",
    "runner_entry_gate.min_short_composite": "lo_danger",
    "runner_entry_gate.mover_min_composite": "lo_danger",
    "runner_entry_gate.pullback_long.min_composite": "lo_danger",
    "runner_entry_gate.pullback_long.max_rsi": "hi_danger",
    "runner_entry_gate.pullback_long.min_slow_burn": "lo_danger",
    "runner_entry_gate.breakout_score_floor.min_composite": "lo_danger",
    "momentum_continuation.max_pullback_pct": "hi_danger",
    "trigger_thresholds.breakout_min_rvol": "lo_danger",
    "trigger_thresholds.breakout_confirm_bars": "lo_danger",
    "dsl_exit.regime_aware.trend_ride.protect_pct": "lo_danger",
    "dsl_exit.regime_aware.trend_ride.retrace_threshold": "lo_danger",
    "signal_enforcement.boost": "armed_bool",
    "trend_filter_200ma.mode": "armed_mode",
    # market_circuit / daily_extension_cap 有意 canonical=shadow（新部署惰性），
    # 生产 /data 切 enforce；其丢键保护由 config_store._production_enforce_arm_errors
    # 在权威源上 fail-closed，故不纳入此「canonical 必须不弱于 live」断言。
}

# 对 mode 三态叶子，按「保护强度」排序：canonical 严格弱于 live 即危险方向。
_MODE_RANK = {"off": 0, "shadow": 1, "enforce": 2}


def flatten_config(d: Mapping, prefix: str = "") -> dict:
    """Flatten nested dict 到 dotted-key → leaf（非 dict 值）。"""
    out: dict = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping):
            out.update(flatten_config(v, path))
        else:
            out[path] = v
    return out


def _is_dangerous(kind: str, canonical, live) -> bool:
    try:
        if kind == "hi_danger":
            return float(canonical) > float(live)
        if kind == "lo_danger":
            return float(canonical) < float(live)
        if kind == "armed_bool":
            return bool(canonical) and not bool(live)
        if kind == "armed_mode":
            return _MODE_RANK.get(str(canonical), -1) < _MODE_RANK.get(str(live), -1)
    except (TypeError, ValueError):
        # 类型不可比时不判危险（schema 校验在别处负责类型错误）。
        return False
    return False


def find_dangerous_divergences(canonical: Mapping, live: Mapping) -> list[dict]:
    """返回所有「canonical 比 live 宽松」的危险方向漂移。"""
    fc = flatten_config(canonical)
    fl = flatten_config(live)
    findings: list[dict] = []
    for leaf, kind in LEAF_DANGER_KIND.items():
        if leaf in fc and leaf in fl and fc[leaf] != fl[leaf]:
            if _is_dangerous(kind, fc[leaf], fl[leaf]):
                findings.append({"leaf": leaf, "canonical": fc[leaf], "live": fl[leaf]})
    return findings
