#!/usr/bin/env python3
"""backtest_majors_surge.py — _MAJOR_VOLUMES 8 主流币 × N 天 5m 逐 bar 全真回放。

三臂对照（纯纸面只读，HERMES_BACKTEST=1，dry-run 默认）：
  baseline : 现有 live 口径 —— composite ≥ minCompositeScore(54) 或
             burst/trend/pattern 旁路浮出水面，且过 late-entry  veto。
  #4       : sigma_burst_gate would-surface —— score ∈ [gate_override, min_score)
             且 pct_z ≥ 3.0 或 vol_z ≥ 5.0（live 为 shadow，这里按 enforce 回放），
             同样过方向推断 + late-entry。
  #7       : breakout_exemption would-downgrade —— 浮出水面但被 late-entry block，
             且 breakout fired 且 RVOL ≥ require_rvol(4.0)（live 为 shadow）。

回放保真口径（与线上 _scan_single_market / analyze_perception / dsl_exit 对齐）：
  * 触发器/旁路/打分全部 import 生产单源函数（triggers / perception /
    ta_filter / market_regime），配置经 trigger_weights_params /
    trigger_thresholds_params 从 live .agent-config.json 解析 —— 零复刻漂移。
  * 每根已收盘 5m bar：最近 100 根窗口跑 13 触发器 + squeeze 耦合 +
    composite（权重分母 = live 全量和）；1h 取决策时刻已收盘切片末 48 根；
    4h 取已收盘切片末 100 根（方向推断 extension 与 late_entry 共用，同 live）。
  * PIT 反前视：决策在 bar i 收盘，成交在 bar i+1 开盘价（加逆方向滑点）；
    高 TF 一律 _closed_slice 取已收盘前缀；出场峰值收盘后推进。
  * 出场 = live dsl_exit 核心阶梯：stale_flat(240min, peak<protect) →
    hard_timeout(600min) → max_loss(min(max_loss_pct, roe/lev)=1%) →
    phase2 tier 移动止损（{2,.35},{6,.3},{12,.2},{20,.15}，arm/选层基于 PEAK）
    + breakeven(2.5→0.3) clamp + monotonic floor。H-7 成本模型：
    来回费 5bps、entry 滑 5bps、exit 滑 15bps、max_loss 止损另加 10bps。
  * 每币每臂同一时刻至多一仓（无加仓），信号在持仓期间忽略。

不回放（报告 caveat）：H-5 wick guard/index 确认、noise_band 钩子、
  regime_aware 分层、smooth_transition、time_scratch、whale/dailyMover/
  momentum_continuation/age-decay（live 全关）、下游 LLM/executor 风险门。

用法：
  docker exec hermes-trader cat /data/.agent-config.json > /tmp/live-agent-config.json
  python scripts/backtest_majors_surge.py --config /tmp/live-agent-config.json \
      --days 180 [--source binance] [--coins BTC,ETH,...] [--write] [--out PATH]

注：HL candleSnapshot 的 5m 仅保留约 17 天（~5000 根）；90-180 天窗口须用
  --source binance（Binance 现货 K 线，1h/4h 亦同源保持口径一致）。
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 与 backtest.py 相同的进程护栏：在任何 hermes_trader import 之前标记回测，
# 使 exchange._make_exchange() 拒绝把 live mainnet 私钥载入模拟进程。
os.environ["HERMES_BACKTEST"] = "1"

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config import (  # noqa: E402
    trigger_thresholds_params,
    trigger_weights_params,
)
from hermes_trader.agents.market_regime import classify_candles  # noqa: E402
from hermes_trader.agents.perception import (  # noqa: E402
    _apply_squeeze_breakout_coupling,
    _sigma_burst_decision,
    extract_fired_triggers,
)
from hermes_trader.agents.ta_filter import (  # noqa: E402
    _extension_atr,
    _high_quality_breakout,
    late_entry_check,
)
from hermes_trader.data.historical_candles import fetch_candle_range  # noqa: E402
from hermes_trader.indicators import triggers as trig  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402

# executor.py:1144-1147 —— 主流币池（8 币）。
MAJORS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX"]

MS_5M = 5 * 60_000
MS_1H = 60 * 60_000
MS_4H = 4 * 3_600_000
MS_DAY = 24 * 3_600_000

# H-7 成本模型（与 scripts/backtest.py 一致）。
ROUND_TRIP_FEE_BPS = 5.0
DEFAULT_ENTRY_SLIP_BPS = 5.0
DEFAULT_EXIT_SLIP_BPS = 15.0
DEFAULT_STOP_DELAY_SLIP_BPS = 10.0  # 仅 max_loss 止损出场（H-7 原口径）

FETCH_CHUNK_BARS = 19_000  # fetch_candle_range 单次保护上限 20,000 根


# ─────────────────────────────────────────────────────────────────────────────
# 配置装载
# ─────────────────────────────────────────────────────────────────────────────

def _load_config(path: Optional[str]) -> Tuple[Dict[str, Any], str]:
    """优先 --config 指定的 live 导出 JSON；缺省尝试容器内 /data 路径；
    都没有则回退 canonical defaults（trigger_*_params 内部回退）并告警。"""
    for p in ([path] if path else []) + ["/data/.agent-config.json"]:
        if p and Path(p).is_file():
            try:
                return json.loads(Path(p).read_text()), p
            except Exception as e:
                print(f"[warn] config {p} 解析失败（{e}），尝试下一路径")
    print("[warn] 未找到 live config —— 使用 canonical defaults（与 live 可能有参数漂移！）")
    return {}, "<canonical defaults>"


def _resolve_params(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """从 live config dict 解析回放所需的全部运行时块。"""
    weights = trigger_weights_params(config=cfg)
    thresholds = trigger_thresholds_params(config=cfg)
    scan = cfg.get("scan") or {}
    ta_le = dict(cfg.get("ta_late_entry") or {})
    # 与 ta_filter screen 一致（:807-808）：pre-screen 强制 mtf off。
    ta_le["mtf_enabled"] = False
    dsl = dict(cfg.get("dsl_exit") or {})
    regime = dict(cfg.get("regime_classifier") or {})
    return {
        "weights": weights,
        "thresholds": thresholds,
        "min_score": float(scan.get("minCompositeScore", 54)),
        "sigma_burst": dict(cfg.get("sigma_burst_gate") or {}),
        "candlestick": dict(cfg.get("candlestick_patterns") or {}),
        "ta_late_entry": ta_le,
        "breakout_exemption": dict(ta_le.get("breakout_exemption") or {}),
        "trend_surface_enabled": bool(cfg.get("trend_surface_enabled", True)),
        "regime": regime,
        "dsl": dsl,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 数据装载（分块 + 预热）
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_chunked(coin: str, interval: str, start_ms: int, end_ms: int,
                   step_ms: int) -> List[Candle]:
    """按 ≤19k 根分块 fetch 并去重排序（5m×180d ≈ 52k 根超单次上限）。"""
    out: List[Candle] = []
    cur = start_ms
    while cur <= end_ms:
        chunk_end = min(end_ms, cur + FETCH_CHUNK_BARS * step_ms - step_ms)
        bars = fetch_candle_range(coin, interval, cur, chunk_end)
        out.extend(bars)
        cur = chunk_end + step_ms
    seen = set()
    dedup: List[Candle] = []
    for b in sorted(out, key=lambda c: c.t):
        if b.t not in seen:
            seen.add(b.t)
            dedup.append(b)
    return dedup


# ── Binance 现货 K 线（HL 5m 仅保留 ~17 天；90-180 天窗口用此源）──
_BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"  # api.binance.com 本机不可达，用公开数据镜像
_BINANCE_CACHE_DIR = _REPO / "logs" / "binance_klines_cache"


def _fetch_binance_chunked(coin: str, interval: str, start_ms: int, end_ms: int,
                           step_ms: int) -> List[Candle]:
    """Binance 现货 klines 分页抓取（<=1000 根/页）+ 磁盘缓存。

    价格源为 Binance 现货（非 HL 永续）；rvol/z-score 均为同源相对量。
    """
    symbol = f"{coin}USDT"
    cache_path = _BINANCE_CACHE_DIR / f"{symbol}_{interval}.json"
    cached: Dict[int, list] = {}
    if cache_path.is_file():
        try:
            for row in json.loads(cache_path.read_text()):
                cached[int(row[0])] = row
        except Exception:
            cached = {}
    grid0 = start_ms - start_ms % step_ms
    have = all(t in cached for t in range(grid0, end_ms + 1, step_ms))
    if not have:
        rows: list = []
        cur = grid0
        now_ms = int(time.time() * 1000)
        while cur <= end_ms:
            url = (f"{_BINANCE_KLINES}?symbol={symbol}&interval={interval}"
                   f"&startTime={cur}&endTime={end_ms + step_ms - 1}&limit=1000")
            with urllib.request.urlopen(url, timeout=30) as r:
                payload = json.loads(r.read())
            if not isinstance(payload, list) or not payload:
                break
            for k in payload:
                # k = [openTime, o, h, l, c, v, closeTime, ...]；只收已收盘 bar
                if int(k[6]) <= now_ms:
                    rows.append([int(k[0]), float(k[1]), float(k[2]),
                                 float(k[3]), float(k[4]), float(k[5])])
            nxt = int(payload[-1][0]) + step_ms
            if nxt <= cur:  # 防御：分页不前进则停
                break
            cur = nxt
            time.sleep(0.05)
        for row in rows:
            cached[row[0]] = row
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps([cached[t] for t in sorted(cached)]))
    return [Candle(t=t, o=cached[t][1], h=cached[t][2], l=cached[t][3],
                   c=cached[t][4], v=cached[t][5])
            for t in sorted(cached) if start_ms <= t <= end_ms]


def _closed_prefix(series: List[Candle], ts_ms: List[int],
                   decision_ms: int, tf_ms: int, take: int) -> List[Candle]:
    """决策时刻已收盘的高 TF 前缀末 `take` 根（同 backtest.py _closed_slice）。"""
    cutoff = decision_ms - tf_ms
    j = bisect.bisect_right(ts_ms, cutoff)
    return series[max(0, j - take):j] if j > 0 else []


# ─────────────────────────────────────────────────────────────────────────────
# 信号评估（逐已收盘 5m bar 复刻 _scan_single_market）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    bar_idx: int          # 信号 bar（成交在 bar_idx+1 开盘）
    side: str             # "long" / "short"
    arm: str              # "baseline" / "sigma4" / "breakout7"
    score: float
    fired: List[str]
    meta: Dict[str, Any] = field(default_factory=dict)


def _eval_bar(coin: str, window5m: List[Candle], c1h: List[Candle],
              c4h: List[Candle], P: Dict[str, Any],
              funnel: Dict[str, int]) -> List[Candidate]:
    """单 bar 信号评估。返回各臂候选（0 或多个）。复刻 perception
    :539-568 触发器列表/:573 squeeze 耦合/:723-805 旁路丢弃链 +
    analyze_perception :782-793 方向推断 + late_entry/#7 流程。"""
    th = P["thresholds"]
    hits = [
        trig.pct_move_spike(window5m, th["sigmaThreshold"]),
        trig.volume_spike(window5m, th["sigmaThreshold"]),
        trig.breakout(
            window5m,
            th["breakoutLookback"],
            min_rvol=th.get("breakoutMinRvol", 1.5),
            rvol_window=th.get("breakoutRvolWindow", 20),
            atr_score_mult=th.get("breakoutAtrScoreMult", 3.0),
            confirm_bars=th.get("breakoutConfirmBars", 2),
        ),
        trig.range_compression(window5m, th["bbLength"], th["bbStdDev"]),
        trig.trend_strength(window5m, th["adxPeriod"]),
        trig.momentum_burst(window5m, th["momentumLookback"], th["momentumPct"]),
        trig.volume_buildup_1h(c1h, th.get("volBuildupRatio", 2.5)),
        trig.trend_flip_1h(c1h, th.get("trendFlipBars", 3)),
        trig.higher_lows_1h(c1h, th.get("higherLowsRequired", 4)),
        trig.uptrend_momentum(window5m, th.get("trendMomentumLookback", 72),
                              th.get("trendMomentumPct", 5.0)),
        trig.downtrend_momentum(window5m, th.get("trendMomentumLookback", 72),
                                th.get("trendMomentumPct", 5.0)),
    ]
    _apply_squeeze_breakout_coupling(hits)
    # momentum_continuation：live off → 不 append（权重也不入分母）。
    cp = P["candlestick"]
    if cp.get("enabled"):
        hits.append(trig.bearish_reversal_candle(
            window5m, cp.get("wick_body_ratio", 2.0),
            int(cp.get("context_lookback", 6)), cp.get("context_pct", 1.5)))
        hits.append(trig.bullish_reversal_candle(
            window5m, cp.get("wick_body_ratio", 2.0),
            int(cp.get("context_lookback", 6)), cp.get("context_pct", 1.5)))
    # dailyMover：runner_mover_surface live off → 恒不 fire，跳过。

    fired_count = sum(1 for h in hits if h.get("fired"))
    if fired_count < 1:  # perception :723-725（age-decay 键不存在时的路径）
        return []
    funnel["fired_ge_1"] += 1

    score = trig.composite_score(hits, P["weights"])
    min_score = P["min_score"]

    # ── 旁路链（:729-771；whale/dailyMover live off → 恒 False）──
    burst_fired = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
    trend_fired = P["trend_surface_enabled"] and any(
        h["name"] in ("uptrendMomentum", "downtrendMomentum") and h["fired"]
        for h in hits)
    trend_chop = False
    if trend_fired and c1h:
        try:
            rg = P["regime"]
            trend_chop = classify_candles(
                c1h,
                fast_p=rg.get("fast_ema"), slow_p=rg.get("slow_ema"),
                slope_up=rg.get("slope_threshold"),
                adx_max=rg.get("chop_adx_max")) == "chop"
        except Exception:
            trend_chop = False  # R12-D1：分类打嗝不埋没信号（同 live）
    trend_bypass = trend_fired and not trend_chop
    pattern_bypass = bool(cp.get("enabled")) and any(
        h["name"] in ("bearishReversalCandle", "bullishReversalCandle")
        and h["fired"] for h in hits)

    # #4 sigma_burst：live shadow → sigma_burst_bypass 恒 False，仅记录 would。
    sigma_would, sigma_info = _sigma_burst_decision(
        float(score), float(min_score), hits, P["sigma_burst"])

    surfaced = not (score < min_score and not burst_fired
                    and not trend_bypass and not pattern_bypass)
    fired_names = [h["name"] for h in hits if h.get("fired")]
    perception = {"coin": coin, "triggers": hits}

    # ── 方向推断（analyze_perception :782-793；extension 用 4h 已收盘切片）──
    def _direction() -> Tuple[Optional[str], Optional[float]]:
        ext = _extension_atr(c4h) if c4h else None
        fired_set = set(extract_fired_triggers(perception))
        bullish = fired_set & {"breakout", "momentumBurst", "uptrendMomentum",
                               "trendFlip1h", "higherLows1h", "volumeBuildup1h",
                               "dailyMover"}
        burst_down = "momentumBurst" in fired_set and (ext or 0) < 0
        intend_long = bool(bullish) and not burst_down
        intend_short = bool(fired_set & {"downtrendMomentum"}) or burst_down
        # 多空互斥保护（breakout 空向 fired + 无多头触发器等边角）：
        # 与 live 一致 —— live 只按 intend_long/intend_short 取 side，
        # 同时为真时 long 优先（analyze_perception 顺序：先 long 后 short，
        # 实际 executor 以 late_entry 侧为准；此处取 long 优先并记录）。
        if intend_long:
            return "long", ext
        if intend_short:
            return "short", ext
        return None, ext

    cands: List[Candidate] = []
    if not surfaced:
        # 仅 #4 臂：would-surface 且过同一套下游门。
        if sigma_would:
            funnel["sigma_would"] += 1
            side, ext = _direction()
            if side is None:
                funnel["sigma_no_dir"] += 1
            else:
                le = late_entry_check(c4h, None, side, P["ta_late_entry"])
                if le.get("block"):
                    funnel["sigma_le_block"] += 1
                else:
                    cands.append(Candidate(
                        -1, side, "sigma4", score, fired_names,
                        {"pct_z": sigma_info.get("pct_z"),
                         "vol_z": sigma_info.get("vol_z"),
                         "rsi4h": le.get("rsi4h"), "adx4h": le.get("adx4h"),
                         "extension": ext}))
        return cands

    funnel["surfaced"] += 1
    side, ext = _direction()
    if side is None:
        funnel["no_dir"] += 1
        return cands
    le = late_entry_check(c4h, None, side, P["ta_late_entry"])
    if not le.get("block"):
        cands.append(Candidate(
            -1, side, "baseline", score, fired_names,
            {"rsi4h": le.get("rsi4h"), "adx4h": le.get("adx4h"),
             "extension": ext,
             "relaxed_by_trend": le.get("relaxed_by_trend")}))
    else:
        funnel["le_block"] += 1
        hq, hq_info = _high_quality_breakout(perception, P["breakout_exemption"])
        if hq:
            funnel["hq_would"] += 1
            cands.append(Candidate(
                -1, side, "breakout7", score, fired_names,
                {"rvol": hq_info.get("rvol"), "le_reason": le.get("reason"),
                 "rsi4h": le.get("rsi4h"), "adx4h": le.get("adx4h"),
                 "extension": ext}))
    return cands


