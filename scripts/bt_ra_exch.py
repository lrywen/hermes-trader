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
    + breakeven(2.5→0.3) clamp + monotonic floor。成本模型（P5-1 实测校正）：
    来回费 8.64bps、entry/exit 滑 = 半价差（per-coin 逐币，majors 均值 0.31）、
    max_loss 止损无额外延迟滑点。旧口径 5/5/15/10 是假设，已弃用；用
    --slip-mode flat --fee-bps 5.0 --entry-slip-bps 5.0 --exit-slip-bps 15.0
    --stop-delay-bps 10.0 可精确复现旧结果做对照。
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

from hermes_trader.agents.config import (
    trigger_thresholds_params,
    trigger_weights_params,
)
from hermes_trader.agents.market_regime import classify_candles
from hermes_trader.agents.perception import (
    _apply_squeeze_breakout_coupling,
    _sigma_burst_decision,
    extract_fired_triggers,
)
from hermes_trader.agents.ta_filter import (
    _extension_atr,
    _high_quality_breakout,
    late_entry_check,
)
from hermes_trader.backtest.stop_model import effective_stop_pct
from hermes_trader.data.historical_candles import fetch_candle_range
from hermes_trader.indicators import triggers as trig
from hermes_trader.indicators.math import atr as _atr_series
from hermes_trader.models.types import Candle

# executor.py:1144-1147 —— 主流币池（8 币）。
MAJORS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX"]

# ── P6b-1a：交易所侧触发单参数（实盘 executor.py 的两条挂单路径）───────────
# 实盘开仓时会挂：
#   ① 备用 SL   _place_backup_sl(executor.py:1584)
#        atr_stop_pct = (atr_4h/entry_px)*sl_atr_mult*100
#        sl_width_pct = clamp(atr_stop_pct, sl_floor_pct, sl_ceiling_pct)
#        sl_px = entry_px ∓ entry_px*sl_width_pct/100
#   ② TP 分批   _place_tp_scale_out(executor.py:1732)
#        tp_px = entry_px ± atr_4h*tp_atr_mult
#        tp_size = size*tp_scale_fraction，若 tp_notional < min_order_usd 则
#          UPSIZE 到最小额（占仓位 ≥90% 时 SKIP）
# 关键：ATR 用的是【4h ATR(14)】（_price_atr_guard → get_hl_atr("4h",14,coin)）。
# 回测此前【完全没有建模这两条路径】—— 这是回测与实盘出场机制正交的根因。
EXCH_DEFAULTS = {
    "sl_atr_mult": 1.2,
    "sl_floor_pct": 1.2,
    "sl_ceiling_pct": 3.0,
    "tp_atr_mult": 2.0,
    "tp_scale_fraction": 0.4,
    "min_order_usd": 10.5,
    "skip_threshold": 0.90,
    "sl_buffer_bps": 10.0,  # B-1a-改①：Phase2 镜像落后 DSL floor 的缓冲
}


def _exch_params(cfg: Dict[str, Any]) -> Dict[str, float]:
    """从权威配置解析交易所侧触发单参数（顶层键）。"""
    out = dict(EXCH_DEFAULTS)
    for k in ("sl_atr_mult", "sl_floor_pct", "sl_ceiling_pct",
              "tp_atr_mult", "tp_scale_fraction", "min_order_usd",
              "sl_buffer_bps"):
        if k in cfg:
            try:
                v = float(cfg[k])
                if v == v:  # 非 NaN
                    out[k] = v
            except (TypeError, ValueError):
                pass
    return out


MS_5M = 5 * 60_000
MS_1H = 60 * 60_000
MS_4H = 4 * 3_600_000
MS_DAY = 24 * 3_600_000

# H-7 成本模型（与 scripts/backtest.py 一致）。
#
# ── P5-1 实测校正（2026-09-19）─────────────────────────────────────────────
# 旧默认值 5/5/15/10 是【假设】不是【测量】，实测两头都错：
#   手续费：链上 70 笔 100% crossed=True（IOC taker），单边 4.32 bps → 往返 8.64。
#           旧 5.0 低估 3.64 bps。
#   滑点：place_hl_order 走穿价 IOC（买 best_ask*1.01 / 卖 best_bid*0.99），
#         代码自注「An IOC fills at the resting price」→ 滑点 = 半价差。
#         majors 半价差均值 0.31 bps；旧假设 5.0/15.0 高估近 50 倍。
#   止损延迟：实测无额外延迟滑点 → 0.0（旧 10.0）。
# 净效果 25.0 → 9.26 bps。只改一头的修正会给出错误结论（Sprint 0 §3 的教训）。
ROUND_TRIP_FEE_BPS = 8.64           # 原 5.0   实测 HL IOC taker 往返
DEFAULT_ENTRY_SLIP_BPS = 0.31       # 原 5.0   majors 半价差均值
DEFAULT_EXIT_SLIP_BPS = 0.31        # 原 15.0  majors 半价差均值
DEFAULT_STOP_DELAY_SLIP_BPS = 0.0   # 原 10.0  实测无额外止损延迟滑点

