"""Maker 成交质量取样记录（方案 A 数据契约）。

记录每笔被动单的生命周期，用于回答"被动成交能否翻正成本结构"：

- posted：挂单进入簿内的时间、价格、当时 mid；
- filled：成交时间、价格；
- 派生：resting_ms（挂到成交的等待）、maker_edge_bps（相对挂单时 mid 的
  成交价改善）、post_fill_mid_drift_bps（成交后 mid 是否向不利方向跑——
  逆向选择）。

本模块只做**纯数据与派生计算**，不联网、不下单；由取样驱动在对应时点调用。
队列位置 HL 公开接口不提供历史，故以"resting 时长 + 成交后漂移"作为逆向选择
的可观测代理（在 ADR/报告中已注明该限制）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class MakerSample:
    coin: str
    is_buy: bool
    size: float
    posted_ms: int
    post_limit_px: float
    post_mid_px: float
    filled_ms: int
    fill_px: float
    post_fill_mid_px: float
    canceled_ms: Optional[int] = None

    @property
    def resting_ms(self) -> int:
        return self.filled_ms - self.posted_ms

    @property
    def maker_edge_bps(self) -> float:
        """成交价相对"挂单时 mid"的改善（正=挂单拿到比 mid 更好的价）。"""
        if self.post_mid_px <= 0:
            return 0.0
        # 买单：低于 mid 为改善；卖单：高于 mid 为改善。
        diff = (self.post_mid_px - self.fill_px) if self.is_buy \
            else (self.fill_px - self.post_mid_px)
        return diff / self.post_mid_px * 1e4

    @property
    def post_fill_mid_drift_bps(self) -> float:
        """成交后 mid 的方向漂移（正=向不利方向，逆向选择信号）。

        买单不利=mid 下跌；卖单不利=mid 上涨。
        """
        if self.fill_px <= 0:
            return 0.0
        drift = (self.fill_px - self.post_fill_mid_px) if self.is_buy \
            else (self.post_fill_mid_px - self.fill_px)
        return drift / self.fill_px * 1e4


def maker_sample_to_dict(s: MakerSample) -> dict:
    d = asdict(s)
    d["resting_ms"] = s.resting_ms
    d["maker_edge_bps"] = round(s.maker_edge_bps, 4)
    d["post_fill_mid_drift_bps"] = round(s.post_fill_mid_drift_bps, 4)
    return d


def append_maker_sample(path: str, s: MakerSample) -> None:
    """以 JSONL 追加一笔取样记录（调用方负责目录与轮转）。"""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(maker_sample_to_dict(s), ensure_ascii=False) + "\n")
