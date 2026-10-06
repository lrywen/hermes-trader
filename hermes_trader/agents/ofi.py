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