# P5-1d：按流动性分档滑点（逐币半价差实测，l2Book top-of-book）。
# 全池一个常数 0.31 会同时高估大币、低估小币 —— BTC 0.06 被高估 5 倍，
# AVAX 0.63 被低估 2 倍。实盘币池（小市值）半价差均值 2.54 bps，是 majors 的
# 8 倍；未列入此表的币回落到 DEFAULT_*_SLIP_BPS。
PER_COIN_SLIP_BPS: Dict[str, float] = {
    "BTC": 0.06, "ETH": 0.19, "SOL": 0.45, "BNB": 0.26,
    "XRP": 0.35, "DOGE": 0.06, "ADA": 0.45, "AVAX": 0.63,
}

# B-4：逐币半价差扩到 81 币池（2026-09-19 l2Book 顶档 3 次中位）。数据随包
# 分发，含采集元数据；缺失时回退上面的 8 majors 硬编码表。
_HALF_SPREAD_JSON = _REPO / "hermes_trader" / "data" / "per_coin_half_spread_bps.json"
try:
    _hs = json.loads(_HALF_SPREAD_JSON.read_text())
    PER_COIN_SLIP_BPS = {
        **{k: float(v) for k, v in (_hs.get("half_spread_bps") or {}).items()},
        **PER_COIN_SLIP_BPS,  # 8 majors 硬编码值优先（历史实测，保持口径稳定）
    }
except (OSError, ValueError):
    pass


