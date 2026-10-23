#!/usr/bin/env python3
"""M-1 H1 OFI 样本外检验脚本（严格按 2026-09-28 预注册口径）。

纪律
----
* 预注册 §2：首次分析**不早于 2026-10-28**。本脚本内置日期闸，未到日期
  默认拒绝执行（--i-know-its-early 仅供管线联调，且不得用于报告结论）。
* 不做参数搜索：窗口 W=60s（300s 仅稳健性）、horizon h=300s（15m 仅稳健性）、
  深度权重 1/i、分位带 [0,10)/(90,100]、train/test 前后各半、去重叠步进=h。
* 主检验：可执行**净 bps/笔**的 95% block-bootstrap CI（B=10000，
  seed=20260928，块长 15m，另报 1h 块），通过判据全部满足才算 H1 通过。

数据：${HERMES_DATA_DIR:-/data}/book-raw/date=YYYY-MM-DD/COIN.jsonl[.gz]
标签用 mid；可执行口径=按 t 当根 ask 买、t+h 当根 bid 卖，扣 taker 费 +
开仓 cross 半价差（均取实测值）。

退出码（§7.7.11）：0=H1 通过；1=证伪；2=数据/日期闸不满足；3=样本不足
（n<300，延长采集，不可判定）。

本脚本为研究工具，不进交易运行时路径；仅因宿主机读不到 /data 卷才随
镜像分发，供容器内一次性执行。
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_trader.agents import ofi as ofi_mod

# ── 预注册固定常量（不得在此调整）────────────────────────────────────────
W_PRIMARY_S = 60
W_ROBUST_S = 300
H_PRIMARY_S = 300
H_ROBUST_S = 900
Q_LOW, Q_HIGH = 0.10, 0.90
TAKER_FEE_BP_PER_SIDE = 4.32          # 已核实 HL 真实 taker
MIN_ECON_BP = 3.0
MIN_TRADES = 300
BOOT_B = 10000
BOOT_SEED = 20260928
BLOCK_MIN_BOOT_S = 15 * 60
BLOCK_HOUR_S = 3600
FIRST_ANALYSIS_DATE = "2026-10-28"

DAY_S = 86400
MS = 1000
HIST_S = 30 * DAY_S            # §7.2 expanding 上限 30 天
MIN_XSECTION = 5               # §7.3 补充 / §7.7.10
K_FRAC = 0.10                  # 每侧 k = floor(0.10 × n_coins)


# ── 数据加载 ────────────────────────────────────────────────────────────────

def list_day_dirs(book_root: Path) -> list[str]:
    out = []
    if not book_root.is_dir():
        return out
    for p in book_root.glob("date=*"):
        day = p.name[len("date="):]
        if p.is_dir():
            out.append(day)
    return sorted(out)


def load_coin_day(book_root: Path, day: str, coin: str) -> list[dict]:
    ddir = book_root / f"date={day}"
    for cand in (ddir / f"{coin}.jsonl", ddir / f"{coin}.jsonl.gz"):
        if cand.is_file():
            opener = gzip.open if cand.name.endswith(".gz") else open
            rows = []
            with opener(cand, "rt") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            rows.sort(key=lambda r: int(r.get("t", 0)))
            return rows
    return []


def coins_present(book_root: Path, day: str) -> set[str]:
    ddir = book_root / f"date={day}"
    out = set()
    if not ddir.is_dir():
        return out
    for p in list(ddir.glob("*.jsonl")) + list(ddir.glob("*.jsonl.gz")):
        name = p.name
        for suf in (".jsonl.gz", ".jsonl"):
            if name.endswith(suf):
                out.add(name[: -len(suf)])
                break
    return out


# ── 特征/标签（严格只用 t 及之前数据）──────────────────────────────────────

def _best_ba(r: dict):
    """返回 (bid_px, bid_sz, ask_px, ask_sz)，缺侧返回 None。"""
    bl = ofi_mod._to_levels(r.get("b"))
    al = ofi_mod._to_levels(r.get("a"))
    if not bl or not al:
        return None
    bid = max(bl)   # 最高出价
    ask = min(al)   # 最低要价
    return bid[0], bid[1], ask[0], ask[1]


def _stream_raw_points(book_root: Path, days: list[str], coin: str):
    """按时间顺序流式产出该币各日原始行（逐日逐行解压，不整文件驻留）。"""
    for day in days:
        for r in load_coin_day(book_root, day, coin):
            yield r


@dataclass(slots=True)
class SlotRec:
    """单个 h 槽、单个币的全部固定特征（槽末一帧计算，只用 t 及之前数据）。"""
    t: int
    bid: float
    ask: float
    ofi: float                    # 窗口 Σe
    n_frames: int                 # 窗口内逐秒帧数
    std_within: float             # 窗口内逐秒 e 的样本标准差（ddof=1）
    z_main: Optional[float]       # §7.2.1 主口径 z
    z_hist: Optional[float]       # 稳健性：expanding-30d 历史标准差分母
    rv_slot: float                # §7.7.3 槽内路径 RV
    rv_30m: Optional[float]       # §7.7.6 最近 6 个槽末 mid 的 RV
    di: Optional[float]           # depth_imbalance（§1 H2 特征）
    slope_diff: Optional[float]   # §7.7.8 depth_slope_diff
    di_extreme: int               # -1 低带 / +1 高带 / 0 不在带
    rv_narrow: bool               # RV_30m <= expanding 30 分位


def _prune_sorted(history, sorted_vals: list[float], cutoff: int) -> None:
    """按 30 天窗口淘汰历史值，并同步删除排序副本中的同一元素。"""
    while history and history[0][0] < cutoff:
        _, v = history.popleft()
        i = bisect.bisect_left(sorted_vals, v)
        if i < len(sorted_vals) and sorted_vals[i] == v:
            del sorted_vals[i]


def build_coin_slots(book_root: Path, days: list[str], coin: str, *,
                     w_s: int, h_s: int) -> list[SlotRec]:
    """逐币流式构建每个 h 槽的固定特征（逐日逐行解压处理）。

    §7.2：所有分位/z/历史统计只用**严格早于当前槽**的数据。
    跨日重置 prev_snap（不拼跨日相邻帧增量），槽号 slot=t//h_s 跨日连续。

    内存说明：单日原始行经 load_coin_day 整日驻留（约 86k dict/活跃币日），
    帧级 ring 仅保留 w_s+h_s 秒；输出为每槽一条 SlotRec。全量提速见
    build_coin_slots_dual（两套口径共享一趟帧循环，输出逐位一致）。
    """
    from collections import deque

    ring: deque = deque()          # (t, e)，窗口可能用到的逐秒增量
    hist: deque = deque()          # (t, 窗口 Σe)，30 天内
    di_hist: deque = deque()       # (t, depth_imbalance)
    di_sorted: list[float] = []
    rv_hist: deque = deque()       # (t, RV_30m)
    rv_sorted: list[float] = []
    hist_sum = hist_sq = 0.0
    ends: deque = deque(maxlen=6)  # (slot, 槽末 mid)
    out: list[SlotRec] = []

    prev_snap = None
    cur_slot = None
    cur_frame = None
    rv_ssq = 0.0
    prev_mid = None

    def flush(slot, frame):
        nonlocal hist_sum, hist_sq
        if frame is None:
            return
        t, bid, ask, mid_px, snap = frame
        ends.append((slot, mid_px))

        win = [e for et, e in ring if et >= t - w_s]
        nf = len(win)
        ofi = float(sum(win))
        std = statistics.stdev(win) if nf >= 2 else 0.0
        z_main = ofi / std if (nf >= 10 and std > 0) else None

        n_h = len(hist)
        z_hist = None
        if n_h >= 2:
            var = (hist_sq - hist_sum * hist_sum / n_h) / (n_h - 1)
            sd = math.sqrt(var) if var > 0 else 0.0
            z_hist = ofi / sd if sd > 0 else None

        di = ofi_mod.depth_imbalance(snap)
        di_extreme = 0
        if di is not None and len(di_sorted) >= 2:
            if di <= quantile(di_sorted, Q_LOW):
                di_extreme = -1
            elif di > quantile(di_sorted, Q_HIGH):
                di_extreme = 1

        rv30 = None
        if len(ends) == 6 and all(mv > 0 for _, mv in ends):
            ms = [mv for _, mv in ends]
            rv30 = math.sqrt(sum(math.log(ms[k] / ms[k - 1]) ** 2
                                 for k in range(1, 6)))
        rv_narrow = (rv30 is not None and len(rv_sorted) >= 2
                     and rv30 <= quantile(rv_sorted, 0.30))

        out.append(SlotRec(
            t=t, bid=bid, ask=ask, ofi=ofi, n_frames=nf, std_within=std,
            z_main=z_main, z_hist=z_hist, rv_slot=math.sqrt(rv_ssq),
            rv_30m=rv30, di=di, slope_diff=ofi_mod.depth_slope_diff(snap),
            di_extreme=di_extreme, rv_narrow=rv_narrow))

        # 以下写入均严格晚于当前槽，供后续槽使用
        hist.append((t, ofi))
        hist_sum += ofi
        hist_sq += ofi * ofi
        while hist and hist[0][0] < t - HIST_S:
            _, old = hist.popleft()
            hist_sum -= old
            hist_sq -= old * old
        if di is not None:
            bisect.insort(di_sorted, di)
            di_hist.append((t, di))
            _prune_sorted(di_hist, di_sorted, t - HIST_S)
        if rv30 is not None:
            bisect.insort(rv_sorted, rv30)
            rv_hist.append((t, rv30))
            _prune_sorted(rv_hist, rv_sorted, t - HIST_S)

    for day in days:
        prev_snap = None           # 跨日不拼相邻帧增量
        for r in load_coin_day(book_root, day, coin):
            try:
                t = int(r.get("t", 0)) // MS
            except (TypeError, ValueError):
                continue
            ba = _best_ba(r)
            if ba is None:
                continue
            bp, bq, ap, aq = ba
            snap = {"b": r.get("b") or [[bp, bq]],
                    "a": r.get("a") or [[ap, aq]]}
            e = (ofi_mod.best_ofi_two_sided(prev_snap, snap)
                 if prev_snap is not None else 0.0)
            prev_snap = snap

            slot = t // h_s
            if cur_slot is not None and slot != cur_slot:
                flush(cur_slot, cur_frame)
                cur_slot, cur_frame = None, None
                rv_ssq, prev_mid = 0.0, None
            cur_slot = slot

            mid_px = (bp + ap) / 2.0
            if prev_mid is not None and prev_mid > 0 and mid_px > 0:
                rv_ssq += math.log(mid_px / prev_mid) ** 2
            prev_mid = mid_px
            cur_frame = (t, bp, ap, mid_px, snap)

            ring.append((t, e))
            # 丢弃绝不可能再进入窗口的旧 edge
            while ring and ring[0][0] < t - w_s - h_s:
                ring.popleft()

    if cur_frame is not None:
        flush(cur_slot, cur_frame)
    return out


# ── 双口径单遍建槽（性能版；与 build_coin_slots 逐位等价）────────────────────
#
# 性能事实（2026-10-23 cProfile，BTC 单日 86,092 帧）：
#   _to_levels 6.2s（每帧最多 6 次重复解析同一快照）+ json 4.1s + _best_ba 2.8s。
# 优化手段（全部保持逐位等价，不改变任何输出比特）：
#   1. 每帧 b/a 只解析一次；e 的算式逐字符复制 best_ofi 的 bid/ask 两侧公式，
#      best 取「过滤后首档」（与 _best 语义一致），bid/ask 价取 max/min（与
#      _best_ba 一致）；snap 直接保存解析后档位（_to_levels 幂等，下游一致）。
#   2. W=60/h=300 与 W=300/h=900 两套槽在同一趟帧循环里产出：ring 共享
#      （按 max(w_s+h_s) 淘汰），e/解析/json 各只做一次；每套口径独立维护
#      hist/di/rv/ends/rv_ssq/prev_mid 状态，flush 逻辑与原实现逐行一致。
# tests/test_m1_ofi_eval.py 内含 dual vs 单口径参考实现的逐 SlotRec 相等断言。

_DUAL_CONFIGS = ((W_PRIMARY_S, H_PRIMARY_S), (W_ROBUST_S, H_ROBUST_S))


def _new_slot_state() -> dict:
    from collections import deque
    return {"hist": deque(), "di_hist": deque(), "di_sorted": [],
            "rv_hist": deque(), "rv_sorted": [], "hist_sum": 0.0,
            "hist_sq": 0.0, "ends": deque(maxlen=6), "cur_slot": None,
            "cur_frame": None, "rv_ssq": 0.0, "prev_mid": None, "out": []}


def _flush_state(st: dict, slot: int, frame, w_s: int, ring) -> None:
    """与 build_coin_slots 内嵌 flush 逐行一致，仅把闭包状态换成 dict。"""
    if frame is None:
        return
    t, bid, ask, mid_px, snap = frame
    st["ends"].append((slot, mid_px))

    win = [e for et, e in ring if et >= t - w_s]
    nf = len(win)
    ofi = float(sum(win))
    std = statistics.stdev(win) if nf >= 2 else 0.0
    z_main = ofi / std if (nf >= 10 and std > 0) else None

    n_h = len(st["hist"])
    z_hist = None
    if n_h >= 2:
        var = (st["hist_sq"] - st["hist_sum"] * st["hist_sum"] / n_h) / (n_h - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        z_hist = ofi / sd if sd > 0 else None

    di = ofi_mod.depth_imbalance(snap)
    di_extreme = 0
    if di is not None and len(st["di_sorted"]) >= 2:
        if di <= quantile(st["di_sorted"], Q_LOW):
            di_extreme = -1
        elif di > quantile(st["di_sorted"], Q_HIGH):
            di_extreme = 1

    ends = st["ends"]
    rv30 = None
    if len(ends) == 6 and all(mv > 0 for _, mv in ends):
        ms = [mv for _, mv in ends]
        rv30 = math.sqrt(sum(math.log(ms[k] / ms[k - 1]) ** 2
                             for k in range(1, 6)))
    rv_narrow = (rv30 is not None and len(st["rv_sorted"]) >= 2
                 and rv30 <= quantile(st["rv_sorted"], 0.30))

    st["out"].append(SlotRec(
        t=t, bid=bid, ask=ask, ofi=ofi, n_frames=nf, std_within=std,
        z_main=z_main, z_hist=z_hist, rv_slot=math.sqrt(st["rv_ssq"]),
        rv_30m=rv30, di=di, slope_diff=ofi_mod.depth_slope_diff(snap),
        di_extreme=di_extreme, rv_narrow=rv_narrow))

    # 以下写入均严格晚于当前槽，供后续槽使用
    st["hist"].append((t, ofi))
    st["hist_sum"] += ofi
    st["hist_sq"] += ofi * ofi
    while st["hist"] and st["hist"][0][0] < t - HIST_S:
        _, old = st["hist"].popleft()
        st["hist_sum"] -= old
        st["hist_sq"] -= old * old
    if di is not None:
        bisect.insort(st["di_sorted"], di)
        st["di_hist"].append((t, di))
        _prune_sorted(st["di_hist"], st["di_sorted"], t - HIST_S)
    if rv30 is not None:
        bisect.insort(st["rv_sorted"], rv30)
        st["rv_hist"].append((t, rv30))
        _prune_sorted(st["rv_hist"], st["rv_sorted"], t - HIST_S)


def build_coin_slots_dual(book_root: Path, days: list[str], coin: str,
                          configs=_DUAL_CONFIGS) -> list[list[SlotRec]]:
    """一趟帧循环产出多套 (w_s, h_s) 口径的槽序列，与逐次调用
    build_coin_slots 的输出逐 SlotRec 相等（单测锁定）。"""
    from collections import deque

    max_span = max(w_s + h_s for w_s, h_s in configs)
    ring: deque = deque()
    states = [_new_slot_state() for _ in configs]

    for day in days:
        prev_bl = prev_al = None       # 跨日不拼相邻帧增量
        for r in load_coin_day(book_root, day, coin):
            try:
                t = int(r.get("t", 0)) // MS
            except (TypeError, ValueError):
                continue
            bl = ofi_mod._to_levels(r.get("b"))
            al = ofi_mod._to_levels(r.get("a"))
            if not bl or not al:
                continue               # prev 保持上一好帧（与原实现一致）
            bp, bq = max(bl)
            ap, aq = min(al)
            snap = {"b": bl, "a": al}  # _to_levels 幂等，下游结果一致
            if prev_bl is None:
                e = 0.0
            else:
                # 逐字符复制 ofi.best_ofi 两侧公式（best=过滤后首档）
                pbb, pqb = prev_bl[0]
                cbb, cqb = bl[0]
                e = (1.0 if cbb >= pbb else 0.0) * cqb \
                    - (1.0 if cbb <= pbb else 0.0) * pqb
                pba, pqa = prev_al[0]
                cba, cqa = al[0]
                e += (-1.0 if cba <= pba else 0.0) * cqa \
                    + (1.0 if cba >= pba else 0.0) * pqa
            prev_bl, prev_al = bl, al

            mid_px = (bp + ap) / 2.0
            for st, (w_s, h_s) in zip(states, configs):
                slot = t // h_s
                if st["cur_slot"] is not None and slot != st["cur_slot"]:
                    _flush_state(st, st["cur_slot"], st["cur_frame"], w_s, ring)
                    st["cur_slot"], st["cur_frame"] = None, None
                    st["rv_ssq"], st["prev_mid"] = 0.0, None
                st["cur_slot"] = slot
                if st["prev_mid"] is not None and st["prev_mid"] > 0 \
                        and mid_px > 0:
                    st["rv_ssq"] += math.log(mid_px / st["prev_mid"]) ** 2
                st["prev_mid"] = mid_px
                st["cur_frame"] = (t, bp, ap, mid_px, snap)

            ring.append((t, e))
            while ring and ring[0][0] < t - max_span:
                ring.popleft()

    for st, (w_s, _) in zip(states, configs):
        if st["cur_frame"] is not None:
            _flush_state(st, st["cur_slot"], st["cur_frame"], w_s, ring)
    return [st["out"] for st in states]


def _dual_one(arg) -> tuple[str, list[list[SlotRec]]]:
    """ProcessPoolExecutor worker（顶层定义以便 pickle）。"""
    book_root, days, coin = arg
    return coin, build_coin_slots_dual(book_root, days, coin)


# ── 分位（只用 train / 该时点过去数据）──────────────────────────────────────

def quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - pos) + sorted_vals[hi] * (pos - lo)


# ── block bootstrap CI ──────────────────────────────────────────────────────

def block_bootstrap_ci(vals: list[tuple[int, float]], *, block_s: int,
                       b: int, seed: int):
    """vals = (t_s, net_bp)，按时间排序。以时间块重采样，返回
    (mean, lo, hi, p)。

    §7.7.2：单侧 p = #{重采样均值 <= 0} / B，与 CI **共用同一次重采样**，
    故 lo = means[249]（B=10000 的 2.5% 位）与 p < 0.025 严格等价。
    """
    if not vals:
        return float("nan"), float("nan"), float("nan"), float("nan")
    vals = sorted(vals)
    t0 = vals[0][0]
    # 编号到时间块
    blocks: dict[int, list[float]] = defaultdict(list)
    for t, v in vals:
        blocks[(t - t0) // block_s].append(v)
    block_ids = sorted(blocks)
    block_vals = [blocks[i] for i in block_ids]
    n = len(vals)
    rng = random.Random(seed)
    means = []
    for _ in range(b):
        acc, cnt = 0.0, 0
        while cnt < n:
            chunk = block_vals[rng.randrange(len(block_vals))]
            acc += sum(chunk)
            cnt += len(chunk)
        means.append(acc / cnt)
    means.sort()
    obs = sum(v for _, v in vals) / n
    lo = means[int(0.025 * (b - 1))]
    hi = means[int(0.975 * (b - 1))]
    p = bisect.bisect_right(means, 0.0) / b
    return obs, lo, hi, p


# ── 横截面选择与主检验（§7.3 补充 / §7.7）─────────────────────────────────

def _net_bp(rec: SlotRec, fwd: SlotRec, side: int) -> float:
    """可执行净 bps：taker 双边 + 开仓 cross 半价差（§1）。"""
    if side == 1:
        gross = (fwd.bid - rec.ask) / rec.ask * 1e4
        cross = (rec.ask - rec.bid) / 2 / rec.ask * 1e4
    else:
        gross = (rec.bid - fwd.ask) / rec.bid * 1e4
        cross = (rec.ask - rec.bid) / 2 / rec.bid * 1e4
    return gross - cross - 2 * TAKER_FEE_BP_PER_SIDE


def _select(coin_slots: dict[str, list[SlotRec]], *, test_start_s: int,
            h_s: int, kind: str) -> dict:
    """先逐币时序 z，再同槽跨币排序；每侧 k = floor(0.10 × n_coins)。

    §7.3 补充 / §7.7.10：n_coins < 5 的槽不触发；无有效 z 的币不参与排序。
    kind="h1"（z_main）/ "h1_hist"（历史标准差稳健性）/ "h2"（depth_imbalance，
    额外要求时序极端带 + 窄幅；§7.7.5/§7.7.6）。
    """
    key_attr = {"h1": "z_main", "h1_hist": "z_hist", "h2": "di"}[kind]
    by_slot: dict[int, list[tuple[str, int, float]]] = defaultdict(list)
    for coin, recs in coin_slots.items():
        for i, rec in enumerate(recs):
            v = getattr(rec, key_attr)
            if v is not None:
                by_slot[rec.t // h_s].append((coin, i, v))

    trades: list[tuple[int, float]] = []
    detail: list[dict] = []
    slot_rv: dict[int, float] = {}
    n_slots = 0
    for slot in sorted(by_slot):
        if slot * h_s < test_start_s:          # 只报告 test 段
            continue
        rows = by_slot[slot]
        n = len(rows)
        slot_rv[slot] = sum(coin_slots[c][i].rv_slot for c, i, _ in rows) / n
        n_slots += 1
        if n < MIN_XSECTION:
            continue
        k = int(K_FRAC * n)                    # floor
        if k < 1:
            continue
        rows.sort(key=lambda x: x[2])
        sel = [(c, i, 1) for c, i, _ in rows[-k:]] \
            + [(c, i, -1) for c, i, _ in rows[:k]]
        for coin, i, side in sel:
            recs = coin_slots[coin]
            if i + 1 >= len(recs):
                continue
            rec, fwd = recs[i], recs[i + 1]
            if fwd.t // h_s != slot + 1:       # forward 必须是紧邻下一槽（t+h）
                continue
            if kind == "h2" and (rec.di_extreme != side or not rec.rv_narrow):
                continue
            net = _net_bp(rec, fwd, side)
            trades.append((rec.t, net))
            detail.append({"t": rec.t, "slot": slot, "coin": coin,
                           "side": side, "net_bp": net})
    return {"h_s": h_s, "trades": trades, "detail": detail,
            "slot_rv": slot_rv, "n_slots": n_slots}


def _boot(trades):
    """15m 主块 + 1h 稳健块；p 与 CI 共用同一次重采样（§7.7.2）。"""
    if not trades:
        return None
    return {
        "15m": block_bootstrap_ci(trades, block_s=BLOCK_MIN_BOOT_S,
                                  b=BOOT_B, seed=BOOT_SEED),
        "1h": block_bootstrap_ci(trades, block_s=BLOCK_HOUR_S,
                                 b=BOOT_B, seed=BOOT_SEED),
    }


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else float("nan")


def _robustness(res: dict, hist_mean: float) -> dict:
    """§3 判据 5 四项 + §7.2.1 历史标准差（§7.7.3/§7.7.4 口径）。

    翻号判定统一为「净均值 < 0」（§7.7.9），不另设显著性门槛。
    """
    trades, detail = res["trades"], res["detail"]
    if not trades:
        return {}
    h_s = res["h_s"]
    mean_all = _mean([v for _, v in trades])

    # ① 换 1h 块
    block1h = block_bootstrap_ci(trades, block_s=BLOCK_HOUR_S,
                                 b=BOOT_B, seed=BOOT_SEED)[0]

    # ② 剔最高波动 10% 时段（整槽、跨币一起剔；RV 阈值取 test 段全部槽分布）
    rv_cut = quantile(sorted(res["slot_rv"].values()), 0.90)
    kept = [v for t, v in trades if res["slot_rv"][t // h_s] <= rv_cut]

    # ③ 只看 BTC/ETH 子集
    be = [d["net_bp"] for d in detail if d["coin"] in ("BTC", "ETH")]

    # ④ 剔单一最大贡献币：Σ net_bp 与 leave-one-out 影响两种都必须过
    sums: dict[str, float] = defaultdict(float)
    cnts: dict[str, int] = defaultdict(int)
    for d in detail:
        sums[d["coin"]] += d["net_bp"]
        cnts[d["coin"]] += 1
    top_sum = max(sums, key=lambda c: sums[c])
    drop_sum = _mean([d["net_bp"] for d in detail if d["coin"] != top_sum])
    loo = {c: cnts[c] * (sums[c] / cnts[c] - mean_all) for c in sums}
    top_loo = max(loo, key=lambda c: loo[c])
    drop_loo = _mean([d["net_bp"] for d in detail if d["coin"] != top_loo])

    return {
        "block1h": block1h,
        "trim_vol_top10": _mean(kept),
        "btc_eth": _mean(be),
        "drop_max_sum": drop_sum,
        "drop_max_loo": drop_loo,
        "hist_std": hist_mean,
        "top_sum_coin": top_sum,
        "top_loo_coin": top_loo,
    }


def evaluate(book_root: Path, days: list[str], *, w_s: int, h_s: int,
             coin_slots: Optional[dict] = None) -> dict:
    """产出 H1 主口径 / H1 历史标准差 / H2 三组结果。

    coin_slots 为 None 时自行逐币建槽（每币仅保留每槽标量记录，处理完即
    释放 1s 帧与原始行）；调用方也可注入 build_coin_slots_dual 的产物，
    以便两套口径共享同一趟帧循环（输出与分别建槽逐位一致）。
    """
    import calendar
    half = len(days) // 2
    y, m, d = map(int, days[half].split("-"))
    test_start_s = calendar.timegm((y, m, d, 0, 0, 0))

    if coin_slots is None:
        all_coins: set[str] = set()
        for dd in days:
            all_coins |= coins_present(book_root, dd)
        coin_slots = {c: build_coin_slots(book_root, days, c, w_s=w_s, h_s=h_s)
                      for c in sorted(all_coins)}
    out: dict[str, dict] = {}
    for name, kind in (("h1", "h1"), ("h1_hist", "h1_hist"), ("h2", "h2")):
        res = _select(coin_slots, test_start_s=test_start_s, h_s=h_s, kind=kind)
        res["n"] = len(res["trades"])
        res["boot"] = _boot(res["trades"])
        out[name] = res
    hist_boot = out["h1_hist"]["boot"]
    out["h1"]["robust"] = _robustness(
        out["h1"], hist_boot["15m"][0] if hist_boot else float("nan"))
    return out


ROBUST_KEYS = ("block1h", "trim_vol_top10", "btc_eth",
               "drop_max_sum", "drop_max_loo", "hist_std")


def judge(name: str, res: dict, *, primary: bool, p_thr: float,
          rob: Optional[dict] = None) -> tuple[bool, list[str]]:
    """逐条给出 §3 五条判据 yes/no（§7.5）；不输出单一布尔。"""
    if not res.get("boot"):
        return False, [f"[{name}] n=0 → 全部判据不可判定（延长采集）"]

    lines: list[str] = []
    mean, lo, hi, p = res["boot"]["15m"]
    c1 = lo > 0
    c2 = mean > 0
    c3 = primary
    c4 = p < p_thr
    ok_n = res["n"] >= MIN_TRADES
    ok_econ = mean >= MIN_ECON_BP

    lines.append(f"[{name}] n={res['n']} mean={mean:.2f}bp "
                 f"CI95(15m块)=[{lo:.2f},{hi:.2f}] p={p:.4f}")
    lines.append(f"  判据1 CI下界>0: {'yes' if c1 else 'no'}")
    lines.append(f"  判据2 方向不翻号(mean>0): {'yes' if c2 else 'no'}")
    lines.append(f"  判据3 主口径单独成立: {'yes' if c3 else 'no'}")
    lines.append(f"  判据4 单侧p<{p_thr}: {'yes' if c4 else 'no'}"
                 "（仅报告不门控，§7.7.2：判据1已隐含 p<0.025）")

    c5 = False
    if rob is None:
        lines.append("  判据5 稳健性: n/a（§3 判据5 只对 H1）")
    else:
        c5 = True
        for k in ROBUST_KEYS:
            v = rob.get(k, float("nan"))
            ok = isinstance(v, float) and not math.isnan(v) and v >= 0
            c5 = c5 and ok
            lines.append(f"  判据5[{k}]: {v:+.2f}bp {'yes' if ok else 'no'}")
        lines.append(f"    (剔除的币: Σ={rob.get('top_sum_coin')} "
                     f"LOO={rob.get('top_loo_coin')})")

    lines.append(f"  样本量 n>={MIN_TRADES}: {'yes' if ok_n else 'no'}")
    lines.append(f"  经济阈值 mean>={MIN_ECON_BP}bp: {'yes' if ok_econ else 'no'}")

    # §7.7.2：判据 4 只报 p，不重复门控（lo>0 与 p<0.025 严格等价，
    # c4 纳入 passed 既冗余又会误导读者的通过门槛）。仅 H2 的 Holm
    # 上界 p<0.025 由 c1 的 CI 隐含，同样无需 c4 门控。
    passed = (c1 and c2 and c3 and ok_n and ok_econ
              and (c5 if rob is not None else True))
    return passed, lines


# ── 多因子分层 IC（探索性报告，不参与 H1 主判定；2026-10-07 增补）────────────

def _spearman(a: list[float], b: list[float]) -> Optional[float]:
    """Spearman 秩相关；n<2/零方差返回 None。"""
    n = len(a)
    if n < 2 or n != len(b):
        return None

    def ranks(x):
        order = sorted(range(n), key=lambda i: x[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and x[order[j + 1]] == x[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma = sum(ra) / n
    mb = sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va <= 0 or vb <= 0:
        return None
    return cov / math.sqrt(va * vb)


def stream_coin_factor_samples(book_root: Path, days: list[str], coin: str, *,
                               w_s: int, h_s: int) -> list[dict]:
    """与 stream_coin_samples 同槽口径，但对 FACTORS 注册的全部因子打分。

    每个 h 槽取末帧为信号帧：
      edge 因子 = 窗口 [t-w_s,t] 内逐帧增量求和；
      frame 因子 = 信号帧的单帧值。
    返回 {t, bid, ask, f:{name:value}}，None 因子不写入该 name。
    """
    from collections import deque

    factor_names = list(ofi_mod.FACTORS)
    # 每个 edge 因子一个 ring：(t, val)
    rings: dict[str, deque] = {n: deque() for n, (k, _) in ofi_mod.FACTORS.items()
                               if k == "edge"}
    prev_snap = None
    cur_slot = None
    cur_frame = None
    samples: list[dict] = []

    def flush_slot(frame):
        if frame is None:
            return
        t, bid, ask, snap = frame
        fv = {}
        for name, (kind, _) in ofi_mod.FACTORS.items():
            if kind == "edge":
                v = sum(v for et, v in rings[name] if et >= t - w_s)
                fv[name] = v
            else:
                fn = ofi_mod.FACTORS[name][1]
                v = fn(snap)
                if v is not None:
                    fv[name] = v
        samples.append({"t": t, "bid": bid, "ask": ask, "f": fv})

    for r in _stream_raw_points(book_root, days, coin):
        t = int(r.get("t", 0)) // MS
        ba = _best_ba(r)
        if ba is None:
            continue
        bp, bq, ap, aq = ba
        snap = {"b": r.get("b", [[bp, bq]]), "a": r.get("a", [[ap, aq]])}
        if prev_snap is not None:
            for name, (kind, fn) in ofi_mod.FACTORS.items():
                if kind != "edge":
                    continue
                try:
                    v = float(fn(prev_snap, snap))
                except Exception:
                    v = 0.0
                rings[name].append((t, v))
        prev_snap = snap

        slot = t // h_s
        if cur_slot is None:
            cur_slot = slot
        if slot != cur_slot:
            flush_slot(cur_frame)
            cur_slot = slot
        cur_frame = (t, bp, ap, snap)
        for name in rings:
            rg = rings[name]
            while rg and rg[0][0] < t - w_s - h_s:
                rg.popleft()

    if cur_frame is not None:
        flush_slot(cur_frame)
    return samples


def evaluate_factors(book_root: Path, days: list[str], *, w_s: int, h_s: int,
                     n_bands: int = 5) -> dict:
    """逐因子：test 段做 ①Spearman(因子, 下一槽 mid 收益) IC；②五档多空
    净 bps（最高档做多 / 最低档做空），分位边界只用 train 段。

    内存安全：逐币流式、只留 test 的 (因子值, forward mid 收益, 可执行净bp)
    标量。返回 {factor: {n, ic, long, short, ls}}，ls=多档−空档净bp均值差。
    """
    import calendar
    half = len(days) // 2
    y, m, d = map(int, days[half].split("-"))
    test_start = calendar.timegm((y, m, d, 0, 0, 0))
    all_coins = set()
    for dd in days:
        all_coins |= coins_present(book_root, dd)

    names = list(ofi_mod.FACTORS)
    train_vals: dict[str, list[float]] = {n: [] for n in names}
    # test: factor -> list[(val, mid_ret_bp, long_net_bp, short_net_bp)]
    test_rows: dict[str, list[tuple]] = {n: [] for n in names}

    for coin in sorted(all_coins):
        s = stream_coin_factor_samples(book_root, days, coin,
                                       w_s=w_s, h_s=h_s)
        for i, rec in enumerate(s):
            if i + 1 >= len(s):
                break
            fwd = s[i + 1]
            m0 = (rec["bid"] + rec["ask"]) / 2.0
            m1 = (fwd["bid"] + fwd["ask"]) / 2.0
            mid_ret = (m1 - m0) / m0 * 1e4 if m0 > 0 else 0.0
            # 可执行净 bps（taker，开仓 cross 半价差）
            cross = (rec["ask"] - rec["bid"]) / 2 / m0 * 1e4
            long_net = (fwd["bid"] - rec["ask"]) / rec["ask"] * 1e4 \
                - cross - 2 * TAKER_FEE_BP_PER_SIDE
            short_net = (rec["bid"] - fwd["ask"]) / rec["bid"] * 1e4 \
                - cross - 2 * TAKER_FEE_BP_PER_SIDE
            for n in names:
                if n not in rec["f"]:
                    continue
                v = rec["f"][n]
                if rec["t"] < test_start:
                    train_vals[n].append(v)
                else:
                    test_rows[n].append((v, mid_ret, long_net, short_net))

    out = {}
    for n in names:
        rows = test_rows[n]
        if len(rows) < 50:
            out[n] = {"n": len(rows)}
            continue
        vals = [r[0] for r in rows]
        rets = [r[1] for r in rows]
        ic = _spearman(vals, rets)
        # train 分位边界（n_bands 等频）
        tv = sorted(train_vals[n])
        cuts = []
        for b in range(1, n_bands):
            cuts.append(quantile(tv, b / n_bands))

        def band_of(v):
            b = 0
            for c in cuts:
                if v > c:
                    b += 1
            return b

        top_l, bot_s = [], []
        for v, _, ln, sn in rows:
            b = band_of(v)
            if b == n_bands - 1:
                top_l.append(ln)
            if b == 0:
                bot_s.append(sn)
        ml = sum(top_l) / len(top_l) if top_l else float("nan")
        ms = sum(bot_s) / len(bot_s) if bot_s else float("nan")
        out[n] = {"n": len(rows), "ic": ic,
                  "long": ml, "short": ms,
                  "n_long": len(top_l), "n_short": len(bot_s),
                  "ls": (ml - ms) if (ml == ml and ms == ms) else float("nan")}
    return out


def print_factor_report(res: dict) -> None:
    print(f"\n== 多因子分层 IC（W=60s h=300s，test 段，探索性，不决定 H1）==")
    print(f"  {'factor':18}{'n':>7}{'IC':>8}{'top档多':>10}{'底档空':>10}"
          f"{'多-空':>9}")
    for n, r in res.items():
        if r.get("n", 0) < 50:
            print(f"  {n:18}{r.get('n',0):>7}  (样本不足)")
            continue
        ic = r.get("ic")
        print(f"  {n:18}{r['n']:>7}"
              f"{(ic if ic is not None else float('nan')):>8.3f}"
              f"{r['long']:>10.2f}{r['short']:>10.2f}{r['ls']:>9.2f}")


# ── 成交流因子 IC（基于 trades-raw；2026-10-07 增补）──────────────────────────

def load_trade_coin_day(trades_root: Path, day: str, coin: str) -> list[dict]:
    ddir = trades_root / f"date={day}"
    for cand in (ddir / f"{coin}.jsonl", ddir / f"{coin}.jsonl.gz"):
        if cand.is_file():
            opener = gzip.open if cand.name.endswith(".gz") else open
            rows = []
            with opener(cand, "rt") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            rows.sort(key=lambda r: int(r.get("time", 0)))
            return rows
    return []


def trade_coins_present(trades_root: Path, day: str) -> set[str]:
    ddir = trades_root / f"date={day}"
    out = set()
    if not ddir.is_dir():
        return out
    for p in list(ddir.glob("*.jsonl")) + list(ddir.glob("*.jsonl.gz")):
        name = p.name
        for suf in (".jsonl.gz", ".jsonl"):
            if name.endswith(suf):
                out.add(name[: -len(suf)])
                break
    return out


def trade_slot_samples(trades_root: Path, days: list[str], coin: str, *,
                       w_s: int, h_s: int) -> list[dict]:
    """按 h_s 槽对逐笔成交流采样。

    流式收集 print；以 print 的 time//h_s 归槽，每槽取**最后一个 print** 为
    信号锚（t=该 print time、px=成交价）。窗口 = time 落在 [t-w_s,t] 的全部
    print，对 TRADE_FACTORS 每个因子打分。下一槽信号锚提供 forward 价。
    返回 {t, px, f:{name:val}}。
    """
    from collections import deque
    ring: deque = deque()   # (t_s, print)
    cur_slot = None
    cur_anchor = None
    samples: list[dict] = []

    def flush(slot, anchor):
        if anchor is None:
            return
        t, px = anchor
        win = [p for pt, p in ring if pt >= t - w_s]
        fv = {}
        for name, fn in ofi_mod.TRADE_FACTORS.items():
            try:
                v = fn(win)
            except Exception:
                v = None
            if v is not None:
                fv[name] = float(v)
        samples.append({"t": t, "px": px, "f": fv})

    for day in days:
        for r in load_trade_coin_day(trades_root, day, coin):
            try:
                t = int(r.get("time", 0)) // MS
                px = float(r.get("px"))
            except Exception:
                continue
            slot = t // h_s
            if cur_slot is None:
                cur_slot = slot
            if slot != cur_slot:
                flush(cur_slot, cur_anchor)
                cur_slot = slot
            cur_anchor = (t, px)
            ring.append((t, r))
            while ring and ring[0][0] < t - w_s - h_s:
                ring.popleft()
    if cur_anchor is not None:
        flush(cur_slot, cur_anchor)
    return samples


def evaluate_trade_factors(trades_root: Path, days: list[str], *,
                           w_s: int, h_s: int, n_bands: int = 5) -> dict:
    """逐成交流因子：test 段 ①Spearman(因子, 下一槽成交收益) IC；②五档多空
    净 bps（顶档做多/底档做空，taker 口径）。分位边界只用 train 段。

    用成交价（print px）相邻槽求收益；可执行净 bp 保守按 taker 双边（不再加
    cross，因为信号来自成交、可直接 taker 成交）。内存安全：逐币流式。
    """
    import calendar
    half = len(days) // 2
    y, m, d = map(int, days[half].split("-"))
    test_start = calendar.timegm((y, m, d, 0, 0, 0))
    all_coins = set()
    for dd in days:
        all_coins |= trade_coins_present(trades_root, dd)

    names = list(ofi_mod.TRADE_FACTORS)
    train_vals: dict[str, list[float]] = {n: [] for n in names}
    test_rows: dict[str, list[tuple]] = {n: [] for n in names}

    for coin in sorted(all_coins):
        s = trade_slot_samples(trades_root, days, coin, w_s=w_s, h_s=h_s)
        for i, rec in enumerate(s):
            if i + 1 >= len(s):
                break
            fwd = s[i + 1]
            p0, p1 = rec["px"], fwd["px"]
            if p0 <= 0:
                continue
            ret_bp = (p1 - p0) / p0 * 1e4
            # 可执行 taker：买按 p0 卖按 p1，扣双边 taker（不加 cross）。
            long_net = ret_bp - 2 * TAKER_FEE_BP_PER_SIDE
            short_net = -ret_bp - 2 * TAKER_FEE_BP_PER_SIDE
            for n in names:
                if n not in rec["f"]:
                    continue
                v = rec["f"][n]
                if rec["t"] < test_start:
                    train_vals[n].append(v)
                else:
                    test_rows[n].append((v, ret_bp, long_net, short_net))

    out = {}
    for n in names:
        rows = test_rows[n]
        if len(rows) < 50:
            out[n] = {"n": len(rows)}
            continue
        vals = [r[0] for r in rows]
        rets = [r[1] for r in rows]
        ic = _spearman(vals, rets)
        tv = sorted(train_vals[n])
        cuts = [quantile(tv, b / n_bands) for b in range(1, n_bands)]

        def band_of(v):
            b = 0
            for c in cuts:
                if v > c:
                    b += 1
            return b

        top_l, bot_s = [], []
        for v, _, ln, sn in rows:
            b = band_of(v)
            if b == n_bands - 1:
                top_l.append(ln)
            if b == 0:
                bot_s.append(sn)
        ml = sum(top_l) / len(top_l) if top_l else float("nan")
        ms = sum(bot_s) / len(bot_s) if bot_s else float("nan")
        out[n] = {"n": len(rows), "ic": ic, "long": ml, "short": ms,
                  "n_long": len(top_l), "n_short": len(bot_s),
                  "ls": (ml - ms) if (ml == ml and ms == ms) else float("nan")}
    return out


def print_trade_factor_report(res: dict) -> None:
    print(f"\n== 成交流因子分层 IC（trades-raw，W=60s h=300s，test，探索性）==")
    print(f"  {'factor':24}{'n':>7}{'IC':>8}{'top档多':>10}{'底档空':>10}"
          f"{'多-空':>9}")
    for n, r in res.items():
        if r.get("n", 0) < 50:
            print(f"  {n:24}{r.get('n',0):>7}  (样本不足)")
            continue
        ic = r.get("ic")
        print(f"  {n:24}{r['n']:>7}"
              f"{(ic if ic is not None else float('nan')):>8.3f}"
              f"{r['long']:>10.2f}{r['short']:>10.2f}{r['ls']:>9.2f}")


def print_block(title: str, res: dict, *, primary: bool, p_thr: float,
                rob: Optional[dict] = None) -> bool:
    passed, lines = judge(title, res, primary=primary, p_thr=p_thr, rob=rob)
    print("\n".join(lines))
    return passed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    data_dir = os.environ.get("HERMES_DATA_DIR", "/data")
    ap.add_argument("--book-root", default=str(Path(data_dir) / "book-raw"))
    ap.add_argument("--i-know-its-early", action="store_true",
                    help="仅供联调；未到 10-28 不得用于结论")
    ap.add_argument("--factor-ic", action="store_true",
                    help="追加多因子分层 IC 探索报告（不参与 H1 主判定）")
    ap.add_argument("--trade-factor-ic", action="store_true",
                    help="追加基于 trades-raw 的成交流因子 IC（探索性）")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                    help="建槽并行进程数（按币分片；1=串行，输出不变）")
    args = ap.parse_args()

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today < FIRST_ANALYSIS_DATE and not args.i_know_its_early:
        print(f"[m1-eval] 日期闸：今天 {today} 早于预注册首次分析日 "
              f"{FIRST_ANALYSIS_DATE}，拒绝执行（防止提前看数）。")
        return 2

    book_root = Path(args.book_root)
    days = list_day_dirs(book_root)
    if len(days) < 2:
        print(f"[m1-eval] book-raw 可用日不足：{days}")
        return 2
    print(f"[m1-eval] 天数={len(days)} {days[0]}~{days[-1]}")

    print("\n== 建槽（单遍双口径，--jobs 并行按币分片）==")
    t0 = time.time()
    all_coins: set[str] = set()
    for dd in days:
        all_coins |= coins_present(book_root, dd)
    coins = sorted(all_coins)
    slots_primary: dict[str, list[SlotRec]] = {}
    slots_robust: dict[str, list[SlotRec]] = {}
    if args.jobs > 1 and len(coins) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for coin, (s1, s2) in ex.map(
                    _dual_one, [(book_root, days, c) for c in coins]):
                slots_primary[coin] = s1
                slots_robust[coin] = s2
    else:
        for coin in coins:
            s1, s2 = build_coin_slots_dual(book_root, days, coin)
            slots_primary[coin] = s1
            slots_robust[coin] = s2
    print(f"[m1-eval] 建槽完成 {len(coins)} 币，{time.time() - t0:.1f}s")

    print("\n== 主口径 W=60s h=300s（唯一决定 H1 的配置）==")
    r = evaluate(book_root, days, w_s=W_PRIMARY_S, h_s=H_PRIMARY_S,
                 coin_slots=slots_primary)
    h1_passed = print_block("H1 主口径", r["h1"], primary=True,
                            p_thr=0.05, rob=r["h1"]["robust"])
    h2_passed = print_block("H2 次要假设 5m（Holm/Bonferroni 0.025）", r["h2"],
                            primary=True, p_thr=0.025)

    print("\n== 稳健性格子 W=300s h=900s（不决定主结论，§3 判据 3）==")
    r2 = evaluate(book_root, days, w_s=W_ROBUST_S, h_s=H_ROBUST_S,
                  coin_slots=slots_robust)
    print_block("H1 稳健格子", r2["h1"], primary=False, p_thr=0.05)
    print_block("H2 参考 15m（§7.7.7）", r2["h2"], primary=False, p_thr=0.025)

    if r["h1"]["n"] < MIN_TRADES:
        # §7.5：样本不足时不得给出方向性结论，只能要求延长采集
        verdict = f"样本不足（n={r['h1']['n']} < {MIN_TRADES}），延长采集，不下结论"
    elif h1_passed:
        verdict = "进入 §5 交付" + ("；H2 亦通过" if h2_passed else "；H2 未通过")
    else:
        verdict = "关闭该方向，不产出策略包装" + \
                   ("；H2 亦通过" if h2_passed else "；H2 未通过")
    print("\n结论：" + verdict)

    if args.factor_ic:
        fr = evaluate_factors(book_root, days,
                              w_s=W_PRIMARY_S, h_s=H_PRIMARY_S)
        print_factor_report(fr)

    if args.trade_factor_ic:
        trades_root = Path(data_dir) / "trades-raw"
        tdays = list_day_dirs(trades_root)
        if len(tdays) < 2:
            print(f"[m1-eval] trades-raw 可用日不足：{tdays}")
        else:
            print(f"[m1-eval] trades 天数={len(tdays)} "
                  f"{tdays[0]}~{tdays[-1]}")
            tfr = evaluate_trade_factors(trades_root, tdays,
                                         w_s=W_PRIMARY_S, h_s=H_PRIMARY_S)
            print_trade_factor_report(tfr)

    return 0 if h1_passed else (3 if r["h1"]["n"] < MIN_TRADES else 1)


if __name__ == "__main__":
    raise SystemExit(main())
