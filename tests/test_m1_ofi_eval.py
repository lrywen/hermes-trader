"""M-1 评估器（scripts/m1_ofi_eval.py）的离线单测。

覆盖预注册 §7.2.1 / §7.3 补充 / §7.7：
  * z 分母为窗口内逐秒 e 的样本标准差，n_frames<10 或 std<=0 不触发；
  * 同槽跨币排序、每侧 k=floor(0.10*n_coins)、n_coins<5 不触发；
  * 逐币 z 与池化原始 OFI 的排序必须可区分（AC-11 可证伪）；
  * 3 币 × 1 槽手算黄金样例的可执行净 bp；
  * 跨日不拼相邻帧增量。
"""
from __future__ import annotations

import calendar
import json
import statistics
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

from m1_ofi_eval import (  # noqa: E402
    TAKER_FEE_BP_PER_SIDE,
    _select,
    build_coin_slots,
)

DAY = "2026-10-01"
T0 = calendar.timegm((2026, 10, 1, 0, 0, 0))     # 午夜，可被 300 整除
W_S, H_S = 60, 300
PX, SPREAD, BASE = 100.0, 0.02, 1e6


def _rows(t0: int, n: int, x: float, y: float, *,
          px: float = PX, base: float = BASE) -> list[dict]:
    """逐秒行；bid 挂量按 ΔQ 交替 x/y 变化（ask 恒定）。"""
    q = base
    rows = []
    for i in range(n):
        if i:
            q += x if i % 2 == 1 else y
        rows.append({"t": (t0 + i) * 1000,
                     "b": [[px, q]],
                     "a": [[px + SPREAD, 10.0]]})
    return rows


def _write(root: Path, coin: str, rows: list[dict], day: str = DAY) -> None:
    d = root / f"date={day}"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{coin}.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _coin_slots(root: Path, coins: dict[str, tuple[float, float]],
                days: list[str] | None = None) -> dict:
    return {c: build_coin_slots(root, days or [DAY], c, w_s=W_S, h_s=H_S)
            for c in coins}


PATTERNS = {
    "c0": (100.0, 0.0),
    "c1": (2.0, 1.0),
    "c2": (3.0, 2.0),
    "c3": (4.0, 3.0),
    "c4": (5.0, 4.0),
    "c5": (6.0, 5.0),
    "c6": (7.0, 6.0),
    "c7": (9.0, 8.0),
    "c8": (11.0, 10.0),
    "c9": (21.0, 20.0),
}


@pytest.fixture()
def ten_coins(tmp_path):
    for coin, (x, y) in PATTERNS.items():
        _write(tmp_path, coin, _rows(T0, 2 * H_S, x, y))
    return tmp_path


def test_guard_std_zero_and_short_window(tmp_path):
    """std_within=0（挂量不变）与 n_frames<10 都不得产生有效 z。"""
    _write(tmp_path, "FLAT", _rows(T0, 2 * H_S, 0.0, 0.0))
    _write(tmp_path, "SHORT", _rows(T0, 5, 2.0, 1.0))
    flat = build_coin_slots(tmp_path, [DAY], "FLAT", w_s=W_S, h_s=H_S)
    short = build_coin_slots(tmp_path, [DAY], "SHORT", w_s=W_S, h_s=H_S)
    assert flat and all(r.std_within == 0.0 and r.z_main is None for r in flat)
    assert short and short[0].n_frames < 10 and short[0].z_main is None


def test_z_is_within_window_sample_std(ten_coins):
    """z = Σe / stdev(e in window, ddof=1)，且与挂量绝对尺度无关。"""
    recs = build_coin_slots(ten_coins, [DAY], "c9", w_s=W_S, h_s=H_S)
    rec = recs[0]
    # 窗口 [t-w_s, t] 闭区间 → 61 帧；末帧 t 为奇数序号 → 多一个 21
    window = [21.0] * 31 + [20.0] * 30
    assert rec.n_frames == 61
    assert rec.ofi == pytest.approx(sum(window))
    assert rec.std_within == pytest.approx(
        statistics.stdev(window), rel=1e-9)          # ddof=1
    assert rec.z_main == pytest.approx(rec.ofi / rec.std_within)


def test_per_coin_z_outranks_pooled_raw_ofi(ten_coins):
    """AC-11：池化原始 OFI 与逐币 z 的排序必须给出不同的多空币。"""
    slots = _coin_slots(ten_coins, PATTERNS)
    raw_top = max(slots, key=lambda c: slots[c][0].ofi)
    raw_bottom = min(slots, key=lambda c: slots[c][0].ofi)
    z_top = max(slots, key=lambda c: slots[c][0].z_main)
    z_bottom = min(slots, key=lambda c: slots[c][0].z_main)
    # 原始 OFI 选 c0 做多；逐币 z 选 c9 做多、c0 做空
    assert (raw_top, raw_bottom) == ("c0", "c1")
    assert (z_top, z_bottom) == ("c9", "c0")

    res = _select(slots, test_start_s=T0, h_s=H_S, kind="h1")
    longs = [d["coin"] for d in res["detail"] if d["side"] == 1]
    shorts = [d["coin"] for d in res["detail"] if d["side"] == -1]
    assert longs == ["c9"]
    assert shorts == ["c0"]


def test_cross_section_minimum_and_k_floor(ten_coins):
    """n_coins<5 不触发；n=5 时 k=floor(0.5)=0 也不触发；n=10 时每侧 1。"""
    slots = _coin_slots(ten_coins, PATTERNS)
    four = {c: slots[c] for c in list(slots)[:4]}
    five = {c: slots[c] for c in list(slots)[:5]}
    assert len(_select(four, test_start_s=T0, h_s=H_S, kind="h1")["trades"]) == 0
    assert len(_select(five, test_start_s=T0, h_s=H_S, kind="h1")["trades"]) == 0
    assert len(_select(slots, test_start_s=T0, h_s=H_S, kind="h1")["trades"]) == 2


def test_gold_sample_executable_net_bp(ten_coins):
    """手算黄金样例：c9 做多，t 时刻 ask 买 / t+h 时刻 bid 卖。"""
    slots = _coin_slots(ten_coins, PATTERNS)
    res = _select(slots, test_start_s=T0, h_s=H_S, kind="h1")
    trade = next(d for d in res["detail"] if d["coin"] == "c9" and d["side"] == 1)
    rec = slots["c9"][0]
    fwd = slots["c9"][1]
    expected = ((fwd.bid - rec.ask) / rec.ask * 1e4
                - (rec.ask - rec.bid) / 2 / rec.ask * 1e4
                - 2 * TAKER_FEE_BP_PER_SIDE)
    # 手算：毛 -2.00bp、cross 半价差 1.00bp、taker 双边 8.64bp → -11.64bp
    assert expected == pytest.approx(-11.64, abs=1e-3)
    assert trade["net_bp"] == pytest.approx(expected)


def test_cross_day_edge_reset(tmp_path):
    """跨日不拼相邻帧增量：第二日首帧 e=0，窗口和=15x+14y。"""
    day1 = "2026-10-01"
    day2 = "2026-10-02"
    _write(tmp_path, "C", _rows(T0, 1, 2.0, 1.0), day=day1)
    _write(tmp_path, "C", _rows(T0 + 86400, 30, 2.0, 1.0), day=day2)
    recs = build_coin_slots(tmp_path, [day1, day2], "C", w_s=W_S, h_s=H_S)
    # 第二日只有 30 帧，全部落在窗口内
    last = recs[-1]
    assert last.n_frames == 30
    assert last.ofi == pytest.approx(15 * 2.0 + 14 * 1.0)
