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
    # ── P0-1: 补登 2026-10-16 复核确认的危险方向叶 ──
    # canonical 比 live 宽松：配置丢键深合并回落到 canonical 会放宽保护。
    #   min_confidence      : canon 0.6 < live 0.7（门槛更低=更宽松）
    #   weak_trend_chase    : canon 0.0 < live 25.0（无 ADX 门槛=更宽松）
    #   equity_fraction     : canon 0.2 > live 0.15（单笔仓位更大=更宽松）
    "runner_entry_gate.min_confidence": "lo_danger",
    "runner_entry_gate.weak_trend_chase.min_adx4h": "lo_danger",
    "equity_fraction_per_trade": "hi_danger",
    # market_circuit / daily_extension_cap 有意 canonical=shadow（新部署惰性），
    # 生产 /data 切 enforce；其丢键保护由 config_store._production_enforce_arm_errors
    # 在权威源上 fail-closed，故不纳入此「canonical 必须不弱于 live」断言。
    # （signal_age_decay / reentry_cap / xs_reversal 为观测流，关闭观测不构成
    #   放宽资金保护，同样不在此表。）
}

# 对 mode 三态叶子，按「保护强度」排序：canonical 严格弱于 live 即危险方向。
_MODE_RANK = {"off": 0, "shadow": 1, "enforce": 2}

# P0-1: 已知且有意 canonical 比 live 宽松的危险叶（白名单）。
#
# 这些叶的 canonical 是「新部署的安全宽松默认」，live 是生产经人工调严并
# 复核过的值（canonical 相对 live 偏宽松 = 丢键会回落到较松值）。它们已被
# 登记进 LEAF_DANGER_KIND（保持可枚举可见），但属于「已知、有注释、经审查」
# 的漂移，find_dangerous_divergences 不将其计为「必须修复的新漂移」。
#
# 这与修复前的「假阴性」有本质区别：修复前这些叶根本不在登记表里，门禁对
# 它们毫无感知；现在它们被显式登记 + 显式豁免，任何「新增的、不在白名单」
# 的危险方向漂移仍会被立即报告（当次即红）。
_SANCTIONED_LOOSER_LEAVES: frozenset[str] = frozenset({
    # canon 0.6 < live 0.7 —— 生产提高置信度门槛（P1-5），canonical 保留旧宽松默认。
    "runner_entry_gate.min_confidence",
    # canon 0.0 < live 25.0 —— 生产启用 4h ADX 弱趋势追单否决（注释建议 25 需配对回测后才 promote），
    # canonical 保留 OFF 宽松默认。
    "runner_entry_gate.weak_trend_chase.min_adx4h",
    # canon 0.2 > live 0.15 —— 生产降低单笔仓位占比，canonical 保留旧宽松默认。
    "equity_fraction_per_trade",
})