# ─────────────────────────────────────────────────────────────────────────────
# 出场引擎（dsl_exit.py 核心阶梯全真复刻，bar 级适配）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DslParams:
    max_loss_pct: float
    protect_pct: float
    retrace_threshold: float
    hard_timeout_minutes: float
    breakeven_trigger_pct: float
    breakeven_lock_pct: float
    stale_flat_timeout_minutes: float
    phase2_tiers: List[Tuple[float, float]]  # (pct_above_entry, retrace) 升序
    time_scratch_minutes: float = 0.0   # >0: 持仓超时且 peak<min_peak → 平仓
    time_scratch_min_peak: float = 0.3

    @classmethod
    def from_config(cls, blk: Dict[str, Any]) -> "DslParams":
        tiers_raw = blk.get("phase2_tiers") or [
            {"pct_above_entry": 2, "retrace_threshold": 0.35},
            {"pct_above_entry": 6, "retrace_threshold": 0.30},
            {"pct_above_entry": 12, "retrace_threshold": 0.20},
            {"pct_above_entry": 20, "retrace_threshold": 0.15},
        ]
        tiers = sorted(
            ((float(t.get("pct_above_entry", 0)),
              float(t.get("retrace_threshold", 0.2))) for t in tiers_raw),
            key=lambda x: x[0])
        return cls(
            max_loss_pct=float(blk.get("max_loss_pct", 1.0)),
            protect_pct=float(blk.get("protect_pct", 1.5)),
            retrace_threshold=float(blk.get("retrace_threshold", 0.15)),
            hard_timeout_minutes=float(blk.get("hard_timeout_minutes", 600)),
            breakeven_trigger_pct=float(blk.get("breakeven_trigger_pct", 2.5)),
            breakeven_lock_pct=float(blk.get("breakeven_lock_pct", 0.3)),
            stale_flat_timeout_minutes=float(
                blk.get("stale_flat_timeout_minutes", 240)),
            phase2_tiers=tiers,
            time_scratch_minutes=(
                float((blk.get("time_scratch") or {}).get("minutes", 0))
                if (blk.get("time_scratch") or {}).get("enabled") else 0.0),
            time_scratch_min_peak=float(
                (blk.get("time_scratch") or {}).get("min_peak_pct", 0.3)),
        )


