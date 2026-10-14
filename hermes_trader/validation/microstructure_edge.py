"""M-1 微结构 edge 的研究统计原语（纯函数，标准库，零第三方依赖）。

本模块只服务于 ``docs/research/2026-09/m1_l2_book_capture_prereg_2026-09-28.md``
预先冻结的 H1（OFI → 短周期未来 mid 收益），在看数之前固定的统计方法上
补齐两块 Vibe-Trading ``bench_runner_strict`` 同源的研究纪律：

1. **Kyle (1985) 价格冲击回归带截距**（:func:`kyles_lambda_with_intercept`）。
   ``ΔP = α + λ·OrderFlow`` 必须含截距：带符号订单流在真实窗口里很少零均值，
   强行过原点会把斜率偏置 ``α·mean(flow)/var(flow)``。本函数同时返回过原点
   斜率作为对照，便于量化该偏置的大小。

2. **同宇宙随机对照（same-universe random control）**
   （:func:`paired_t_against_random` / :func:`permute_flow_within_bars`）。
   信号 IC 对零检验可能只证明了"这批 bar / 这批币同涨同跌"。做法是在**每个
   时间块（bar）内部对 OFI 做符号置换**——保留该 bar 的量级分布、笔数与总
   绝对流量，只摧毁"OFI → 收益"的对应关系——多 seed 得到随机基线 IC，再对
   逐日配对差 ``α_t = IC_signal − IC_random`` 做 t 检验，并分四档：
   ``confirmed_alive / train_only / reversed_strict / noise``。

   为什么是"bar 内符号置换"而不是跨时间打乱：M-1 的推断单位是时间块（同一
   时刻跨币相关，见预注册 §3），在块内置换能保留横截面量级结构与成交量的
   时间结构，是比"打乱收益"更严格的同宇宙零假设。

所有函数不做任何 I/O、不读时钟、不改输入，调用方（研究脚本）负责装载
book-raw/tape 与落盘。判定阈值（如 |t|≥3.5 的 Harvey-Liu-Zhu 多重检验口径）
由调用方按预注册传入，本模块不擅自决定策略结论。
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass

# Harvey, Liu & Zhu (2016)：对"因子动物园"做多重检验校正后，发表门槛的中位
# |t| 约 3.5（而非 2.0）。这是同宇宙对照的保守通过线，调用方可覆盖。
HARVEY_MIN_ABS_T = 3.5
# OOS 符号翻转为强反号的阈值（alpha_t < -该值 即 reversed_strict）。
REVERSED_ABS_T = 2.0


# ── Cont et al. (2014) OFI ──────────────────────────────────────────────────

def ofi_cont_best_level(
    prev_bid_px: float,
    prev_bid_sz: float,
    prev_ask_px: float,
    prev_ask_sz: float,
    bid_px: float,
    bid_sz: float,
    ask_px: float,
    ask_sz: float,
) -> float:
    """best-level 订单流失衡（Cont 等 2014 式 (3)，单帧增量 e_n）。

    ::

        e_n = 1[Pb_n >= Pb_{n-1}] Qb_n  - 1[Pb_n <= Pb_{n-1}] Qb_{n-1}
            - 1[Pa_n <= Pa_{n-1}] Qa_n  + 1[Pa_n >= Pa_{n-1}] Qa_{n-1}

    价格不变时退化为该档挂量变化 ΔQ。买入（bid 堆积 / ask 撤量）为正。
    输入价格为正、挂量非负；非法输入抛错而不是静默返回 0（0 会伪装成中性）。
    """
    if min(prev_bid_px, prev_ask_px, bid_px, ask_px) <= 0.0:
        raise ValueError("prices must be positive")
    if min(prev_bid_sz, prev_ask_sz, bid_sz, ask_sz) < 0.0:
        raise ValueError("sizes must be non-negative")
    e_bid = (bid_sz if bid_px >= prev_bid_px else 0.0) - (
        prev_bid_sz if bid_px <= prev_bid_px else 0.0
    )
    e_ask = (ask_sz if ask_px <= prev_ask_px else 0.0) - (
        prev_ask_sz if ask_px >= prev_ask_px else 0.0
    )
    return e_bid - e_ask


def ofi_depth(
    prev_bids: list[tuple[float, float]],
    prev_asks: list[tuple[float, float]],
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    levels: int = 10,
) -> float:
    """OFI-k：把 best-level 的 Cont (2014) 公式推广到前 ``levels`` 档。

    对每个档级 k，比较前后两帧的**第 k 档**，套用与 best-level 完全相同的
    四个指示项（缺失档视为"价位不存在"、挂量 0）：

    ::

        e_bid,k = 1[Pb_k >= Pb'_k] Qb_k - 1[Pb_k <= Pb'_k] Qb'_k
        e_ask,k = 1[Pa_k <= Pa'_k] Qa_k - 1[Pa_k >= Pa'_k] Qa'_k

    旧价位退出 / 新价位进入通过 Q=0 自然得到 -Q' / +Q，无需特判。档位按排序后
    的第 k 档对齐（调用方传入已按价格排序、bid 降 ask 升的档位）。各档以预注册
    固定权重 w_k = 1/k 衰减求和，**不优化权重**。
    """
    def _at(book: list[tuple[float, float]], i: int):
        return book[i] if i < len(book) else (None, 0.0)

    total = 0.0
    for k in range(levels):
        w = 1.0 / (k + 1)
        pp, pq = _at(prev_bids, k)
        cp, cq = _at(bids, k)
        total += w * _ofi_one_side(pp, pq, cp, cq, bid_side=True)
        ap_p, aq_p = _at(prev_asks, k)
        ap_c, aq_c = _at(asks, k)
        total -= w * _ofi_one_side(ap_p, aq_p, ap_c, aq_c, bid_side=False)
    return total


def _ofi_one_side(prev_px, prev_sz, px, sz, *, bid_side: bool) -> float:
    """单档单边 OFI 项，严格同构于 best-level Cont 公式。

    ``bid_side=True``（bid）：``1[P>=P']Q − 1[P<=P']Q'``；
    ``bid_side=False``（ask）方向取反：``1[P<=P']Q − 1[P>=P']Q'``。
    缺失价位（None）只让相应指示项为 0：旧档缺失即"新档进入"，当前档缺失即
    "旧档退出"。无 None/None 并存的歧义（_at 保证至少一侧有价格或都缺失）。
    """
    if bid_side:
        first = sz if (px is not None and (prev_px is None or px >= prev_px)) else 0.0
        second = prev_sz if (prev_px is not None and (px is None or px <= prev_px)) else 0.0
    else:
        first = sz if (px is not None and (prev_px is None or px <= prev_px)) else 0.0
        second = prev_sz if (prev_px is not None and (px is None or px >= prev_px)) else 0.0
    return first - second


# ── Kyle's lambda：必须带截距的 OLS ─────────────────────────────────────────

@dataclass(frozen=True)
class KyleResult:
    """ΔP = α + λ·flow 回归结果。"""

    alpha: float
    lam_with_intercept: float
    lam_through_origin: float
    slope_bias_from_origin: float
    n: int
    r_squared: float


def kyles_lambda_with_intercept(
    flows: list[float], delta_px: list[float]
) -> KyleResult:
    """估计 Kyle (1985) 价格冲击系数 λ（**带截距** OLS），并给过原点对照。

    - ``lam_with_intercept`` 是无偏口径；``lam_through_origin`` 仅作对照，
      二者之差 ``slope_bias_from_origin`` 即 α·mean(flow)/var(flow) 的实证大小，
      用来证明显式截距是否必要。
    - 非有限输入抛错；``n<2`` 或 flow 无变异时抛错（不静默返回 0，避免给出
      一个看起来"无冲击"的谎）。
    """
    if len(flows) != len(delta_px):
        raise ValueError("flows and delta_px length mismatch")
    n = len(flows)
    if n < 2:
        raise ValueError("kyles_lambda needs >= 2 observations")
    if any(not math.isfinite(x) for x in flows + delta_px):
        raise ValueError("non-finite input")
    mx = statistics.fmean(flows)
    my = statistics.fmean(delta_px)
    sxx = sum((x - mx) ** 2 for x in flows)
    if sxx <= 0.0:
        raise ValueError("order flow has zero variance")
    sxy = sum((x - mx) * (y - my) for x, y in zip(flows, delta_px))
    lam = sxy / sxx
    alpha = my - lam * mx
    # 过原点（有偏对照）：λ0 = Σxy / Σx²
    sxx0 = sum(x * x for x in flows)
    lam0 = sum(x * y for x, y in zip(flows, delta_px)) / sxx0
    # R²（带截距模型）
    ss_res = sum((y - (alpha + lam * x)) ** 2 for x, y in zip(flows, delta_px))
    ss_tot = sum((y - my) ** 2 for y in delta_px)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else 0.0
    return KyleResult(
        alpha=alpha,
        lam_with_intercept=lam,
        lam_through_origin=lam0,
        slope_bias_from_origin=lam0 - lam,
        n=n,
        r_squared=r2,
    )


# ── 同宇宙随机对照（bar 内符号置换）─────────────────────────────────────────

def permute_flow_within_bars(
    bars: list[list[float]], *, seed: int
) -> list[list[float]]:
    """对每个时间块（bar）内部的 OFI 做**符号置换**。

    对块内每个观测独立地以 50/50 翻转符号，从而：

    - 保留每笔 |OFI| 的量级分布与笔数（块内总量级、成交量时间结构不变）；
    - 摧毁"OFI 符号 → 未来收益符号"的映射，且块内净流量的期望归零；
    - 不跨块/跨币移动数据，保留预注册 §3 所说的"同一时刻跨币相关结构"。

    这是比跨时间 shuffle 更严格的同宇宙零假设。确定性 ``seed`` 保证可复现。
    """
    rng = random.Random(seed)
    out: list[list[float]] = []
    for bar in bars:
        out.append([abs(v) if rng.random() < 0.5 else -abs(v) for v in bar])
    return out


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 2 or len(y) != n:
        raise ValueError("pearson needs equal-length >=2 series")
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx <= 0.0 or sy <= 0.0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def paired_t_against_random(
    signal_by_bar: list[list[float]],
    fwd_ret_by_bar: list[list[float]],
    *,
    n_seeds: int = 5,
    base_seed: int = 20260928,
    oos_signal: list[list[float]] | None = None,
    oos_fwd_ret: list[list[float]] | None = None,
    min_abs_t: float = HARVEY_MIN_ABS_T,
) -> dict[str, object]:
    """同宇宙随机对照检验（keyword-only 安全参数，见下方参数说明）。

    步骤：
      1. 信号 IC：每个 bar 内 OFI 与未来收益的 Pearson 相关，按 bar 等权平均；
      2. 随机 IC：对 OFI 做 :func:`permute_flow_within_bars`，``n_seeds`` 个
         seed 各算一次 IC 后取逐日（逐 bar）均值，得到同宇宙随机基线；
      3. 配对差 ``α_b = IC_signal_b − IC_random_b``，对该配对序列做单样本 t；
      4. 分四档（与 Vibe ``bench_runner_strict`` 同词表）：

         - ``confirmed_alive``：全样本 α_t ≥ min_abs_t；若提供 OOS，则 OOS
           α_t 也必须 ≥ min_abs_t（否则降为 train_only）；
         - ``reversed_strict``：α_t ≤ −REVERSED_ABS_T（OOS 翻号是最强的
           "训练期 alpha 系人为产物"信号，单独暴露）；
         - ``noise``：|α_t| 在 REVERSED_ABS_T 与 min_abs_t 之间，与随机不可分；
         - ``train_only``：训练过线、OOS 掉下来。

    返回信号 IC、随机 IC、配对 alpha 序列、t、p（双尾近似）、分类。
    """
    if len(signal_by_bar) != len(fwd_ret_by_bar) or not signal_by_bar:
        raise ValueError("signal/fwd bars length mismatch or empty")
    if n_seeds < 1:
        raise ValueError("n_seeds must be >= 1")

    def _bar_ics(signal_bars, ret_bars):
        ics = []
        for sb, rb in zip(signal_bars, ret_bars):
            if len(sb) != len(rb) or len(sb) < 2:
                continue
            ics.append(_pearson(sb, rb))
        return ics

    sig_ic = _bar_ics(signal_by_bar, fwd_ret_by_bar)
    n_bars = len(sig_ic)
    if n_bars < 2:
        raise ValueError("need >= 2 evaluable bars")

    # 每个 seed 一串 bar IC；跨 seed 对齐到同一 bar 后取均值。
    rand_ic_seeds: list[list[float]] = []
    for s in range(n_seeds):
        perm = permute_flow_within_bars(signal_by_bar, seed=base_seed + s)
        rand_ic_seeds.append(_bar_ics(perm, fwd_ret_by_bar))
    rand_ic = [
        statistics.fmean(rand_ic_seeds[s][b] for s in range(n_seeds))
        for b in range(n_bars)
    ]
    alpha = [a - b for a, b in zip(sig_ic, rand_ic)]
    t_full, p_full = _one_sample_t(alpha)
    category = _categorize(t_full, None)

    oos = None
    if oos_signal is not None and oos_fwd_ret is not None:
        oos_sig = _bar_ics(oos_signal, oos_fwd_ret)
        oos_rand_seeds = [
            _bar_ics(
                permute_flow_within_bars(oos_signal, seed=base_seed + 10_000 + s),
                oos_fwd_ret,
            )
            for s in range(n_seeds)
        ]
        oos_rand = [
            statistics.fmean(oos_rand_seeds[s][b] for s in range(n_seeds))
            for b in range(len(oos_sig))
        ]
        oos_alpha = [a - b for a, b in zip(oos_sig, oos_rand)]
        t_oos, p_oos = _one_sample_t(oos_alpha)
        category = _categorize(t_full, t_oos, min_abs_t=min_abs_t)
        oos = {
            "signal_ic": statistics.fmean(oos_sig),
            "random_ic": statistics.fmean(oos_rand),
            "alpha_mean": statistics.fmean(oos_alpha),
            "t": t_oos,
            "p_two_sided": p_oos,
            "bars": len(oos_sig),
        }

    return {
        "signal_ic": statistics.fmean(sig_ic),
        "random_ic": statistics.fmean(rand_ic),
        "alpha_mean": statistics.fmean(alpha),
        "t": t_full,
        "p_two_sided": p_full,
        "bars": n_bars,
        "n_seeds": n_seeds,
        "category": category,
        "oos": oos,
    }


def _one_sample_t(x: list[float]) -> tuple[float, float]:
    n = len(x)
    if n < 2:
        raise ValueError("t needs >= 2 samples")
    m = statistics.fmean(x)
    sd = statistics.stdev(x)
    if sd <= 0.0:
        # 完全无变异：均值非零即极端、均值为零即不显著。用大/零 t 表达。
        return (math.inf if m > 0 else -math.inf if m < 0 else 0.0), 0.0
    t = m / (sd / math.sqrt(n))
    return t, _student_t_two_sided_p(t, n - 1)


def _student_t_two_sided_p(t: float, df: int) -> float:
    """Student-t 双尾 p 的正则化不完全 Beta 近似（标准库，无 scipy）。"""
    if not math.isfinite(t):
        return 0.0
    x = df / (df + t * t)
    # I_x(df/2, 1/2) ；双尾 p = I_{ν/(ν+t²)}(ν/2, 1/2)
    return _betainc(df / 2.0, 0.5, x)


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 B_x(a,b)/B(a,b)，连分数展开（Numerical Recipes）。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float, itmax: int = 200, eps: float = 3e-12) -> float:
    qab, qap, qam = a + b, 1.0 + a, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _categorize(
    t_full: float,
    t_oos: float | None,
    *,
    min_abs_t: float = HARVEY_MIN_ABS_T,
) -> str:
    if t_full <= -REVERSED_ABS_T or (t_oos is not None and t_oos <= -REVERSED_ABS_T):
        return "reversed_strict"
    full_pass = t_full >= min_abs_t
    if t_oos is None:
        return "confirmed_alive" if full_pass else "noise"
    oos_pass = t_oos >= min_abs_t
    if full_pass and oos_pass:
        return "confirmed_alive"
    if full_pass and not oos_pass:
        return "train_only"
    return "noise"
