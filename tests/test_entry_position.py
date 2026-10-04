"""entry_position 模块测试：区间分位硬闸 + 末端 blowoff 锁存迟滞。"""
from __future__ import annotations

import pytest

from hermes_trader.agents import entry_position as ep


# ── range_percentile ────────────────────────────────────────────────────────

def test_range_percentile_basic() -> None:
    # 窗口 [9..11]，价 10.9 → 95 分位
    assert ep.range_percentile([10, 11], [9, 10], 10.9) == pytest.approx(95.0)
    assert ep.range_percentile([10], [10], 10) is None  # 退化窗口
    assert ep.range_percentile([], [], 10) is None
    assert ep.range_percentile([11], [9], float("nan")) is None


def test_extreme_position_block_long() -> None:
    assert ep.extreme_position_block(
        side="long", percentile=91, block_high_pct=90,
        block_low_pct=10) is not None
    assert ep.extreme_position_block(
        side="long", percentile=80, block_high_pct=90,
        block_low_pct=10) is None


def test_extreme_position_block_short() -> None:
    assert ep.extreme_position_block(
        side="short", percentile=9, block_high_pct=90,
        block_low_pct=10) is not None
    assert ep.extreme_position_block(
        side="short", percentile=50, block_high_pct=90,
        block_low_pct=10) is None


def test_extreme_position_none_passes() -> None:
    assert ep.extreme_position_block(
        side="long", percentile=None, block_high_pct=90,
        block_low_pct=10) is None


# ── blowoff latch 状态机 ────────────────────────────────────────────────────

def test_latch_set_then_blocked(tmp_path) -> None:
    p = str(tmp_path / "latch.json")
    assert ep.is_blowoff_latched(coin="AAVE", direction="up", path=p) is False
    ep.set_blowoff(coin="AAVE", direction="up", now_ms=1000, path=p)
    assert ep.is_blowoff_latched(coin="AAVE", direction="up",
                                 now_ms=2000, path=p) is True
    # 反向（down）不受影响
    assert ep.is_blowoff_latched(coin="AAVE", direction="down",
                                 now_ms=2000, path=p) is False


def test_latch_clear_releases(tmp_path) -> None:
    p = str(tmp_path / "latch.json")
    ep.set_blowoff(coin="AAVE", direction="up", now_ms=1000, path=p)
    ep.clear_blowoff(coin="AAVE", direction="up", path=p)
    assert ep.is_blowoff_latched(coin="AAVE", direction="up",
                                 now_ms=2000, path=p) is False


def test_latch_expires_by_max_age(tmp_path) -> None:
    p = str(tmp_path / "latch.json")
    ep.set_blowoff(coin="AAVE", direction="up", now_ms=1000, path=p)
    # 超过 max_age_ms → 视为过期，fail-open
    assert ep.is_blowoff_latched(
        coin="AAVE", direction="up", now_ms=1000 + 2000,
        max_age_ms=1000, path=p) is False
    # 未过期仍锁
    assert ep.is_blowoff_latched(
        coin="AAVE", direction="up", now_ms=1500,
        max_age_ms=1000, path=p) is True


def test_release_if_cooled(tmp_path) -> None:
    p = str(tmp_path / "latch.json")
    ep.set_blowoff(coin="AAVE", direction="up", now_ms=0, path=p)
    # cooldown 未满 → 不释放
    assert ep.release_if_cooled(
        coin="AAVE", direction="up", cooldown_ms=1000,
        now_ms=500, path=p) is False
    assert ep.is_blowoff_latched(coin="AAVE", direction="up",
                                 now_ms=500, path=p) is True
    # cooldown 满 → 释放
    assert ep.release_if_cooled(
        coin="AAVE", direction="up", cooldown_ms=1000,
        now_ms=1500, path=p) is True
    assert ep.is_blowoff_latched(coin="AAVE", direction="up",
                                 now_ms=1500, path=p) is False


def test_latch_set_idempotent(tmp_path) -> None:
    p = str(tmp_path / "latch.json")
    ep.set_blowoff(coin="AAVE", direction="up", now_ms=1000, path=p)
    ep.set_blowoff(coin="AAVE", direction="up", now_ms=2000, path=p)
    assert ep.is_blowoff_latched(coin="AAVE", direction="up",
                                 now_ms=2000, path=p) is True
