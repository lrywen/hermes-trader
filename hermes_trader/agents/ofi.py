"""M-1 L2 order-flow-imbalance (OFI) 因子纯函数。

严格实现预注册（docs/research/2026-09/m1_l2_book_capture_prereg_2026-09-28.md
§1 H1）中"看数前固定"的特征定义，不含任何拟合/参数搜索。供：
  * 生产路径：作为突破信号的 confirm/deny 闸门（仅在 H1 检验通过后接线）；
  * 研究路径：scripts/m1_ofi_eval.py 在 2026-10-28 后做样本外检验。

口径
----
输入快照为 book-raw 行（dict）：
    {"t": ms, "b": [[px, sz], ...bid 档], "a": [[px, sz], ...ask 档]}
档位按价格从好到差排列（bid 降序、ask 升序），挂量为字符串或数值。

best-level OFI（Cont et al. 2014）：
    e =  1[Pb>=Pb_] Qb  - 1[Pb<=Pb_] Qb_
         - 1[Pa<=Pa_] Qa  + 1[Pa>=Pa_] Qa_
深度扩展 OFI-k：对各档匹配同价位（按价格对齐），按相同方向规则求各档贡献，
权重 w_i = 1/i（i 从 1 起，预先固定）。

本模块只做"相邻两帧"的增量 e；窗口聚合 / 标准化 / horizon 标签在研究脚本里
用严格的过去窗口完成。
"""
from __future__ import annotations

from typing import Optional, Sequence

# 一档: (price, size)
Level = tuple[float, float]


def _to_levels(raw) -> list[Level]:
    """把一帧 b/a（[[px,sz], ...]）转成 (float px, float size)，丢弃坏档。"""
    out: list[Level] = []
    if not raw:
        return out
    for row in raw:
        try:
            px, sz = float(row[0]), float(row[1])
        except Exception:
            continue
        if px > 0 and sz >= 0:
            out.append((px, sz))
    return out


def _best(levels: Sequence[Level]) -> Optional[Level]:
    """bid 的 best=最高出价；ask 的 best=最低要价。调用方须已按序排列；
    这里不假设排序，直接取极值，鲁棒处理乱序帧。"""
    return levels[0] if levels else None


def best_ofi(prev: dict, cur: dict, *, side_book: str) -> float:
    """单侧 best-level 增量 e。side_book="bid" 或 "ask"。

    bid 侧（best=最高价）：
        e = 1[Pb>=Pb_] Qb - 1[Pb<=Pb_] Qb_
    ask 侧（best=最低价），方向相反：
        e = -1[Pa<=Pa_] Qa + 1[Pa>=Pa_] Qa_
    任一侧缺帧（首次/断线）返回 0.0（不臆造方向）。
    """
    key = "b" if side_book == "bid" else "a"
    p = _best(_to_levels(prev.get(key)))
    c = _best(_to_levels(cur.get(key)))
    if p is None or c is None:
        return 0.0
    pb, qb = p
    pc, qc = c
    if side_book == "bid":
        return (1.0 if pc >= pb else 0.0) * qc \
             - (1.0 if pc <= pb else 0.0) * qb
    # ask
    return (-1.0 if pc <= pb else 0.0) * qc \
         + (1.0 if pc >= pb else 0.0) * qb


def best_ofi_two_sided(prev: dict, cur: dict) -> float:
    """完整 best-level OFI = bid 侧 + ask 侧（同 Cont 2014 单量纲）。"""
    return best_ofi(prev, cur, side_book="bid") \
         + best_ofi(prev, cur, side_book="ask")


def _depth_map(levels: Sequence[Level]) -> dict[float, float]:
    """按价格聚合挂量（同价可能重复）。"""
    m: dict[float, float] = {}
    for px, sz in levels:
        m[px] = m.get(px, 0.0) + sz
    return m