def _slip_for(coin: str, flat_bps: float, per_coin: bool) -> float:
    """按币解析半价差滑点；per_coin=False 时退化为 flat_bps（兼容旧扫描口径）。"""
    if not per_coin:
        return flat_bps
    return PER_COIN_SLIP_BPS.get(coin.upper(), flat_bps)

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
        # P6b-1a：交易所侧触发单参数（顶层键）
        "exch": _exch_params(cfg),
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
    # B-1a-改③④：逐笔有效现货止损 = min(spot_cap, max_loss_roe_pct/lev)，
    # ATR 分支 clamp(atr*mult,floor,ceiling) 只能放宽到 regime cap。lev=1 时
    # 回退到旧行为（恒取 max_loss_pct），保证不带杠杆的对照臂口径不变。
    leverage: float = 1.0
    max_loss_roe_pct: float = 0.0       # 0 → ROE cap 不绑定
    atr_stop_enabled: bool = False
    entry_atr_pct: float = 0.0
    atr_mult: float = 1.5
    atr_floor_pct: float = 1.0
    atr_ceiling_pct: float = 4.0

    def effective_max_loss_pct(self) -> float:
        """逐笔带杠杆/ATR 的有效现货止损（与实盘 _effective_max_loss 同源）。"""
        return effective_stop_pct(
            max_loss_pct=self.max_loss_pct, leverage=self.leverage,
            max_loss_roe_pct=self.max_loss_roe_pct,
            atr_stop_enabled=self.atr_stop_enabled,
            entry_atr_pct=self.entry_atr_pct, atr_mult=self.atr_mult,
            atr_floor_pct=self.atr_floor_pct,
            atr_ceiling_pct=self.atr_ceiling_pct).spot_pct

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
            # B-1a-改③④：ROE cap 与 ATR 止损参数（生产权威默认：leverage 在
            # main 从顶层注入，roe=15，atr_stop.enabled=false）。
            leverage=float(blk.get("_leverage", 1.0)),
            max_loss_roe_pct=float(blk.get("max_loss_roe_pct", 0.0)),
            atr_stop_enabled=bool((blk.get("atr_stop") or {}).get("enabled", False)),
            atr_mult=float((blk.get("atr_stop") or {}).get("atr_mult", 1.5)),
            atr_floor_pct=float((blk.get("atr_stop") or {}).get("floor_pct", 1.0)),
            atr_ceiling_pct=float((blk.get("atr_stop") or {}).get("ceiling_pct", 4.0)),
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
                    pullback_bars: int = 0,
                    exch: Optional[Dict[str, float]] = None,
                    atr_abs: float = 0.0,
                    sizing: Optional[Dict[str, float]] = None,
                    bar_ms: int = MS_5M) -> Optional[Trade]:
    """bar i 收盘出信号，bar i+1 开盘成交，逐 bar 跑 dsl_exit 阶梯。

    bar_ms：``bars`` 的真实周期。5m 回放取 MS_5M（默认，行为与历史逐字一致）；
    C-7 的 1h 出场回放取 MS_1H —— 此时调用方负责把信号去重成「每小时一个」并
    让 ``i`` 索引到 1h 序列（PIT 对齐，见 replay_coin）。所有墙钟量（elapsed、
    成交时间戳、pullback 挂单有效期的调用方根数换算）都按 bar_ms 标定。

    pullback_pct>0：改为限价挂单 —— 参考价 = bar i+1 开盘，
    限价 = ref*(1-sgn*pct%)，pullback_bars 根内触及则成交（gap 有利按
    开盘价），否则撤单返回 None。

    exch 非 None（P6b-1a）：额外建模实盘开仓时挂的**交易所侧触发单** ——
      ① 备用 SL（_place_backup_sl）：宽度 clamp(atr4h%*sl_atr_mult,
         sl_floor_pct, sl_ceiling_pct)，价格在 entry 不利侧
      ② TP 分批（_place_tp_scale_out）：tp_px = entry ± atr4h*tp_atr_mult，
         平 tp_scale_fraction；意图额 < min_order_usd 时 UPSIZE 到最小额
         （占仓位 >= skip_threshold 则 SKIP）
    交易所单是 tick 级挂单，**同一根 bar 内先于 DSL 的收盘检查**触发。
    atr_abs 必须是【4h ATR(14)】的绝对值（复现 get_hl_atr("4h",14,coin)）。
    """
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
    # ── P6b-1a：逐笔 notional（复现实盘 atr_equal_risk_notional + tiered cap）──
    # 实盘 notional = risk_per_trade_pct*equity / ((sl_atr_mult*atr4h)/entry_px)，
    # 再被 max_trade_notional_usd 截顶（_tiered_notional_cap：equity<50 → base）。
    # $30 账户下：高波动币 → $1.75，低波动币 → $30（截顶）。
    # 【关键】notional 决定交易所侧 TP 腿是否被 UPSIZE。回测若用固定 $10,000，
    # TP 意图额 = $4,000 >> $10.5 最小额，upsize 永不触发 → 实盘 52.6% 的
    # exchange_trigger 在回测里退化成 0。**这是「notional 规模」影响出场机制
    # 的具体通路**，不是单纯的仓位大小差异。
    if sizing is not None and atr_abs > 0:
        _stop_frac = (float(sizing.get("sl_atr_mult", 1.2)) * atr_abs) / entry_px
        if _stop_frac > 0:
            notional = min(
                float(sizing.get("risk_per_trade_pct", 0.026))
                * float(sizing.get("equity", 30.58)) / _stop_frac,
                float(sizing.get("max_notional_usd", 30.0)))
    tr = Trade(coin=coin, side=cand.side, arm=cand.arm, entry_t=entry_t,
               entry_px=entry_px, notional=notional, score=cand.score,
               fired=cand.fired, meta=cand.meta)

    peak = entry_px
    prev_floor: Optional[float] = None
    # B-1a-改③④：逐笔带杠杆的有效现货止损 min(spot_cap, roe_cap/lev)，ATR 分支
    # 只放宽到 regime cap。entry_atr_pct 逐笔捕获（本币 4h ATR / entry），与实盘
    # 注册仓位时锁定一致；用 replace 构造逐笔副本，不污染共享的 dsl。
    # lev=1 且 roe/atr 缺省时退化为旧的 max_loss_pct，对照臂口径不变。
    if dsl.atr_stop_enabled and atr_abs > 0 and entry_px > 0:
        dsl = replace(dsl, entry_atr_pct=atr_abs / entry_px * 100.0)
    eff_max_loss = dsl.effective_max_loss_pct()
    stop_px = entry_px * (1 - sgn * eff_max_loss / 100.0)

    # ── P6b-1a：交易所侧触发单价格 ────────────────────────────────
    ex_sl_px: Optional[float] = None
    ex_tp_px: Optional[float] = None
    tp_frac_eff = 0.0
    min_order = 0.0
    if exch is not None and atr_abs > 0:
        min_order = float(exch.get("min_order_usd", 10.5))
        atr_pct = atr_abs / entry_px * 100.0
        _w = atr_pct * float(exch.get("sl_atr_mult", 1.2))
        _w = min(max(_w, float(exch.get("sl_floor_pct", 1.2))),
                 float(exch.get("sl_ceiling_pct", 3.0)))
        ex_sl_px = entry_px * (1 - sgn * _w / 100.0)
        ex_tp_px = entry_px * (
            1 + sgn * atr_pct * float(exch.get("tp_atr_mult", 2.0)) / 100.0)
        _f = float(exch.get("tp_scale_fraction", 0.4))
        _intended = notional * _f
        if _intended < min_order:
            _up = (min_order / notional) if notional > 0 else 1.0
            tp_frac_eff = (0.0 if _up >= float(exch.get("skip_threshold", 0.9))
                           else _up)
        else:
            tp_frac_eff = _f

    def _fill(raw_stop: float, bar: Candle) -> float:
        """止损/地板成交：gap 穿过则按开盘价成交（不利方向）。"""
        return bar.o if (bar.o - raw_stop) * sgn < 0 else raw_stop

    def _close(exit_t: int, raw_px: float, reason: str, k: int,
               is_stop: bool, realized: float = 0.0,
               size_left: float = 1.0) -> Trade:
        """realized / size_left：交易所侧 TP 分批已实现部分 + 剩余仓位比例。"""
        slip = exit_slip + (stop_delay if is_stop else 0.0)
        tr.exit_t = exit_t
        tr.exit_px = raw_px * (1 - sgn * slip / 1e4)
        tr.exit_reason = reason
        tr.hold_bars = k - j + 1
        tr.peak_pct = sgn * (peak - entry_px) / entry_px * 100.0
        tr.pnl_gross = (realized
                        + sgn * (tr.exit_px - entry_px) / entry_px
                        * notional * size_left)
        tr.pnl_net = tr.pnl_gross - notional * ROUND_TRIP_FEE_BPS / 1e4
        return tr

    realized = 0.0
    size_left = 1.0
    for k in range(j, len(bars)):
        b = bars[k]
        elapsed_min = (b.t + bar_ms - entry_t) / 60_000.0
        peak_pct = sgn * (peak - entry_px) / entry_px * 100.0  # 截至 k-1 收盘

        # ── B-1a-改①：Phase2 交易所镜像 SL = DSL floor × (1 − 10bps) ──
        # 进 Phase2（上一根已 ratchet 出 prev_floor）后，把交易所 Stop Market
        # 从初始静态 SL 收为追踪 DSL floor 的镜像，落后 sl_buffer_bps（实盘=10），
        # 永远在 floor 的不利侧 → 正常回调 DSL 先触发，快速下挫（软件 ~15s 轮询
        # 来不及）镜像先触发（标签 exchange_trigger，实为 floor 镜像）。
        # 用上一根收盘确定的 prev_floor（PIT），单调只收紧。
        if (exch is not None and prev_floor is not None
                and peak_pct >= dsl.protect_pct):
            _buf = float(exch.get("sl_buffer_bps", 10.0))
            mirror = prev_floor * (1 - sgn * _buf / 1e4)
            if ex_sl_px is None:
                ex_sl_px = mirror
            else:  # long 只上移、short 只下移（只收紧）
                ex_sl_px = max(ex_sl_px, mirror) if sgn > 0 \
                    else min(ex_sl_px, mirror)

        # ── P6b-1a：交易所侧触发单（tick 级挂单 → 同一 bar 内先于 DSL）──
        if ex_sl_px is not None:
            hit_ex_sl = (b.l <= ex_sl_px) if sgn > 0 else (b.h >= ex_sl_px)
            hit_ex_tp = (b.h >= ex_tp_px) if sgn > 0 else (b.l <= ex_tp_px)
            if hit_ex_sl:
                # SL 与 TP 同 bar 都触及 → bar 级无法判先后，保守取 SL（不利）
                return _close(b.t + bar_ms, _fill(ex_sl_px, b),
                              "exchange_trigger", k, True,
                              realized=realized, size_left=size_left)
            if hit_ex_tp and tp_frac_eff > 0:
                _part = tp_frac_eff * size_left
                realized += (sgn * (ex_tp_px - entry_px) / entry_px
                             * notional * _part)
                size_left -= _part
                if size_left * notional < min_order:
                    # 剩余仓位低于交易所最小额 → 无法主动平仓（reduce_only 同样
                    # 受 min-size 限制）→ 整仓最终由交易所侧了结
                    realized += (sgn * (ex_tp_px - entry_px) / entry_px
                                 * notional * size_left)
                    return _close(b.t + bar_ms, ex_tp_px,
                                  "exchange_trigger", k, False,
                                  realized=realized, size_left=0.0)
                tp_frac_eff = 0.0  # 剩余仓位继续走 DSL

        # check() 优先级链（dsl_exit :970-1187）：
        # 0) time_scratch(实验) → 1) stale_flat → 2) hard_timeout →
        # 3) max_loss → 4) phase2 floor
        if (dsl.time_scratch_minutes > 0
                and elapsed_min >= dsl.time_scratch_minutes
                and peak_pct < dsl.time_scratch_min_peak):
            return _close(b.t + bar_ms, b.c, "time_scratch", k, False,
                          realized=realized, size_left=size_left)
        if (dsl.stale_flat_timeout_minutes > 0
                and elapsed_min >= dsl.stale_flat_timeout_minutes
                and peak_pct < dsl.protect_pct):
            return _close(b.t + bar_ms, b.c, "stale_flat_timeout", k, False,
                          realized=realized, size_left=size_left)
        if (dsl.hard_timeout_minutes > 0
                and elapsed_min >= dsl.hard_timeout_minutes):
            return _close(b.t + bar_ms, b.c, "hard_timeout", k, False,
                          realized=realized, size_left=size_left)
        hit_stop = (b.l <= stop_px) if sgn > 0 else (b.h >= stop_px)
        if hit_stop:
            return _close(b.t + bar_ms, _fill(stop_px, b), "max_loss", k, True,
                          realized=realized, size_left=size_left)
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
                return _close(b.t + bar_ms, _fill(floor, b), "floor_breach", k,
                              False, realized=realized, size_left=size_left)
        # peak 收盘后推进 —— 杜绝 bar 内前视
        peak = max(peak, b.h) if sgn > 0 else min(peak, b.l)

    # 数据末端仍持仓 → 按末根收盘价平仓
    k = len(bars) - 1
    return _close(bars[k].t + bar_ms, bars[k].c, "end_of_data", k, False,
                  realized=realized, size_left=size_left)