@dataclass
class Trade:
    coin: str
    side: str
    arm: str
    entry_t: int
    entry_px: float
    notional: float
    score: float
    fired: List[str]
    meta: Dict[str, Any]
    exit_t: int = 0
    exit_px: float = 0.0
    exit_reason: str = ""
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    hold_bars: int = 0
    peak_pct: float = 0.0


def _active_tier(dsl: DslParams, peak_pct: float) -> float:
    """dsl_exit._active_tier：取满足 peak_pct >= pct_above_entry 的最高层
    retrace，默认 retrace_threshold。arm/选层都基于 PEAK（非当前 mark）。"""
    retrace = dsl.retrace_threshold
    for pct_above, tr in dsl.phase2_tiers:
        if peak_pct >= pct_above:
            retrace = tr
    return retrace


def _simulate_trade(cand: Candidate, bars: List[Candle], i: int,
                    dsl: DslParams, notional: float,
                    entry_slip: float, exit_slip: float,
                    stop_delay: float, coin: str,
                    pullback_pct: float = 0.0,
                    pullback_bars: int = 0) -> Optional[Trade]:
    """bar i 收盘出信号，bar i+1 开盘成交，逐 bar 跑 dsl_exit 阶梯。

    pullback_pct>0：改为限价挂单 —— 参考价 = bar i+1 开盘，
    限价 = ref*(1-sgn*pct%)，pullback_bars 根内触及则成交（gap 有利按
    开盘价），否则撤单返回 None。"""
    j = i + 1
    if j >= len(bars):
        return None
    sgn = 1 if cand.side == "long" else -1
    if pullback_pct > 0 and pullback_bars > 0:
        limit_px = bars[j].o * (1 - sgn * pullback_pct / 100.0)
        filled = False
        for m in range(j, min(j + pullback_bars, len(bars))):
            bm = bars[m]
            if (bm.l <= limit_px) if sgn > 0 else (bm.h >= limit_px):
                entry_px = (min(limit_px, bm.o) if sgn > 0
                            else max(limit_px, bm.o))
                entry_px *= (1 + sgn * entry_slip / 1e4)  # 保守仍计入场滑
                entry_t = bm.t
                j = m
                filled = True
                break
        if not filled:
            return None
    else:
        entry_px = bars[j].o * (1 + sgn * entry_slip / 1e4)
        entry_t = bars[j].t
    tr = Trade(coin=coin, side=cand.side, arm=cand.arm, entry_t=entry_t,
               entry_px=entry_px, notional=notional, score=cand.score,
               fired=cand.fired, meta=cand.meta)

    peak = entry_px
    prev_floor: Optional[float] = None
    # lev=1 → effective_max_loss = min(max_loss_pct, roe_cap/lev)（:866-896，
    # atr_stop off）。roe cap 15/1=15 ≥ 1 → 恒取 max_loss_pct。
    eff_max_loss = dsl.max_loss_pct
    stop_px = entry_px * (1 - sgn * eff_max_loss / 100.0)

    def _fill(raw_stop: float, bar: Candle) -> float:
        """止损/地板成交：gap 穿过则按开盘价成交（不利方向）。"""
        return bar.o if (bar.o - raw_stop) * sgn < 0 else raw_stop

    def _close(exit_t: int, raw_px: float, reason: str, k: int,
               is_stop: bool) -> Trade:
        slip = exit_slip + (stop_delay if is_stop else 0.0)
        tr.exit_t = exit_t
        tr.exit_px = raw_px * (1 - sgn * slip / 1e4)
        tr.exit_reason = reason
        tr.hold_bars = k - j + 1
        tr.peak_pct = sgn * (peak - entry_px) / entry_px * 100.0
        tr.pnl_gross = sgn * (tr.exit_px - entry_px) / entry_px * notional
        tr.pnl_net = tr.pnl_gross - notional * ROUND_TRIP_FEE_BPS / 1e4
        return tr

    for k in range(j, len(bars)):
        b = bars[k]
        elapsed_min = (b.t + MS_5M - entry_t) / 60_000.0
        peak_pct = sgn * (peak - entry_px) / entry_px * 100.0  # 截至 k-1 收盘

        # check() 优先级链（dsl_exit :970-1187）：
        # 0) time_scratch(实验) → 1) stale_flat → 2) hard_timeout →
        # 3) max_loss → 4) phase2 floor
        if (dsl.time_scratch_minutes > 0
                and elapsed_min >= dsl.time_scratch_minutes
                and peak_pct < dsl.time_scratch_min_peak):
            return _close(b.t + MS_5M, b.c, "time_scratch", k, False)
        if (dsl.stale_flat_timeout_minutes > 0
                and elapsed_min >= dsl.stale_flat_timeout_minutes
                and peak_pct < dsl.protect_pct):
            return _close(b.t + MS_5M, b.c, "stale_flat_timeout", k, False)
        if (dsl.hard_timeout_minutes > 0
                and elapsed_min >= dsl.hard_timeout_minutes):
            return _close(b.t + MS_5M, b.c, "hard_timeout", k, False)
        hit_stop = (b.l <= stop_px) if sgn > 0 else (b.h >= stop_px)
        if hit_stop:
            return _close(b.t + MS_5M, _fill(stop_px, b), "max_loss", k, True)
        # phase 2：arm 基于 PEAK ≥ protect（:1182）
        if peak_pct >= dsl.protect_pct:
            retrace = _active_tier(dsl, peak_pct)
            floor = entry_px + (peak - entry_px) * (1 - retrace)
            if (dsl.breakeven_trigger_pct > 0
                    and peak_pct >= dsl.breakeven_trigger_pct):
                be_floor = entry_px * (1 + sgn * dsl.breakeven_lock_pct / 100.0)
                floor = max(floor, be_floor) if sgn > 0 else min(floor, be_floor)
            if prev_floor is not None:  # monotonic：只收紧不放松
                floor = max(floor, prev_floor) if sgn > 0 else min(floor, prev_floor)
            prev_floor = floor
            breached = (b.l < floor) if sgn > 0 else (b.h > floor)
            if breached:
                return _close(b.t + MS_5M, _fill(floor, b), "floor_breach", k, False)
        # peak 收盘后推进 —— 杜绝 bar 内前视
        peak = max(peak, b.h) if sgn > 0 else min(peak, b.l)

    # 数据末端仍持仓 → 按末根收盘价平仓
    k = len(bars) - 1
    return _close(bars[k].t + MS_5M, bars[k].c, "end_of_data", k, False)