def depth_ofi_side(prev: dict, cur: dict, *, side_book: str,
                   n_levels: int = 10) -> float:
    """单侧深度扩展 OFI：按价格对齐 prev/cur 各档，逐价位用与 best 相同的
    方向规则（bid: 价升吃新量/价跌消旧量；ask 反向），权重 w_i=1/i，
    i 为"当前帧档位序号"（从 best 起 1..n_levels）；退出档位 i 取其在 prev
    中的序号。价不变同价位 = ΔQ。
    """
    key = "b" if side_book == "bid" else "a"
    cur_lv = _to_levels(cur.get(key))[:n_levels]
    prev_lv = _to_levels(prev.get(key))[:n_levels]
    cur_map = _depth_map(cur_lv)
    prev_map = _depth_map(prev_lv)
    cur_rank = {px: i + 1 for i, (px, _) in enumerate(cur_lv)}
    prev_rank = {px: i + 1 for i, (px, _) in enumerate(prev_lv)}

    total = 0.0
    prices = set(cur_map) | set(prev_map)
    for px in prices:
        qc = cur_map.get(px, 0.0)
        qp = prev_map.get(px, 0.0)
        # 该价位的"档位序号"：当前在册用 cur rank，否则用 prev rank
        rank = cur_rank.get(px, prev_rank.get(px, 1))
        w = 1.0 / rank
        if side_book == "bid":
            # 逐价位方向：价不变同价=ΔQ；新价位进入 bid=吃新量；旧价位退出=消旧量。
            if px in cur_map and px in prev_map:
                e = qc - qp                      # 价不变：ΔQ
            elif px in cur_map:                  # 新价位进入 bid
                e = qc
            else:                                  # 旧价位退出 bid
                e = -qp
        else:
            if px in cur_map and px in prev_map:
                e = qc - qp                      # ask 价不变：ΔQ（同向符号）
            elif px in cur_map:                  # 新价位进入 ask（供给增加=负）
                e = -qc
            else:                                  # 旧价位退出 ask（供给减少=正）
                e = qp
        total += w * e
    return total


def depth_ofi(prev: dict, cur: dict, *, n_levels: int = 10) -> float:
    """深度扩展 OFI-10 = bid 侧 + ask 侧（权重 1/i，预注册固定）。"""
    return depth_ofi_side(prev, cur, side_book="bid", n_levels=n_levels) \
         + depth_ofi_side(prev, cur, side_book="ask", n_levels=n_levels)


def mid(snap: dict) -> Optional[float]:
    """mid = (best bid + best ask)/2；缺一侧返回 None。"""
    b = _best(_to_levels(snap.get("b")))
    a = _best(_to_levels(snap.get("a")))
    if b is None or a is None:
        return None
    return (b[0] + a[0]) / 2.0


def aggregate_ofi(increments: Sequence[float]) -> float:
    """窗口内逐帧 e 求和（W_OFI=60/300s）。"""
    return float(sum(increments))


# ── 扩展因子族（看数前固定口径，2026-10-07 增补，供 10-28 分层 IC）──────────
#
# 这些因子只用 book 快照（无成交流），因此"撤单"只能以"价位未移动而同价位
# 挂量减少"作为队列撤离（cancellation proxy）。全部为相邻两帧或单帧纯函数，
# 窗口聚合沿用 aggregate_*：有符号量按求和、[0,1] 比例按末值。

def _side_levels(snap: dict, side_book: str, n_levels: int) -> list[Level]:
    key = "b" if side_book == "bid" else "a"
    return _to_levels(snap.get(key))[:n_levels]


def total_depth(snap: dict, *, n_levels: int = 10) -> tuple[float, float]:
    """返回 (bid 深度总量, ask 深度总量)，各取前 n_levels 档。"""
    bd = sum(sz for _, sz in _side_levels(snap, "bid", n_levels))
    ad = sum(sz for _, sz in _side_levels(snap, "ask", n_levels))
    return bd, ad


