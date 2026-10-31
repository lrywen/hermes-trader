"""P2 数据同源：用真实 tape-raw / book-raw 替代固定滑点的 fill 模型。

融合文档偏离③：K 线 vs tick 不同源。此前 driver.py 的 entry/exit fill 用
``CostModel`` 的固定 bps 滑点（entry 5 / exit 15 / max_loss +10），与实盘真实
成交不同源。本模块在 **有真实逐笔/盘口数据** 时给出同源 fill：

* **tape-raw 优先**（仅 BTC/ETH）：在 fill 时刻前后 ``window_s`` 秒窗口内取
  真实逐笔成交，按成交量加权（VWAP）得到该时刻的可成交价格；买单在 VWAP 上
  加半个 spread、卖单减半个 spread（方向不利原则，与 CostModel 一致）。
* **book-raw 回退**（21 币 1s 快照）：无 tape 时取该时刻最近一帧 best bid/ask，
  买取 ask、卖取 bid，再加固定 ``depth_slip_bps`` 深度衰减。
* **固定滑点兜底**：两者都缺（非试点币 / 数据空洞）时退回 ``CostModel``，
  保证任何币任何时刻都有确定 fill，绝不抛异常中断回测。

PIT 契约：fill 时刻 ``t_ms`` 之后的数据绝不使用（窗口只向后看 ``window_s``，
且要求逐笔 ``t <= t_ms``），与 driver 的 bar-close 决策 / bar-open 成交语义
一致——driver 传入的 ``t_ms`` 是成交 bar 的 open 时刻。

只读 /data，不写任何文件；按 (coin, day) 懒加载并缓存，重复回测不重复读盘。
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Optional

from .cost import CostModel
from .types import ExitReason

_DATA_DIR = Path(os.environ.get("HERMES_DATA_DIR", "/data"))
TAPE_ROOT = _DATA_DIR / "tape-raw"
BOOK_ROOT = _DATA_DIR / "book-raw"

# VWAP 向后看窗口（秒）。5s 在 BTC/ETH 上通常有数百笔，足够稳定；
# 窗口内无成交时逐级放大到 fallback_window_s，再空则回退 book/固定。
DEFAULT_WINDOW_S = 5.0
FALLBACK_WINDOW_S = 30.0
# book 快照允许的最大陈旧（秒）：超过则认为该时刻盘口未知，回退固定滑点。
BOOK_MAX_AGE_S = 10.0
# book 路径的深度衰减滑点（无真实成交流，用固定 bps 近似吃档成本）。
DEFAULT_DEPTH_SLIP_BPS = 8.0


def _day_str(t_ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(t_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def _read_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if path.name.endswith(".gz") else open
    rows = []
    try:
        with opener(path, "rt") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        return []
    return rows


class RawFillModel:
    """同源 fill 模型。与 CostModel 同样的 fill_entry/fill_exit 接口。

    用法：构造后传给 driver.run(cost=...)。driver 在 entry/exit 调用时附带
    成交时刻（通过 set_fill_time 或直接调 price_at）。为保持与 CostModel
    接口兼容（driver 只调 fill_entry(raw_px, side)），fill 时刻通过
    :meth:`at` 上下文或 :meth:`set_time` 预设。
    """

    def __init__(self, coin: str, *, fallback: Optional[CostModel] = None,
                 window_s: float = DEFAULT_WINDOW_S,
                 depth_slip_bps: float = DEFAULT_DEPTH_SLIP_BPS,
                 tape_root: Path = TAPE_ROOT, book_root: Path = BOOK_ROOT):
        self.coin = coin
        self.fallback = fallback or CostModel()
        self.window_s = window_s
        self.depth_slip_bps = depth_slip_bps
        self.tape_root = tape_root
        self.book_root = book_root
        self._tape_cache: dict[str, list[dict]] = {}
        self._book_cache: dict[str, list[dict]] = {}
        # 预计算的 ts 列表（与对应 rows 同序），避免每次 fill O(n) 重建。
        self._tape_ts: dict[str, list[int]] = {}
        self._book_ts: dict[str, list[int]] = {}
        self._now_ms: Optional[int] = None
        # 观测计数：fill 走了哪条路径（tape/book/fallback），供 P2.4 对比。
        self.stats = {"tape": 0, "book": 0, "fallback": 0}

    # ── 数据加载（按日懒加载 + 缓存）─────────────────────────────────
    def _tape_day(self, day: str) -> list[dict]:
        if day not in self._tape_cache:
            ddir = self.tape_root / f"date={day}"
            rows: list[dict] = []
            for cand in (ddir / f"{self.coin}.jsonl", ddir / f"{self.coin}.jsonl.gz"):
                if cand.is_file():
                    rows = _read_jsonl(cand)
                    break
            rows.sort(key=lambda r: int(r.get("t", 0)))
            self._tape_cache[day] = rows
            self._tape_ts[day] = [int(r.get("t", 0)) for r in rows]
        return self._tape_cache[day]

    def _book_day(self, day: str) -> list[dict]:
        if day not in self._book_cache:
            ddir = self.book_root / f"date={day}"
            rows: list[dict] = []
            for cand in (ddir / f"{self.coin}.jsonl", ddir / f"{self.coin}.jsonl.gz"):
                if cand.is_file():
                    rows = _read_jsonl(cand)
                    break
            rows.sort(key=lambda r: int(r.get("t", 0)))
            self._book_cache[day] = rows
            self._book_ts[day] = [int(r.get("t", 0)) for r in rows]
        return self._book_cache[day]

    # ── fill 时刻设定 ──────────────────────────────────────────────
    def set_time(self, t_ms: int) -> None:
        """driver 在每次 fill 前设定成交时刻（bar open ms）。"""
        self._now_ms = t_ms

    # ── tape VWAP ──────────────────────────────────────────────────
    def _tape_vwap(self, t_ms: int, is_buy: bool) -> Optional[float]:
        """t_ms 前 window_s 窗口内的真实逐笔 VWAP ± 半 spread。PIT：只用 t<=t_ms。"""
        import bisect
        day = _day_str(t_ms)
        rows = self._tape_day(day)
        if not rows:
            return None
        ts = self._tape_ts[day]
        # 右端：最后一个 t <= t_ms 的逐笔
        hi = bisect.bisect_right(ts, t_ms)
        if hi == 0:
            return None
        for win_s in (self.window_s, FALLBACK_WINDOW_S):
            lo = bisect.bisect_left(ts, t_ms - int(win_s * 1000), 0, hi)
            window = rows[lo:hi]
            if not window:
                continue
            pv = sum(float(r["px"]) * float(r["sz"]) for r in window
                     if r.get("px") and r.get("sz"))
            vol = sum(float(r["sz"]) for r in window if r.get("sz"))
            if vol <= 0 or pv <= 0:
                continue
            vwap = pv / vol
            # 半 spread：取窗口内最近一笔的 spread_bps，方向不利加减。
            last = window[-1]
            spread_bps = float(last.get("spread_bps", 0.0) or 0.0)
            half = vwap * spread_bps / 2e4
            return vwap + half if is_buy else vwap - half
        return None

    # ── book 最优档 ────────────────────────────────────────────────
    def _book_price(self, t_ms: int, is_buy: bool) -> Optional[float]:
        """t_ms 最近一帧（不超过 BOOK_MAX_AGE_S）的 best ask(买)/bid(卖) + 深度 slip。"""
        import bisect
        day = _day_str(t_ms)
        rows = self._book_day(day)
        if not rows:
            return None
        ts = self._book_ts[day]
        hi = bisect.bisect_right(ts, t_ms)
        if hi == 0:
            return None
        frame = rows[hi - 1]
        age_s = (t_ms - int(frame.get("t", 0))) / 1000.0
        if age_s > BOOK_MAX_AGE_S:
            return None
        key = "a" if is_buy else "b"
        levels = frame.get(key) or []
        if not levels:
            return None
        try:
            best = float(levels[0][0])
        except Exception:
            return None
        if best <= 0:
            return None
        delta = best * self.depth_slip_bps / 1e4
        return best + delta if is_buy else best - delta

    # ── 统一 fill 入口 ─────────────────────────────────────────────
    def _price(self, raw_px: float, is_buy: bool) -> float:
        t_ms = self._now_ms
        if t_ms is None:
            self.stats["fallback"] += 1
            return raw_px  # 未设时刻：不加滑点，由调用方保证先 set_time
        px = self._tape_vwap(t_ms, is_buy)
        if px is not None and px > 0:
            self.stats["tape"] += 1
            return px
        px = self._book_price(t_ms, is_buy)
        if px is not None and px > 0:
            self.stats["book"] += 1
            return px
        # 兜底：固定滑点（与 CostModel 同口径）。
        self.stats["fallback"] += 1
        return self.fallback._adjust(raw_px, is_buy, self.fallback.entry_slip_bps)

    def fill_entry(self, raw_px: float, side: str) -> float:
        return self._price(raw_px, is_buy=(side == "long"))

    def fill_exit(self, raw_px: float, side: str, reason: ExitReason) -> float:
        # max_loss 的额外 stop_delay 在 raw 路径下不叠加（真实成交已含冲击）。
        return self._price(raw_px, is_buy=(side == "short"))

    # fee/pnl 与 CostModel 完全一致（费用口径不变，只改 fill 价）。
    def fee_usd(self, notional_usd: float) -> float:
        return self.fallback.fee_usd(notional_usd)

    def pnl_usd(self, side: str, entry_fill_px: float, exit_fill_px: float,
                notional_usd: float) -> tuple[float, float]:
        return self.fallback.pnl_usd(side, entry_fill_px, exit_fill_px, notional_usd)