# ─────────────────────────────────────────────────────────────────────────────
# 单币回放
# ─────────────────────────────────────────────────────────────────────────────

_OPP = {"long": "short", "short": "long"}

# 实验臂 → 信号源臂（baseline 派生）与 DSL 键（main 注入 dsls dict）
DERIVED_ARMS = ["dsl_t1", "dsl_t2", "fade_live", "fade_tuned",
                "filt", "filt_s60", "pullback"]
PULLBACK_PCT = 0.4   # 限价回撤幅度（%）
PULLBACK_BARS = 24   # 挂单有效期（5m 根数 = 2h）


def _passes_filter(c: Candidate) -> bool:
    """方向3 过滤：禁 short、砍 relaxed_by_trend、黑名单抄底组合。"""
    if c.side != "long":
        return False
    if c.meta.get("relaxed_by_trend"):
        return False
    fs = set(c.fired)
    if {"higherLows1h", "uptrendMomentum"} <= fs:  # 抄底组合（39 笔 -$2.1k）
        return False
    return True


def replay_coin(coin: str, start_ms: int, end_ms: int, P: Dict[str, Any],
                dsls: Dict[str, DslParams], notional: float,
                slips: Tuple[float, float, float],
                source: str = "hyperliquid"
                ) -> Tuple[List[Trade], Dict[str, int]]:
    funnel: Dict[str, int] = {
        "bars": 0, "fired_ge_1": 0, "surfaced": 0, "no_dir": 0,
        "le_block": 0, "hq_would": 0, "sigma_would": 0,
        "sigma_no_dir": 0, "sigma_le_block": 0,
        "skip_open_pos": 0, "fetch_error": 0,
    }
    trades: List[Trade] = []
    fetch = _fetch_binance_chunked if source == "binance" else _fetch_chunked
    try:
        bars = fetch(coin, "5m", start_ms - 120 * MS_5M, end_ms, MS_5M)
        c1h_all = fetch(coin, "1h", start_ms - 60 * MS_1H, end_ms, MS_1H)
        c4h_all = fetch(coin, "4h", start_ms - 110 * MS_4H, end_ms, MS_4H)
    except Exception as e:
        print(f"  [fetch-error] {coin}: {e}")
        funnel["fetch_error"] += 1
        return trades, funnel
    ts_1h = [c.t for c in c1h_all]
    ts_4h = [c.t for c in c4h_all]
    if len(bars) < 60:
        print(f"  [skip] {coin}: 5m 数据不足（{len(bars)} 根）")
        return trades, funnel

    # 各臂候选 → 逐臂模拟（每币每臂同一时刻至多一仓）
    per_arm: Dict[str, List[Tuple[int, Candidate]]] = {
        arm: [] for arm in
        ["baseline", "sigma4", "breakout7"] + DERIVED_ARMS}
    for i in range(len(bars)):
        if bars[i].t < start_ms or bars[i].t > end_ms:
            continue
        if i + 1 >= len(bars):
            break  # 末根无下一根成交
        window = bars[max(0, i - 99):i + 1]
        if len(window) < 50:  # live 最少 50 根（:503/:524）
            continue
        funnel["bars"] += 1
        decision_ms = bars[i].t + MS_5M
        c1h = _closed_prefix(c1h_all, ts_1h, decision_ms, MS_1H, 48)
        c4h = _closed_prefix(c4h_all, ts_4h, decision_ms, MS_4H, 100)
        for cand in _eval_bar(coin, window, c1h, c4h, P, funnel):
            cand.bar_idx = i
            per_arm[cand.arm].append((i, cand))
            if cand.arm == "baseline":
                # fade：同信号反向（side 反转，meta 记原方向）
                fmeta = dict(cand.meta)
                fmeta["orig_side"] = cand.side
                for fa in ("fade_live", "fade_tuned"):
                    per_arm[fa].append((i, replace(
                        cand, side=_OPP[cand.side], arm=fa, meta=fmeta)))
                # DSL 降档两臂（信号不变，出场变）
                for da in ("dsl_t1", "dsl_t2"):
                    per_arm[da].append((i, replace(cand, arm=da)))
                # pullback：限价挂单入场
                per_arm["pullback"].append((i, replace(cand, arm="pullback")))
                # 过滤臂
                if _passes_filter(cand):
                    per_arm["filt"].append((i, replace(cand, arm="filt")))
                    if cand.score >= 60:
                        per_arm["filt_s60"].append(
                            (i, replace(cand, arm="filt_s60")))

    for arm, cands in per_arm.items():
        arm_dsl = dsls.get(arm, dsls["live"])
        pb_kw = ({"pullback_pct": PULLBACK_PCT, "pullback_bars": PULLBACK_BARS}
                 if arm == "pullback" else {})
        open_until = -1  # 持仓覆盖到的最后一根 bar idx
        for i, cand in cands:
            if i <= open_until:
                funnel["skip_open_pos"] += 1
                continue
            tr = _simulate_trade(cand, bars, i, arm_dsl, notional,
                                 slips[0], slips[1], slips[2], coin, **pb_kw)
            if tr is None:
                continue
            trades.append(tr)
            open_until = i + tr.hold_bars  # 入场 bar i+1 … i+hold_bars
    return trades, funnel


