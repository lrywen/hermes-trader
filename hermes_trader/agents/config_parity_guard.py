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
    #   weak_trend_chase    : canon 0.0 < live 25.0（无 ADX 门槛=更宽松）
    #   equity_fraction     : canon 0.2 > live 0.15（单笔仓位更大=更宽松）
    # 注：runner_entry_gate.min_confidence 曾登记为 canon 0.6 < live 0.7，但
    # 2026-10-19 P0-1 已把生产门槛恢复到 canonical 0.6（快照去毒），该叶不再有
    # 漂移，登记同步移除；将来若生产再次调门槛，应作为新决策重新登记。
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
    # canon 0.0 < live 25.0 —— 生产启用 4h ADX 弱趋势追单否决（注释建议 25 需配对回测后才 promote），
    # canonical 保留 OFF 宽松默认。
    "runner_entry_gate.weak_trend_chase.min_adx4h",
    # canon 0.2 > live 0.15 —— 生产降低单笔仓位占比，canonical 保留旧宽松默认。
    "equity_fraction_per_trade",
})


# recheck 2026-10-18 §5.1 / fix-verification 2026-10-18 §11③: 门禁「有效覆盖为 0」
# 与「三代假绿」的补强。
#
# find_dangerous_divergences 只遍历 LEAF_DANGER_KIND（登记表），而真实生产漂移
# 大多不在登记表里（当前 56 处漂移 vs 28 登记，经白名单后交集为 0），导致门禁
# 永远返回 []、三层信号同时「健康」——但未登记的漂移（含可能的危险 fail-open）
# 却毫无感知。这是比「假阴性」更坏的状态：有绿灯就没有人会怀疑。
#
# 修复：门禁判据从「已登记内无危险」改为「漂移 ⊆ 登记 且值与登记一致」。
# 所有实际漂移必须显式归属三类之一，且值必须与复核当时一致，否则当次即红：
#   1. LEAF_DANGER_KIND（危险方向）
#   2. _SANCTIONED_LOOSER_LEAVES（已知有意 canonical 宽松）
#   3. _REVIEWED_SAFE_DIVERGENCES（已人工复核为 fail-safe / 无风险的生产漂移，
#      且登记复核当时的 (canonical, live) 值）
#
# 值绑定（fix-verification §11③）：_REVIEWED_SAFE_DIVERGENCES 从「只按叶名」改为
# 记录复核当时的 (canonical, live)。这样 `max_daily_loss_usd` 从 -3 改成 -500 会
# 因为 live != 登记值 -3 而立刻变红；新增 live-only 叶也会报。只有 operator 显式
# 重新审查并更新登记值后门禁才会重新变绿——这正是防复发的根治，消除「三代假绿」。
#
# 值来自 2026-10-18 对生产 /home/ldy/hermes-deploy/.agent-config.json 的逐一审阅：
# 全部为 fail-safe 方向（canonical 更严/更保守）或无风险（shadow 路径/cooldown/
# 模式切换/权重/测试余额），无新的危险 fail-open。
_REVIEWED_SAFE_DIVERGENCES: dict[str, tuple[object, object]] = {
    # 元数据/账户
    "_comment": ("", "10 USDC 实盘 · 10倍杠杆 · BTC/ETH增强版 | 选择性开启辅助功能"),
    "mode": ("OFF", "SHADOW"),
    "coin_blocklist": (["TON", "TRX"], []),
    "shadow_book.starting_balance": (10000.0, 20),
    # shadow 观测流路径与模式开关（关闭/路径变更不构成放宽资金保护）
    "daily_extension_cap.mode": ("shadow", "enforce"),
    "daily_extension_cap.shadow_log_path": ("", "/data/daily_extension_cap_shadow.jsonl"),
    "reentry_cap.mode": ("off", "shadow"),
    "reentry_cap.shadow_log_path": ("", "/data/reentry_cap_shadow.jsonl"),
    "signal_age_decay.mode": ("off", "shadow"),
    "signal_age_decay.shadow_log_path": ("", "/data/signal_age_decay_shadow.jsonl"),
    "xs_reversal.mode": ("off", "shadow"),
    "xs_reversal.shadow_log_path": ("", "/data/xs_reversal_shadow.jsonl"),
    "market_circuit.mode": ("shadow", "enforce"),
    "regime_risk_overlay.shadow_log_path": ("", "/data/regime_overlay_shadow.jsonl"),
    "trend_filter_200ma.shadow_log_path": ("", "/data/trend_filter_shadow.jsonl"),
    "weight_shadow.shadow_mode": (False, True),
    "dsl_exit.stop_confirm_shadow.shadow_mode": (False, True),
    "dsl_exit.stop_tuning_shadow.candidate_breakeven_trigger_pct": (0.0, 1),
    "dsl_exit.stop_tuning_shadow.candidate_max_loss_pct": (0.0, 1.5),
    "runner_entry_gate.breakout_score_floor.shadow_mode": (False, True),
    "runner_entry_gate.per_coin_cooldown.shadow_mode": (True, False),
    "runner_entry_gate.pullback_long.shadow_mode": (False, True),
    # 生产功能开关（live 开启，canonical 关闭；开启更激进，但非保护放宽）
    "candlestick_patterns.enabled": (False, True),
    "debate_research.enabled": (False, True),
    "enable_hip3": (False, True),
    "launch_capture.trades_capture": (False, True),
    "loop_runtime.scan_dynamic": (False, True),
    "loop_runtime.ws_fill_wake": (False, True),
    "loop_runtime.ws_status_event": (False, True),
    "regime_risk_overlay.enabled": (False, True),
    "runner_entry_gate.pullback_long.enabled": (False, True),
    # P0-2 (2026-10-19)：long-only 研究节流。live=true 让 loop 在付费 LLM research
    # 前砍掉纯空头候选（allow_shorts=false 时反正会被末端闸门拒），只省 token/降噪，
    # 不放宽任何资金保护；terminal runner_entry_gate.allow_shorts 仍是权威空头开关。
    "scan_long_only": (False, True),
    # 生产节奏/超时/退避（非保护语义）
    "held_research_interval_min": (10, 20),
    "research_cooldown_min": (3, 10),
    "research_llm.backoff_cap_sec": (15.0, 4),
    "crypto_whale.http_timeout_s": (2.5, 5),
    "news_catalyst.http_timeout_s": (3.0, 8),
    "research_rescore_delta": (0.0, 8),
    "scan_budget.parallel_workers": (32, 24),
    # HIP-3 audit 2026-10-19：canonical movers floor 50k→1M（执行层流动性门仍是
    # 5M），修复 100x scan-vs-execute 预算错配。live 暂留 50k（旧值），登记为
    # fail-safe 临时漂移——live 更宽只会多喂低流动候选（末端被风控门拒），不放宽
    # 任何资金保护；部署后经审计通道把 live 抬到 1M，届时此登记可删（两侧相等）。
    "scan_budget.hip3_movers_floor_usd": (1_000_000.0, 50_000),
    # P0-a (2026-10-20)：canonical universe_sweep 0→8 开启全宇宙轮转覆盖
    # （在 max_markets=60 内从 crypto-by-volume 残额切槽，不追加、不碰限流）。
    # live 暂留 0（旧值，关闭轮转）——这只是"暂不扩大扫描视野"，不放宽任何资金/
    # 流动性保护，反而是更保守的少扫。部署后经审计把 live 置 8，届时此登记可删。
    "scan_budget.universe_sweep": (8, 0),
    # 生产调优权重/阈值（canonical 更严或非保护）
    "trigger_weights.trend_strength": (0.3, 0.55),
    "trigger_weights.uptrend_momentum": (0.2, 0),
    "momentum_continuation.min_trend_pct": (8.0, 5),
    "regime_classifier.chop_adx_max": (20.0, 25),
    "regime_classifier.fast_ema": (20, 15),
    "regime_classifier.slow_ema": (30, 21),
    "regime_risk_overlay.chop.allow_shorts": (False, True),
    "runner_entry_gate.trend_rsi_overbought": (80.0, 82.0),
    "analyst_scoring.analyst5_whale_or_conf": (0.6, 0.62),
    "trigger_thresholds.momentum_lookback": (3, 2),
    "trigger_thresholds.momentum_pct": (3.0, 4),
    # 出场/风控生产值（canonical 比 live 严 = fail-safe 方向）
    "dsl_exit.max_loss_pct": (1.0, 1.5),
    "max_daily_loss_usd": (-2.0, -3),
    "tp_scale_fraction": (0.5, 0.4),
    "debate_research.use_structured_output": (True, False),
    # recheck-4 P0-C：live-only 叶（canonical 无此键，canonical 侧为 None）。
    # HYPE 币级 ATR 止损 override，为生产新增的 per-coin 收紧，无风险内容。值绑定
    # (None, live) 使「新增未被登记的 live-only 叶」或「这些叶值被改」都会变红。
    "coin_overrides.HYPE.dsl_exit.atr_stop.ceiling_pct": (None, 3),
    "coin_overrides.HYPE.dsl_exit.atr_stop.floor_pct": (None, 1.5),
}


