#!/usr/bin/env python3
"""验证 40 -> 60 暴涨场景的复盘报告生成与横幅推送。

场景：
  - 周期 1：score=40（gate 以下，纯趋势，无动量触发器）
  - 周期 2：score=60（跳升 +20，超过 min_jump=15，并伴随 momentumBurst/breakout）
  - 预期：触发复盘，打印 [SURGE_POSTMORTEM] 横幅，写 markdown 到 /data/postmortems/

同时跑一个反例（40 -> 52，跳升 12 分 < 15）确认不会误报。

运行：
    docker exec hermes-trader python scripts/verify_surge_40_to_60.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from hermes_trader.surge_postmortem import (
    POSTMORTEM_DIR,
    SurgeConfig,
    SurgeDetector,
)

COIN = "SIM4060"


def _trigger(name: str, score: float, fired: bool, reason: str = "") -> dict:
    return {"name": name, "score": score, "fired": fired, "reason": reason}


def _triggers_cycle1():
    """周期 1：40 分，只有趋势类，无动量触发器。"""
    return [
        _trigger("trendStrength", 22.0, True, "uptrend 1h"),
        _trigger("higherLows1h", 12.0, True, "higher lows"),
        _trigger("momentumBurst", 0.0, False, ""),
        _trigger("breakout", 0.0, False, ""),
        _trigger("pctMoveSpike", 0.0, False, ""),
    ]


def _triggers_cycle2():
    """周期 2：60 分，动量触发器同时点火。"""
    return [
        _trigger("trendStrength", 20.0, True, "strong trend"),
        _trigger("momentumBurst", 20.0, True,
                 "5m return > 2.0ATR in 3 bars"),
        _trigger("breakout", 14.0, True, "20-bar high + RVOL 3.4x"),
        _trigger("volumeSpike", 6.0, True, "RVOL 3.4x"),
        _trigger("pctMoveSpike", 0.0, False, ""),
    ]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    )
    rc = 0

    # ── 正例：40 -> 60，跳升 +20，动量确认 ─────────────────────────
    print("=" * 64)
    print(" 正例：SIM4060 composite 40 -> 60 (jump +20, +momentum)")
    print("=" * 64)
    # Synchronous report + no persistence: we assert on the markdown file
    # immediately after observe(), and the two detectors below must not see
    # each other's (or the live loop's) snapshot.
    det = SurgeDetector(SurgeConfig(min_score=54.0, min_jump=15.0,
                                    async_report=False, persist_state=False))

    det.observe(COIN, 40.0, _triggers_cycle1())
    fired = det.observe(
        COIN, 60.0, _triggers_cycle2(),
        perception={"coin": COIN, "mid": 65000.0, "composite_score": 60.0},
    )

    if not fired:
        print("FAIL: 正例应触发 surge 但被抑制")
        rc = 1
    else:
        reports = sorted(POSTMORTEM_DIR.glob(f"surge-{COIN}-*.md"))
        if not reports:
            print("FAIL: 横幅已触发但未找到 markdown 报告")
            rc = 1
        else:
            latest = reports[-1]
            size = latest.stat().st_size
            print(f"\nOK: surge fired=True, report written ({size} bytes)")
            print(f"    {latest}")

    # ── 反例：40 -> 52，跳升 12 < 15，不应触发 ─────────────────────
    print()
    print("=" * 64)
    print(" 反例：SLOWJUMP composite 40 -> 52 (jump +12 < 15) -> 应抑制")
    print("=" * 64)
    det2 = SurgeDetector(SurgeConfig(min_score=54.0, min_jump=15.0,
                                     async_report=False, persist_state=False))
    det2.observe("SLOWJUMP", 40.0, _triggers_cycle1())
    false_fired = det2.observe("SLOWJUMP", 52.0, _triggers_cycle2())
    if false_fired:
        print("FAIL: 跳升仅 +12 分却触发了 surge（误报）")
        rc = 1
    else:
        print("OK: 跳升 +12 < min_jump=15，正确抑制，无误报")

    print()
    if rc == 0:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
    return rc


if __name__ == "__main__":
    sys.exit(main())