def depth_imbalance(snap: dict, *, n_levels: int = 10) -> Optional[float]:
    """深度失衡 DI = (Qb − Qa)/(Qb + Qa) ∈ [-1,1]；全零/缺帧返回 None。

    单帧因子，窗口内取末值（不求和）。>0 买盘挂单堆积占优。
    """
    bd, ad = total_depth(snap, n_levels=n_levels)
    tot = bd + ad
    if tot <= 0:
        return None
    return (bd - ad) / tot


def queue_withdrawal(prev: dict, cur: dict, *, n_levels: int = 10) -> float:
    """队列撤离（撤单代理）有符号增量。

    仅在 prev/cur **同价位**（价位未移动）上比较挂量：
      bid 侧：同价位减少=买盘撤离（负），增加=买盘堆积（正）。
      ask 侧：同价位减少=卖盘撤离（正，利好），增加=卖盘堆积（负）。
    新进入/退出档位不计入（那已由 best/depth OFI 捕捉），以隔离"挂单撤离"
    这一独立信息。缺帧返回 0.0。
    """
    total = 0.0
    for side_book in ("bid", "ask"):
        pm = _depth_map(_side_levels(prev, side_book, n_levels))
        cm = _depth_map(_side_levels(cur, side_book, n_levels))
        for px in set(pm) & set(cm):
            dq = cm[px] - pm[px]          # 价不变 ΔQ
            total += dq if side_book == "bid" else -dq
    return total


def queue_buildup_slope(snap: dict, *, n_levels: int = 10) -> Optional[float]:
    """挂单堆积斜率：逐档 (bid 累计挂量 − ask 累计挂量) 对档位序号做最小二乘
    斜率，再用总深度归一（除以 Qb+Qa），使其跨币可比。

    斜率>0：买盘挂量随档位增长更快（远端买盘厚）；<0 远端卖盘厚。单帧因子，
    窗口取末值。样本<2 或总深度<=0 返回 None。
    """
    bl = _side_levels(snap, "bid", n_levels)
    al = _side_levels(snap, "ask", n_levels)
    m = min(len(bl), len(al))
    if m < 2:
        return None
    cb = ca = 0.0
    xs = list(range(1, m + 1))
    ys = []
    tot = 0.0
    for i in range(m):
        cb += bl[i][1]
        ca += al[i][1]
        ys.append(cb - ca)
        tot += bl[i][1] + al[i][1]
    if tot <= 0:
        return None
    mx = sum(xs) / m
    my = sum(ys) / m
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    return num / den / tot


def ofi_mid_divergence(prev: dict, cur: dict) -> Optional[float]:
    """OFI 与中间价的方向背离：best-OFI 与 Δmid 同向返回 +1，反向返回 -1，
    任一为 0/缺帧返回 0。

    解释：+1 = 订单流推动价格（健康）；-1 = 挂单流失向与价格变动相反（潜在
    诱多/诱空或流动性回补）。相邻两帧因子。
    """
    m0, m1 = mid(prev), mid(cur)
    if m0 is None or m1 is None or m0 <= 0:
        return None
    e = best_ofi_two_sided(prev, cur)
    dm = m1 - m0
    if e == 0.0 or dm == 0.0:
        return 0.0
    return 1.0 if (e > 0) == (dm > 0) else -1.0


# 因子注册表：name -> (kind, fn)。kind="edge"（窗口求和，吃 prev,cur）或
# "frame"（窗口取末值，吃 snap）。供研究脚本统一遍历做分层 IC；口径在此固定。
FACTORS: dict[str, tuple[str, object]] = {
    "ofi_best": ("edge", best_ofi_two_sided),
    "ofi_depth10": ("edge", lambda prev, cur: depth_ofi(prev, cur, n_levels=10)),
    "queue_withdrawal": ("edge", lambda prev, cur: queue_withdrawal(prev, cur, n_levels=10)),
    "depth_imbalance": ("frame", depth_imbalance),
    "buildup_slope": ("frame", queue_buildup_slope),
    "ofi_mid_divergence": ("edge", ofi_mid_divergence),
}