def _covered_by_review(leaf: str, canon: object, live: object) -> bool:
    """判定一个漂移叶是否「已复核安全」——叶名已登记且 (canonical, live) 与复核
    当时一致。值不一致（如 max_daily_loss_usd 从 -3 改 -500）即视为未复核 → 红。
    """
    reg = _REVIEWED_SAFE_DIVERGENCES.get(leaf)
    if reg is None:
        return False
    return _values_equal(reg[0], canon) and _values_equal(reg[1], live)


def uncovered_divergences(canonical: Mapping[str, object],
                          live: Mapping[str, object]) -> list[str]:
    """返回所有「实际漂移但未登记归属 / 值已漂移」的叶——门禁覆盖盲区。

    判据（recheck §9#3 + fix-verification §11③）：「漂移 ⊆ 登记 且值与登记一致」。
    凡 canonical↔live 不一致、且既不在 LEAF_DANGER_KIND（危险）、也不在
    _SANCTIONED_LOOSER_LEAVES（有意宽松）、也不在 _REVIEWED_SAFE_DIVERGENCES
    （已复核安全，但值必须与登记一致）的漂移，都会被返回。值绑定保证：即使叶名
    已登记为「已复核安全」，只要 live（或 canonical）值变了（如 max_daily_loss_usd
    从 -3 改成 -500），也会被当作未复核而返回。门禁断言它为空；非空即代表存在
    未审查的新漂移或已审查值漂移，必须显式登记/更新后才绿。
    """
    fc = flatten_config(canonical)
    fl = flatten_config(live)
    # recheck-4 P0-C：原 `diff = {k for k in fc if k in fl and fc[k] != fl[k]}`
    # 只遍历 canonical 的键且要求键在 live 里存在——只存在于 live 的新叶（如新增的
    # 风险块）永远不会进入判据，是永久盲区。现改为 set(fc) | set(fl) 同时看两边，
    # 并区分：
    #   - canonical-only（live 未覆盖，沿用 canonical 默认）→ 非「live 新增」，不报；
    #   - live-only / 值漂移 → 纳入检查。
    all_keys = set(fc) | set(fl)
    uncovered: list[str] = []
    for leaf in sorted(all_keys):
        if leaf in _SANCTIONED_LOOSER_LEAVES:
            continue  # 已知有意 canonical 宽松，豁免
        in_c = leaf in fc
        in_l = leaf in fl
        if in_c and not in_l:
            continue  # canonical-only：live 沿用 canonical 默认，不构成 live 新增
        if in_c and in_l and _values_equal(fc[leaf], fl[leaf]):
            continue  # 无漂移（元组/列表语义相同不算漂移）
        if leaf in LEAF_DANGER_KIND:
            kind = LEAF_DANGER_KIND[leaf]
            # recheck-4 P0-B：危险叶在此只补「live 比 canonical 宽松」方向。
            # find_dangerous_divergences 只查 canonical 宽松（丢键回退）方向；数值
            # 保护限制被 live 放宽（如 max_total_notional_pct 2→100）此前完全不可见。
            # 仅对 hi/lo_danger 数值限制生效；armed_bool/mode 无「live 放宽」语义，
            # 由 find_dangerous_divergences 全权覆盖其唯一方向。
            if kind in ("hi_danger", "lo_danger"):
                if _is_dangerous(kind, fl.get(leaf), fc.get(leaf)):
                    uncovered.append(leaf)
            continue
        if not _covered_by_review(leaf, fc.get(leaf), fl.get(leaf)):
            uncovered.append(leaf)
    return sorted(uncovered)


def _normalize_for_compare(v: object) -> object:
    """归一化用于值比较：list/tuple 视为等价序列（逐元素归一化），其余原样返回。

    这样「元组 vs 列表」这类仅容器类型不同、语义相同的差异不会被误判为漂移。
    """
    if isinstance(v, (list, tuple)):
        return [_normalize_for_compare(x) for x in v]
    return v


def _values_equal(a: object, b: object) -> bool:
    """序列归一化后的相等判断（元组列表语义相同视为相等）。"""
    return _normalize_for_compare(a) == _normalize_for_compare(b)


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
        if leaf in fc and leaf in fl and not _values_equal(fc[leaf], fl[leaf]):
            if _is_dangerous(kind, fc[leaf], fl[leaf]):
                findings.append({"leaf": leaf, "canonical": fc[leaf], "live": fl[leaf]})
    return findings
