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

本脚本为研究工具，不进运行时镜像。
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

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


def stream_coin_samples(book_root: Path, days: list[str], coin: str, *,
                        w_s: int, h_s: int):
    """流式构建"每 h 一条"的稀疏样本，O(w_s) 内存、单币常驻后即释放。

    滚动维护最近 w_s 秒的逐帧 edge OFI；以信号 epoch 对齐（slot=t//h_s），
    在每个 h 槽内取该槽最后一帧为信号帧：
      * 窗口 OFI = 落在 [t-w_s, t] 的 edge e 之和（首帧无前置则该窗可能为 None）；
      * forward 行情 = 下一个 h 槽的信号帧（t+h），由调用方按列表相邻关系取。
    返回样本 dict 列表（每币约 days*86400/h_s 条）：
      {t, bid, ask, ofi}
    bid/ask=该信号帧 best 价。
    """
    from collections import deque

    # edge ring: (t, e)，仅保留可能进入窗口的项（>=t-w_s）
    ring: deque = deque()
    prev_snap = None
    cur_slot = None
    cur_frame = None
    cur_window_edges: list[float] = []

    samples: list[dict] = []

    def flush_slot(slot, frame, edges):
        if frame is None:
            return
        t, bid, ask = frame
        # 窗口 OFI：用已收集到本槽的 edges（逐帧 e），取落在 [t-w_s,t] 的和
        total = sum(e for et, e in edges if et >= t - w_s)
        samples.append({"t": t, "bid": bid, "ask": ask, "ofi": total})

    for r in _stream_raw_points(book_root, days, coin):
        t = int(r.get("t", 0)) // MS
        ba = _best_ba(r)
        if ba is None:
            continue
        bp, bq, ap, aq = ba
        snap = {"b": [[bp, bq]], "a": [[ap, aq]]}
        e = ofi_mod.best_ofi_two_sided(prev_snap, snap) if prev_snap is not None else 0.0
        prev_snap = snap

        slot = t // h_s
        if cur_slot is None:
            cur_slot = slot
        if slot != cur_slot:
            # 收尾上一槽
            flush_slot(cur_slot, cur_frame, ring)
            cur_slot = slot

        cur_frame = (t, bp, ap)
        ring.append((t, e))
        # 丢弃绝不可能再进入窗口的旧 edge
        while ring and ring[0][0] < t - w_s - h_s:
            ring.popleft()

    if cur_frame is not None:
        flush_slot(cur_slot, cur_frame, ring)
    return samples


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
    """vals = (t_s, net_bp)，按时间排序。以时间块重采样，返回 (mean, lo, hi)。"""
    if not vals:
        return float("nan"), float("nan"), float("nan")
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
    return obs, lo, hi


# ── 主检验（单个配置）──────────────────────────────────────────────────────