# ─────────────────────────────────────────────────────────────────────────────
# 单币回放
# ─────────────────────────────────────────────────────────────────────────────

_OPP = {"long": "short", "short": "long"}

# 实验臂 → 信号源臂（baseline 派生）与 DSL 键（main 注入 dsls dict）
DERIVED_ARMS = ["dsl_t1", "dsl_t2", "fade_live", "fade_tuned",
                "filt", "filt_s60", "pullback", "filt_ld", "filt_ra",
                "filt_exch", "filt_ra_exch"]
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


def _dispatch_arms_1h(coin, per_arm, bars5m, h1, dsls, notional, e_slip,
                      x_slip, stop_delay, P, regime_at, atr4h_at, funnel):
    """C-7：信号去重成「每根 1h 每臂一个」后，在 1h 序列上回放出场/持仓。

    与 5m 路径共用 _simulate_trade（bar_ms=MS_1H），仅 bar 序列与信号密度不同；
    生产语义（杠杆有效止损、交易所镜像 SL/TP、逐笔 sizing、per-coin 滑点）全部不变。
    单仓：同一臂一笔持仓未平前，后续信号跳过（复刻 5m 的 open_until 占用规则）。
    """
    ts1h = [c.t for c in h1]
    trades: List[Trade] = []
    for arm, cands in per_arm.items():
        # 5m 决策时点 -> 成交 1h bar 索引（第一根 open >= 决策时点的 1h）。
        # 同一成交 bar 只保留决策最晚的候选；cands 已按 i 升序，后者覆盖前者。
        by_h: Dict[int, Any] = {}
        for i5, cand in cands:
            decision_ms = bars5m[i5].t + MS_5M
            h = bisect.bisect_left(ts1h, decision_ms)  # 该 1h open >= 决策时点
            if h >= len(h1):
                continue
            by_h[h] = cand  # 升序覆盖 → 保留同 bar 最晚信号
        arm_dsl = dsls.get(arm, dsls["live"])
        open_until_h = -1
        for h in sorted(by_h):
            if h <= open_until_h:
                funnel["skip_open_pos"] += 1
                continue
            cand = by_h[h]
            _dsl = arm_dsl
            if arm in ("filt_ra", "filt_ra_exch"):
                _dsl = (dsls["ra_trend"]
                        if regime_at(h1[h].t) in ("up", "down")
                        else dsls["ra_nontrend"])
            _exch = P.get("exch") if arm in ("filt_exch", "filt_ra_exch") else None
            _atr = atr4h_at(h1[h].t) if _exch else 0.0
            _sizing = P.get("sizing") if _exch else None
            # 1h 限价挂单有效期：5m 的 24 根(2h) = 2 根 1h
            pb_kw = ({"pullback_pct": PULLBACK_PCT, "pullback_bars": 2}
                     if arm == "pullback" else {})
            tr = _simulate_trade(cand, h1, h - 1, _dsl, notional, e_slip,
                                 x_slip, stop_delay, coin, exch=_exch,
                                 atr_abs=_atr, sizing=_sizing,
                                 bar_ms=MS_1H, **pb_kw)
            if tr is None:
                continue
            trades.append(tr)
            open_until_h = h + tr.hold_bars
    return trades, funnel