# ─────────────────────────────────────────────────────────────────────────────
# 汇总报告
# ─────────────────────────────────────────────────────────────────────────────

def _summ(trades: List[Trade]) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if t.pnl_net > 0]
    net = sum(t.pnl_net for t in trades)
    gross_w = sum(t.pnl_net for t in wins)
    gross_l = -sum(t.pnl_net for t in trades if t.pnl_net <= 0)
    return {
        "n": n,
        "winrate": len(wins) / n * 100,
        "expectancy": net / n,
        "net": net,
        "pf": (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "avg_hold_h": sum(t.hold_bars for t in trades) * 5 / 60 / n,
        "avg_peak": sum(t.peak_pct for t in trades) / n,
    }


def _fmt_row(name: str, s: Dict[str, Any]) -> str:
    if s["n"] == 0:
        return f"  {name:<28s}   0 笔"
    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    return (f"  {name:<28s} {s['n']:>4d} 笔  胜率 {s['winrate']:>5.1f}%  "
            f"期望 ${s['expectancy']:>7.2f}  净损益 ${s['net']:>9.2f}  "
            f"PF {pf:>5s}  均持仓 {s['avg_hold_h']:>5.1f}h  "
            f"均峰值 {s['avg_peak']:>5.2f}%")


ARMS = [("baseline", "baseline（live ≥54+旁路）"),
        ("sigma4", "#4 sigma_burst would"),
        ("breakout7", "#7 breakout would-down"),
        ("dsl_t1", "E1a DSL降档(protect.8/be1.0)"),
        ("dsl_t2", "E1b t1+time_scratch(60/.3)"),
        ("fade_live", "E2a fade 反向(live DSL)"),
        ("fade_tuned", "E2b fade 反向(降档DSL)"),
        ("filt", "E3a 过滤(禁short/relaxed/黑)"),
        ("filt_s60", "E3b 过滤+score≥60"),
        ("pullback", "E4 限价-0.4%挂单(24根)")]


def report(all_trades: List[Trade], funnels: Dict[str, Dict[str, int]],
           start_ms: int, end_ms: int, days: int,
           source: str = "hyperliquid") -> str:
    lines: List[str] = []
    seg90_start = end_ms - 90 * MS_DAY

    def _seg(ts: int) -> str:
        return "近90天" if ts >= seg90_start else "全窗口"

    lines.append("=" * 78)
    lines.append(f"回放窗口: {time.strftime('%Y-%m-%d', time.gmtime(start_ms/1000))}"
                 f" → {time.strftime('%Y-%m-%d', time.gmtime(end_ms/1000))}"
                 f"（{days} 天，分段：全窗口 / 近 90 天）")
    lines.append("=" * 78)

    lines.append("\n── 漏斗（8 币合计）──")
    tot: Dict[str, int] = {}
    for f in funnels.values():
        for k, v in f.items():
            tot[k] = tot.get(k, 0) + v
    for k in ("bars", "fired_ge_1", "surfaced", "no_dir", "le_block",
              "hq_would", "sigma_would", "sigma_no_dir", "sigma_le_block",
              "skip_open_pos", "fetch_error"):
        if tot.get(k):
            lines.append(f"  {k:<16s} {tot[k]}")

    for seg_name, seg_start in (("全窗口", None), ("近90天", seg90_start)):
        lines.append(f"\n── 三臂对照（{seg_name}）──")
        for arm_key, arm_name in ARMS:
            ts = [t for t in all_trades if t.arm == arm_key
                  and (seg_start is None or t.entry_t >= seg_start)]
            lines.append(_fmt_row(arm_name, _summ(ts)))

    lines.append("\n── baseline 按币分解（全窗口）──")
    for coin in MAJORS:
        ts = [t for t in all_trades if t.arm == "baseline" and t.coin == coin]
        if ts:
            lines.append(_fmt_row(coin, _summ(ts)))

    lines.append("\n── 出场原因分布（全窗口）──")
    for arm_key, arm_name in ARMS:
        reasons: Dict[str, int] = {}
        for t in all_trades:
            if t.arm == arm_key:
                reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        if reasons:
            body = "  ".join(f"{k}×{v}" for k, v in
                             sorted(reasons.items(), key=lambda x: -x[1]))
            lines.append(f"  {arm_name:<28s} {body}")

    lines.append("\n── Caveats（不回放项 / 口径说明）──")
    _caveats: List[str] = []
    if source == "binance":
        _caveats.append(
            "数据源=Binance 现货 K 线（HL 5m 仅保留 ~17 天）：价格/成交量为 "
            "Binance 现货口径，与 HL 永续轻微偏差；rvol/z-score 为同源相对量")
    for c in _caveats + [
        "H-5 wick guard / index 确认：bar 级无 tick/index，max_loss 立即触发（fail-open）",
        "noise_band / regime_aware 分层 / smooth_transition / time_scratch 未回放",
        "whale / dailyMover / momentum_continuation / age-decay：live 全关，已跳过",
        "下游 LLM 研究、executor 风险门（流动性/持仓上限/组合层）不回放",
        "stop_delay 10bps 仅加在 max_loss（H-7 原口径）；floor_breach 按普通 exit slip",
        "每币每臂同一时刻至多一仓，持仓期间同币同臂信号忽略；无组合层并发上限",
        "#4/#7 为 shadow 臂的 would-* 反事实回放（as-if enforce），非 live 实际成交",
    ]:
        lines.append(f"  - {c}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None,
                    help="live .agent-config.json 导出路径（缺省尝试 /data，"
                         "再回退 canonical defaults）")
    ap.add_argument("--days", type=int, default=180, help="回放天数（默认 180）")
    ap.add_argument("--source", choices=["hyperliquid", "binance"],
                    default="hyperliquid",
                    help="K 线数据源：HL 5m 仅 ~17 天历史；90-180 天窗口用 binance 现货")
    ap.add_argument("--coins", default=",".join(MAJORS),
                    help="逗号分隔币池（默认 _MAJOR_VOLUMES 8 币）")
    ap.add_argument("--end-ms", type=int, default=None,
                    help="窗口结束 anchor（默认=当前最近已收盘 5m bar open）")
    ap.add_argument("--notional", type=float, default=10_000.0)
    ap.add_argument("--entry-slip-bps", type=float, default=DEFAULT_ENTRY_SLIP_BPS)
    ap.add_argument("--exit-slip-bps", type=float, default=DEFAULT_EXIT_SLIP_BPS)
    ap.add_argument("--stop-delay-bps", type=float, default=DEFAULT_STOP_DELAY_SLIP_BPS)
    ap.add_argument("--write", action="store_true",
                    help="写隔离产物 JSONL（默认 dry-run 只打印汇总）")
    ap.add_argument("--out", default=str(_REPO / "logs" / "backtest_majors_surge_experiments.jsonl"))
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    cfg, cfg_src = _load_config(args.config)
    P = _resolve_params(cfg)
    live_dsl = DslParams.from_config(P["dsl"])
    tuned = replace(
        live_dsl,
        protect_pct=0.8,
        breakeven_trigger_pct=1.0,
        phase2_tiers=[(0.8, 0.30), (6.0, 0.30), (12.0, 0.20), (20.0, 0.15)])
    tuned_ts = replace(tuned, time_scratch_minutes=60.0,
                       time_scratch_min_peak=0.3)
    dsls: Dict[str, DslParams] = {
        "live": live_dsl,
        "baseline": live_dsl, "sigma4": live_dsl, "breakout7": live_dsl,
        "fade_live": live_dsl,
        "dsl_t1": tuned, "dsl_t2": tuned_ts,
        "fade_tuned": tuned, "filt": tuned, "filt_s60": tuned,
        "pullback": tuned,
    }

    now_ms = int(time.time() * 1000)
    end_ms = args.end_ms or (now_ms // MS_5M) * MS_5M - MS_5M
    start_ms = end_ms - args.days * MS_DAY

    print(f"config: {cfg_src}")
    print(f"coins: {','.join(coins)}  days: {args.days}  notional: ${args.notional:,.0f}  "
          f"source: {args.source}")
    print(f"gate: min_score={P['min_score']:.0f}  weights_sum={sum(P['weights'].values()):.2f}  "
          f"breakoutMinRvol={P['thresholds'].get('breakoutMinRvol')}  "
          f"confirmBars={P['thresholds'].get('breakoutConfirmBars')}")
    print(f"sigma_burst_gate: {P['sigma_burst'] or '<off>'}")
    print(f"breakout_exemption: {P['breakout_exemption'] or '<off>'}")
    print(f"dsl_exit: max_loss={live_dsl.max_loss_pct}% protect={live_dsl.protect_pct}% "
          f"retrace={live_dsl.retrace_threshold} tiers={live_dsl.phase2_tiers} "
          f"be={live_dsl.breakeven_trigger_pct}/{live_dsl.breakeven_lock_pct} "
          f"hard={live_dsl.hard_timeout_minutes}m stale={live_dsl.stale_flat_timeout_minutes}m")

    all_trades: List[Trade] = []
    funnels: Dict[str, Dict[str, int]] = {}
    for coin in coins:
        print(f"\n[{coin}] fetching + replaying ...")
        t0 = time.time()
        trades, funnel = replay_coin(
            coin, start_ms, end_ms, P, dsls, args.notional,
            (args.entry_slip_bps, args.exit_slip_bps, args.stop_delay_bps),
            source=args.source)
        funnels[coin] = funnel
        all_trades.extend(trades)
        n_base = sum(1 for t in trades if t.arm == "baseline")
        print(f"  bars={funnel['bars']} surfaced={funnel['surfaced']} "
              f"trades={len(trades)} (baseline {n_base})  "
              f"{time.time()-t0:.1f}s")

    print("\n" + report(all_trades, funnels, start_ms, end_ms, args.days,
                             args.source))

    if args.write:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            f.write(json.dumps({
                "type": "run_meta", "config_src": cfg_src, "coins": coins,
                "source": args.source,
            "arms": [k for k, _ in ARMS],
                "days": args.days, "start_ms": start_ms, "end_ms": end_ms,
                "notional": args.notional,
                "fees_bps": ROUND_TRIP_FEE_BPS,
                "slips_bps": [args.entry_slip_bps, args.exit_slip_bps,
                              args.stop_delay_bps],
                "dsl": P["dsl"], "min_score": P["min_score"],
                "outcome_source": "historical_replay",
            }) + "\n")
            for t in sorted(all_trades, key=lambda x: x.entry_t):
                f.write(json.dumps({
                    "type": "trade", "arm": t.arm, "coin": t.coin,
                    "side": t.side, "entry_t": t.entry_t, "entry_px": t.entry_px,
                    "exit_t": t.exit_t, "exit_px": t.exit_px,
                    "exit_reason": t.exit_reason, "pnl_gross": round(t.pnl_gross, 4),
                    "pnl_net": round(t.pnl_net, 4), "hold_bars": t.hold_bars,
                    "peak_pct": round(t.peak_pct, 4), "score": t.score,
                    "fired": t.fired, "meta": t.meta,
                }) + "\n")
        print(f"\n[write] {len(all_trades)} trades → {out_path}")
    else:
        print("\n[dry-run] 未写产物（--write 落隔离 JSONL）")


if __name__ == "__main__":
    main()