def evaluate(book_root: Path, days: list[str], *, w_s: int, h_s: int) -> dict:
    """对全部币做 walk-forward，train 前半定分位，test 后半唯一报告。

    内存安全：流式稀疏采样，逐币处理、处理完即释放；只保留全局 train 特征池
    （标量）与 test 成交（标量 bp）。扫描步进=h_s（预注册去重叠）。
    forward 行情直接取下一个 h 槽样本（t+h），无需保留 1s 帧。
    """
    half = len(days) // 2
    train_days, test_days = days[:half], days[half:]

    import calendar

    def boundary(day_list):
        y, m, d = map(int, day_list[0].split("-"))
        return calendar.timegm((y, m, d, 0, 0, 0))

    test_start_s = boundary(test_days)

    all_coins = set()
    for d in days:
        all_coins |= coins_present(book_root, d)

    def coin_samples(coin):
        return stream_coin_samples(book_root, days, coin,
                                   w_s=w_s, h_s=h_s)

    # Pass 1：收集 train（t<test_start）窗口 OFI 特征，分位用。
    feat_pool: list[float] = []
    for coin in sorted(all_coins):
        for s in coin_samples(coin):
            if s["t"] < test_start_s:
                feat_pool.append(s["ofi"])
    feat_pool_sorted = sorted(feat_pool)
    lo_q = quantile(feat_pool_sorted, Q_LOW)
    hi_q = quantile(feat_pool_sorted, Q_HIGH)

    # Pass 2：test（t>=test_start）逐槽判定；t+h 行情=下一个槽。
    trades: list[tuple[int, float]] = []
    for coin in sorted(all_coins):
        samples = coin_samples(coin)
        for i, s in enumerate(samples):
            if s["t"] < test_start_s:
                continue
            z = s["ofi"]
            side = 1 if z > hi_q else (-1 if z < lo_q else 0)
            if side == 0 or i + 1 >= len(samples):
                continue
            fwd = samples[i + 1]
            ask0, bid0 = s["ask"], s["bid"]
            ask1, bid1 = fwd["ask"], fwd["bid"]
            if side == 1:
                gross_bp = (bid1 - ask0) / ask0 * 1e4
                cross_half = (ask0 - bid0) / 2 / ask0 * 1e4
            else:
                gross_bp = (bid0 - ask1) / bid0 * 1e4
                cross_half = (ask0 - bid0) / 2 / bid0 * 1e4
            net_bp = gross_bp - cross_half - 2 * TAKER_FEE_BP_PER_SIDE
            trades.append((s["t"], net_bp))

    res = {"n": len(trades)}
    if trades:
        for label, block in (("block15m", BLOCK_MIN_BOOT_S),
                             ("block1h", BLOCK_HOUR_S)):
            mean, lo, hi = block_bootstrap_ci(
                trades, block_s=block, b=BOOT_B, seed=BOOT_SEED)
            res[label] = (mean, lo, hi)
    return res


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


def verdict(res: dict) -> tuple[bool, str]:
    if res["n"] < MIN_TRADES:
        return False, f"样本不足：{res['n']} < {MIN_TRADES}，延长采集"
    mean, lo, hi = res["block15m"]
    ok_ci = lo > 0
    ok_econ = mean >= MIN_ECON_BP
    passed = ok_ci and ok_econ
    return passed, (f"n={res['n']} mean={mean:.2f}bp CI95=[{lo:.2f},{hi:.2f}] "
                    f"1h块 mean={res['block1h'][0]:.2f} CI=[{res['block1h'][1]:.2f},"
                    f"{res['block1h'][2]:.2f}] | CI下界>0={ok_ci} 经济阈值"
                    f"≥{MIN_ECON_BP}bp={ok_econ}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    data_dir = os.environ.get("HERMES_DATA_DIR", "/data")
    ap.add_argument("--book-root", default=str(Path(data_dir) / "book-raw"))
    ap.add_argument("--i-know-its-early", action="store_true",
                    help="仅供联调；未到 10-28 不得用于结论")
    ap.add_argument("--factor-ic", action="store_true",
                    help="追加多因子分层 IC 探索报告（不参与 H1 主判定）")
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

    print("\n== 主口径 W=60s h=300s ==")
    r = evaluate(book_root, days, w_s=W_PRIMARY_S, h_s=H_PRIMARY_S)
    passed, txt = verdict(r)
    print(("H1 通过 ✓ " if passed else "H1 未通过 ✗ ") + txt)

    print("\n== 稳健性 W=300s h=900s（不决定主结论）==")
    r2 = evaluate(book_root, days, w_s=W_ROBUST_S, h_s=H_ROBUST_S)
    print(txt if False else f"n={r2['n']} " + (
        f"mean={r2['block15m'][0]:.2f} CI=[{r2['block15m'][1]:.2f},"
        f"{r2['block15m'][2]:.2f}]" if r2['n'] else "(无交易)"))

    print("\n结论：" + ("进入 §5 交付" if passed else "关闭该方向，不产出策略包装"))

    if args.factor_ic:
        fr = evaluate_factors(book_root, days,
                              w_s=W_PRIMARY_S, h_s=H_PRIMARY_S)
        print_factor_report(fr)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