# recheck 2026-10-18 §5.1: 门禁「有效覆盖为 0」的补强。
#
# find_dangerous_divergences 只遍历 LEAF_DANGER_KIND（登记表），而真实生产漂移
# 大多不在登记表里（当前 56 处漂移 vs 28 登记，经白名单后交集为 0），导致门禁
# 永远返回 []、三层信号同时「健康」——但未登记的漂移（含可能的危险 fail-open）
# 却毫无感知。这是比「假阴性」更坏的状态：有绿灯就没有人会怀疑。
#
# 修复：门禁判据从「已登记内无危险」改为「漂移 ⊆ 登记」。所有实际漂移必须显式
# 归属三类之一，否则当次即红：
#   1. LEAF_DANGER_KIND（危险方向）
#   2. _SANCTIONED_LOOSER_LEAVES（已知有意 canonical 宽松）
#   3. _REVIEWED_SAFE_DIVERGENCES（已人工复核为 fail-safe / 无风险的生产漂移）
#
# 当前生产 53 处未登记漂移经逐一审阅：全部为 fail-safe 方向（canonical 更严/更
# 保守）或无风险（shadow 路径/cooldown/模式切换/权重/测试余额），无新的危险
# fail-open。据此登记为「已复核安全」。任何未来新增、未登记、未复核的漂移都会
# 使 uncovered_divergences 非空 → 门禁红，迫使 operator 显式审查——这正是防复发。
_REVIEWED_SAFE_DIVERGENCES: frozenset[str] = frozenset({
    # 元数据/账户
    "_comment", "mode", "coin_blocklist", "shadow_book.starting_balance",
    # shadow 观测流路径与模式开关（关闭/路径变更不构成放宽资金保护）
    "daily_extension_cap.mode", "daily_extension_cap.shadow_log_path",
    "reentry_cap.mode", "reentry_cap.shadow_log_path",
    "signal_age_decay.mode", "signal_age_decay.shadow_log_path",
    "xs_reversal.mode", "xs_reversal.shadow_log_path",
    "market_circuit.mode", "regime_risk_overlay.shadow_log_path",
    "trend_filter_200ma.shadow_log_path", "weight_shadow.shadow_mode",
    "dsl_exit.stop_confirm_shadow.shadow_mode",
    "dsl_exit.stop_tuning_shadow.candidate_breakeven_trigger_pct",
    "dsl_exit.stop_tuning_shadow.candidate_max_loss_pct",
    "runner_entry_gate.breakout_score_floor.shadow_mode",
    "runner_entry_gate.per_coin_cooldown.shadow_mode",
    "runner_entry_gate.pullback_long.shadow_mode",
    # 生产功能开关（live 开启，canonical 关闭；开启更激进，但非保护放宽）
    "candlestick_patterns.enabled", "debate_research.enabled", "enable_hip3",
    "launch_capture.trades_capture", "loop_runtime.scan_dynamic",
    "loop_runtime.ws_fill_wake", "loop_runtime.ws_status_event",
    "regime_risk_overlay.enabled", "runner_entry_gate.pullback_long.enabled",
    # 生产节奏/超时/退避（非保护语义）
    "held_research_interval_min", "research_cooldown_min",
    "research_llm.backoff_cap_sec", "crypto_whale.http_timeout_s",
    "news_catalyst.http_timeout_s", "research_rescore_delta",
    "scan_budget.parallel_workers",
    # 生产调优权重/阈值（canonical 更严或非保护）
    "trigger_weights.trend_strength", "trigger_weights.uptrend_momentum",
    "momentum_continuation.min_trend_pct", "regime_classifier.chop_adx_max",
    "regime_classifier.fast_ema", "regime_classifier.slow_ema",
    "regime_risk_overlay.chop.allow_shorts", "runner_entry_gate.trend_rsi_overbought",
    "analyst_scoring.analyst5_whale_or_conf",
    "trigger_thresholds.momentum_lookback", "trigger_thresholds.momentum_pct",
    # 出场/风控生产值（canonical 比 live 严 = fail-safe 方向）
    "dsl_exit.max_loss_pct", "max_daily_loss_usd", "tp_scale_fraction",
    "debate_research.use_structured_output",
})


def uncovered_divergences(canonical: Mapping[str, object],
                          live: Mapping[str, object]) -> list[str]:
    """返回所有「实际漂移但未登记归属」的叶——门禁覆盖盲区。

    判据（recheck §9#3）：「漂移 ⊆ 登记」。凡 canonical↔live 不一致、且既不在
    LEAF_DANGER_KIND（危险）、也不在 _SANCTIONED_LOOSER_LEAVES（有意宽松）、
    也不在 _REVIEWED_SAFE_DIVERGENCES（已复核安全）的漂移，都会被返回。
    门禁断言它为空；非空即代表存在未审查的新漂移，必须显式登记后才会绿。
    """
    fc = flatten_config(canonical)
    fl = flatten_config(live)
    covered = (set(LEAF_DANGER_KIND) | _SANCTIONED_LOOSER_LEAVES
               | _REVIEWED_SAFE_DIVERGENCES)
    diff = {k for k in fc if k in fl and fc[k] != fl[k]}
    return sorted(diff - covered)


def flatten_config(d: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    """Flatten nested dict 到 dotted-key → leaf（非 dict 值）。"""
    out: dict[str, object] = {}
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


def find_dangerous_divergences(canonical: Mapping[str, object],
                               live: Mapping[str, object]) -> list[dict[str, object]]:
    """返回所有「canonical 比 live 宽松」的危险方向漂移。

    P0-1: 已显式登记且列入 _SANCTIONED_LOOSER_LEAVES（已知、有注释、经审查的
    有意漂移）的叶被豁免，不重复报警；其余登记叶若 canonical 宽松于 live 即返回。
    """
    fc = flatten_config(canonical)
    fl = flatten_config(live)
    findings: list[dict[str, object]] = []
    for leaf, kind in LEAF_DANGER_KIND.items():
        if leaf in _SANCTIONED_LOOSER_LEAVES:
            continue
        if leaf in fc and leaf in fl and fc[leaf] != fl[leaf]:
            if _is_dangerous(kind, fc[leaf], fl[leaf]):
                findings.append({"leaf": leaf, "canonical": fc[leaf], "live": fl[leaf]})
    return findings