def replay_coin(coin: str, start_ms: int, end_ms: int, P: Dict[str, Any],
                dsls: Dict[str, DslParams], notional: float,
                slips: Tuple[float, float, float],
                source: str = "hyperliquid",
                per_coin_slip: bool = True,
                exit_interval: str = "5m",
                ) -> Tuple[List[Trade], Dict[str, int]]:
    funnel: Dict[str, int] = {
        "bars": 0, "fired_ge_1": 0, "surfaced": 0, "no_dir": 0,
        "le_block": 0, "hq_would": 0, "sigma_would": 0,
        "sigma_no_dir": 0, "sigma_le_block": 0,
        "skip_open_pos": 0, "fetch_error": 0,
    }
    trades: List[Trade] = []
    # P5-1d：按币解析半价差滑点（全池一个常数会同时高估大币、低估小币）
    e_slip, x_slip, stop_delay = slips
    if per_coin_slip:
        e_slip = _slip_for(coin, e_slip, True)
        x_slip = _slip_for(coin, x_slip, True)
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
    # P6b-1a：交易所侧触发单用【4h ATR(14)】（executor._price_atr_guard →
    # get_hl_atr("4h",14,coin)）。用 5m ATR 会把止损宽度和 TP 目标都算错。
    _atr4h = _atr_series(c4h_all, 14) if c4h_all else []

    # ── P6-RA：实盘 regime 用 BTC 1h 代理（market_regime.CRYPTO_PROXY="BTC"），
    # 全局共享、与当前币无关。回测若用「当前币的 1h」判 regime 就是代理失真。
    try:
        _btc_1h = fetch("BTC", "1h", start_ms - 170 * MS_1H, end_ms, MS_1H)
        _btc_ts = [c.t for c in _btc_1h]
    except Exception:
        _btc_1h, _btc_ts = [], []

    def _regime_at(decision_ms: int) -> str:
        """复现 detect_regime_with_score('BTC')：取末 100 根【已收盘】1h →
        classify_candles（与实盘同一函数、同一参数）。"""
        if len(_btc_1h) < 100:
            return "neutral"
        k = bisect.bisect_right(_btc_ts, decision_ms - MS_1H)
        if k < 100:
            return "neutral"
        sl = _btc_1h[k - 100:k]
        try:
            rg = P["regime"]
            return classify_candles(sl, fast_p=rg.get("fast_ema"),
                                    slow_p=rg.get("slow_ema"),
                                    slope_up=rg.get("slope_threshold"),
                                    adx_max=rg.get("chop_adx_max"))
        except Exception:
            return "neutral"

    def _atr4h_at(decision_ms: int) -> float:
        """复现 get_hl_atr("4h", 14, coin)：最后一根【已收盘】4h bar 的 ATR。"""
        if not _atr4h:
            return 0.0
        k = bisect.bisect_right(ts_4h, decision_ms - MS_4H)
        if k < 15 or k > len(_atr4h):
            return 0.0
        v = _atr4h[k - 1]
        return v if (v == v and v > 0) else 0.0  # NaN / 非正 → 0

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
                    per_arm["filt_ld"].append((i, replace(cand, arm="filt_ld")))
                    # P6-RA：同信号、同过滤，出场按 BTC regime 动态切换
                    per_arm["filt_ra"].append((i, replace(cand, arm="filt_ra")))
                    # P6b-1a：同信号、同过滤，额外建模交易所侧 SL/TP 触发单
                    per_arm["filt_exch"].append(
                        (i, replace(cand, arm="filt_exch")))
                    per_arm["filt_ra_exch"].append(
                        (i, replace(cand, arm="filt_ra_exch")))
                    if cand.score >= 60:
                        per_arm["filt_s60"].append(
                            (i, replace(cand, arm="filt_s60")))

    # ── C-7：出场/持仓迁 1h（信号仍是上面 5m 引擎的产物）──────────────────
    # PIT 对齐：每个候选的成交 1h bar = open 时间 >= 5m 决策时点(bars[i].t+5m)
    # 的第一根 1h（即信号确定后才开盘的那一根，绝不用包含信号前价格的当根）。
    # 同一根成交 1h bar 内的多个 5m 信号去重，每臂只保留决策最晚的一个；
    # _simulate_trade 约定「bar i 收盘出信号、i+1 开盘成交」，故传 h_idx-1。
    if exit_interval == "1h":
        return _dispatch_arms_1h(
            coin, per_arm, bars, c1h_all, dsls, notional, e_slip, x_slip,
            stop_delay, P, _regime_at, _atr4h_at, funnel)

    for arm, cands in per_arm.items():
        arm_dsl = dsls.get(arm, dsls["live"])
        pb_kw = ({"pullback_pct": PULLBACK_PCT, "pullback_bars": PULLBACK_BARS}
                 if arm == "pullback" else {})
        open_until = -1  # 持仓覆盖到的最后一根 bar idx
        for i, cand in cands:
            if i <= open_until:
                funnel["skip_open_pos"] += 1
                continue
            _dsl = arm_dsl
            if arm in ("filt_ra", "filt_ra_exch"):
                # P6-RA：复现 select_exit_params —— trend(up/down) 走 trend_ride
                # （宽追踪 + 4.0% 止损天花板），neutral/chop 走 scalp（+1.5% 止损）。
                _dsl = (dsls["ra_trend"]
                        if _regime_at(bars[i].t + MS_5M) in ("up", "down")
                        else dsls["ra_nontrend"])
            # P6b-1a：交易所侧触发单（仅 *_exch 臂）
            _exch = (P.get("exch")
                     if arm in ("filt_exch", "filt_ra_exch") else None)
            _atr_abs = _atr4h_at(bars[i].t + MS_5M) if _exch else 0.0
            _sizing = P.get("sizing") if _exch else None
            tr = _simulate_trade(cand, bars, i, _dsl, notional,
                                 e_slip, x_slip, stop_delay, coin,
                                 exch=_exch, atr_abs=_atr_abs,
                                 sizing=_sizing, **pb_kw)
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
        "avg_hold_h": sum(t.exit_t - t.entry_t for t in trades) / 3_600_000 / n,
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
        ("pullback", "E4 限价-0.4%挂单(24根)"),
        ("filt_ld", "E3c 过滤+实盘DSL出场"),
        ("filt_ra", "E3d 过滤+regime感知出场"),
        ("filt_exch", "E3e 过滤+tuned+交易所侧单"),
        ("filt_ra_exch", "E3f 过滤+regime+交易所侧单")]


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
    global ROUND_TRIP_FEE_BPS
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
    # ── P6b-1a：实盘逐笔 sizing（*_exch 臂）──────────────────────────
    # 固定 $10,000 会让交易所侧 TP 腿的 upsize 永不触发（意图额 $4,000 远大于
    # 最小额 $10.5），从而把实盘 52.6% 的 exchange_trigger 抹成 0。
    ap.add_argument("--live-sizing", action="store_true",
                    help="*_exch 臂改用实盘逐笔 notional"
                         "（atr_equal_risk_notional + tiered cap）")
    ap.add_argument("--equity", type=float, default=30.58,
                    help="实盘净值，默认 $30.58")
    ap.add_argument("--risk-per-trade-pct", type=float, default=0.026,
                    help="atr_risk_sizing.risk_per_trade_pct，默认 0.026")
    ap.add_argument("--max-notional", type=float, default=30.0,
                    help="max_trade_notional_usd（tiered cap base），默认 30")
    ap.add_argument("--fee-bps", type=float, default=ROUND_TRIP_FEE_BPS,
                    help="往返手续费（bps）。默认 8.64 = 链上实测 HL IOC taker 4.32×2")
    ap.add_argument("--entry-slip-bps", type=float, default=DEFAULT_ENTRY_SLIP_BPS,
                    help="入场滑点（bps）。per-coin 模式下仅作未列币的回退值")
    ap.add_argument("--exit-slip-bps", type=float, default=DEFAULT_EXIT_SLIP_BPS,
                    help="出场滑点（bps）。per-coin 模式下仅作未列币的回退值")
    ap.add_argument("--stop-delay-bps", type=float, default=DEFAULT_STOP_DELAY_SLIP_BPS,
                    help="max_loss 止损的额外延迟滑点（bps）。实测 0.0")
    ap.add_argument("--slip-mode", choices=("per-coin", "flat"), default="per-coin",
                    help="per-coin（默认）：按 PER_COIN_SLIP_BPS 逐币半价差（P5-1d）；"
                         "flat：全池统一用 --entry/exit-slip-bps（复现旧口径用）")
    ap.add_argument("--exit-interval", choices=("5m", "1h"), default="5m",
                    help="C-7：信号仍在 5m 引擎上生成，但出场/持仓回放迁到该周期。"
                         "1h 时每个 1h bar 每臂只取该小时内最后一个通过过滤的 5m 信号"
                         "（PIT 对齐，无混频未来函数），成交在下一 1h 开盘。默认 5m=历史口径。")
    ap.add_argument("--write", action="store_true",
                    help="写隔离产物 JSONL（默认 dry-run 只打印汇总）")
    ap.add_argument("--out", default=str(_REPO / "logs" / "backtest_majors_surge_experiments.jsonl"))
    args = ap.parse_args()

    ROUND_TRIP_FEE_BPS = args.fee_bps   # P5-1b：实测成本常数可 CLI 覆盖

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    cfg, cfg_src = _load_config(args.config)
    P = _resolve_params(cfg)

    # B-guard：回测生产可比性，调度前硬校验（不满足直接报错退出，不产结果）。
    # ① 成本表齐全度：币池必须全部有逐币半价差，否则缺失币静默回退 0.31bps
    #    低估成本；② 杠杆一致性：回测杠杆必须等于生产 leverage。
    from hermes_trader.backtest import guard as _guard
    _guard.assert_cost_table_complete(coins, list(PER_COIN_SLIP_BPS.keys()))
    # P6b-1a：实盘逐笔 sizing（--live-sizing 时对 *_exch 臂生效）
    P["sizing"] = ({
        "risk_per_trade_pct": float(args.risk_per_trade_pct),
        "equity": float(args.equity),
        "max_notional_usd": float(args.max_notional),
        "sl_atr_mult": float(P["exch"].get("sl_atr_mult", 1.2)),
    } if args.live_sizing else None)
    # B-1a-改③：逐笔带杠杆。实盘权威配置顶层 leverage（生产=10），ROE cap 在
    # dsl_exit.max_loss_roe_pct（生产=15）；注入副本供 DslParams.from_config 读取。
    _live_leverage = float(cfg.get("leverage", 1) or 1)
    # B-guard② 杠杆一致性：配置杠杆须为生产整数倍且等于 LIVE_LEVERAGE。
    if _live_leverage != int(_live_leverage):
        raise ValueError(f"config leverage={_live_leverage} 非整数倍，无法对齐生产口径")
    _guard.assert_leverage_allowed(int(_live_leverage))
    _dsl_cfg_for_params = dict(P["dsl"])
    _dsl_cfg_for_params["_leverage"] = _live_leverage
    live_dsl = DslParams.from_config(_dsl_cfg_for_params)
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
        "pullback": tuned, "filt_ld": live_dsl,
        # P6b-1a：与 filt / filt_ra 同出场，仅多一层交易所侧触发单
        # （filt_ra_exch 的出场在调用点按 regime 覆盖）
        "filt_exch": tuned, "filt_ra_exch": tuned,
    }

    # ── P6-RA：复现实盘 select_exit_params（regime_aware.enabled=True）──
    # 实盘出场是 regime 相关的：trend(up/down) → trend_ride（protect 2.5 /
    # retrace 0.4 / 止损 4.0% 天花板）；non_trend(neutral/chop) → scalp（顶层
    # protect/retrace + 止损 1.5%）。顶层 max_loss_pct=1 在 regime_aware
    # 开启时【永远用不到】——A 组（全程 1%）与 B 组（全程 1.5%）都是极端假设。
    # clocks.enabled=False → hard/stale 用顶层全局值。
    _dx = cfg.get("dsl_exit", {}) or {}
    _ra = _dx.get("regime_aware", {}) or {}
    _tr = _ra.get("trend_ride", {}) or {}
    _ml = _ra.get("max_loss", {}) or {}
    _tr_ml = _ml.get("trend", {}) or {}
    _nt_ml = _ml.get("non_trend", {}) or {}
    _tr_tiers = sorted(
        (float(t["pct_above_entry"]), float(t["retrace_threshold"]))
        for t in (_tr.get("phase2_tiers") or [])) or None
    dsls["ra_trend"] = DslParams(
        max_loss_pct=float(_tr_ml.get("max_loss_pct", 4.0)),
        protect_pct=float(_tr.get("protect_pct", 2.5)),
        retrace_threshold=float(_tr.get("retrace_threshold", 0.4)),
        hard_timeout_minutes=float(_dx.get("hard_timeout_minutes", 600)),
        breakeven_trigger_pct=float(_dx.get("breakeven_trigger_pct", 2.5)),
        breakeven_lock_pct=float(_dx.get("breakeven_lock_pct", 0.5)),
        stale_flat_timeout_minutes=float(_dx.get("stale_flat_timeout_minutes", 90)),
        phase2_tiers=_tr_tiers or live_dsl.phase2_tiers,
        # B-1a-改③④：杠杆/ROE/ATR 与 live 同源（只 regime cap 不同）
        leverage=live_dsl.leverage,
        max_loss_roe_pct=live_dsl.max_loss_roe_pct,
        atr_stop_enabled=live_dsl.atr_stop_enabled,
        entry_atr_pct=live_dsl.entry_atr_pct,
        atr_mult=live_dsl.atr_mult,
        atr_floor_pct=live_dsl.atr_floor_pct,
        atr_ceiling_pct=live_dsl.atr_ceiling_pct,
    )
    # non_trend = 顶层 protect/retrace/tiers，仅覆盖 max_loss
    dsls["ra_nontrend"] = replace(
        live_dsl, max_loss_pct=float(_nt_ml.get("max_loss_pct", 1.5)))
    dsls["filt_ra"] = dsls["ra_nontrend"]  # 兜底；实际在调用点按 regime 选

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
            source=args.source, per_coin_slip=(args.slip_mode == "per-coin"),
            exit_interval=args.exit_interval)
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
                "signal_interval": "5m", "exit_interval": args.exit_interval,
            "arms": [k for k, _ in ARMS],
                "days": args.days, "start_ms": start_ms, "end_ms": end_ms,
                "notional": args.notional,
                "fees_bps": ROUND_TRIP_FEE_BPS,
                "slips_bps": [args.entry_slip_bps, args.exit_slip_bps,
                              args.stop_delay_bps],
                "slip_mode": args.slip_mode,
                "per_coin_slip_bps": (PER_COIN_SLIP_BPS
                                      if args.slip_mode == "per-coin" else None),
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
                    "notional": round(t.notional, 4),
                    "peak_pct": round(t.peak_pct, 4), "score": t.score,
                    "fired": t.fired, "meta": t.meta,
                }) + "\n")
        print(f"\n[write] {len(all_trades)} trades → {out_path}")
    else:
        print("\n[dry-run] 未写产物（--write 落隔离 JSONL）")


if __name__ == "__main__":
    main()
