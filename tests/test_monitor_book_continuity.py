"""book-raw 采集连续性监控的离线单测。"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import pytest

from scripts.monitor_book_continuity import (
    evaluate_coin,
    missing_day_dirs,
    read_timestamps,
    run_check,
    timestamp_gaps,
)

S = 1000


def _write_book(book_root: Path, day: str, coin: str, ts: list[int],
                gz: bool = False) -> Path:
    ddir = book_root / f"date={day}"
    ddir.mkdir(parents=True, exist_ok=True)
    name = f"{coin}.jsonl.gz" if gz else f"{coin}.jsonl"
    path = ddir / name
    opener = gzip.open if gz else open
    with opener(path, "wt") as f:
        for t in ts:
            f.write(json.dumps({"t": t, "b": [], "a": []}) + "\n")
    return path


def test_timestamp_gaps_detects_worst_and_breaches():
    worst, breaches = timestamp_gaps([0, 1000, 2000, 20000], max_gap_s=15)
    assert worst == 18000
    assert breaches == 1


def test_timestamp_gaps_all_regular():
    ts = list(range(0, 5000, 1000))
    worst, breaches = timestamp_gaps(ts, max_gap_s=15)
    assert worst == 1000 and breaches == 0


def test_evaluate_coin_flags_stale_today():
    now = 100_000
    issues = evaluate_coin(coin="BTC", ts_ms=[0, 1000, 2000], now_ms=now,
                           stale_s=30, max_gap_s=15, is_today=True)
    kinds = [i.kind for i in issues]
    assert "stale" in kinds


def test_evaluate_coin_flags_tail_gap_when_fresh():
    # 末行新鲜，但最后一段间隔 19s > 15s：断流后恢复，报 gap 不报 stale
    now = 20_000
    issues = evaluate_coin(coin="BTC", ts_ms=[0, 1000, 2000, 20_000],
                           now_ms=now, stale_s=30, max_gap_s=15, is_today=True)
    kinds = [i.kind for i in issues]
    assert "gap" in kinds and "stale" not in kinds


def test_evaluate_coin_middle_gap_recovered_is_quiet():
    # 中间曾有大缺口，但末端恢复为 1s 节奏且末行新鲜：不应告警
    now = 100_000
    ts = list(range(0, 90_000, 1000)) + [95_000, 96_000, 97_000, 98_000,
                                         99_000, 100_000]
    issues = evaluate_coin(coin="BTC", ts_ms=ts, now_ms=now, stale_s=30,
                           max_gap_s=15, is_today=True)
    assert issues == []


def test_evaluate_coin_healthy_history_day_not_stale():
    # 历史文件末行本就陈旧，但 is_today=False 不应报 stale
    issues = evaluate_coin(coin="BTC", ts_ms=[0, 1000, 2000], now_ms=10**9,
                           stale_s=30, max_gap_s=15, is_today=False)
    assert all(i.kind != "stale" for i in issues)


def test_evaluate_coin_empty_today_is_stale():
    issues = evaluate_coin(coin="ETH", ts_ms=[], now_ms=5000, stale_s=30,
                           max_gap_s=15, is_today=True)
    assert issues and issues[0].kind == "stale"


def test_missing_day_dirs_detects_gap(tmp_path):
    # 固定 now：2026-10-06 12:00 UTC（ms）
    now_ms = int(time.mktime((2026, 10, 6, 12, 0, 0, 0, 0, 0)) * 1000)
    # 上面 mktime 用本地时区，不稳定；改为直接构造已知 UTC 毫秒。
    # 2026-10-06 00:00 UTC 的 epoch 秒（手算以日历为锚，用 calendar 校验）
    import calendar
    now_ms = calendar.timegm((2026, 10, 6, 12, 0, 0, 0, 0, 0)) * 1000
    # 只造 10-05（k=1），缺 10-04/10-03
    _write_book(tmp_path, "2026-10-05", "BTC", [1, 2])
    missing = missing_day_dirs(book_root=tmp_path, now_ms=now_ms,
                               lookback_days=3)
    assert missing == ["2026-10-04", "2026-10-03"]


def test_missing_day_dirs_all_present(tmp_path):
    import calendar
    now_ms = calendar.timegm((2026, 10, 6, 12, 0, 0, 0, 0, 0)) * 1000
    for day in ("2026-10-03", "2026-10-04", "2026-10-05"):
        _write_book(tmp_path, day, "BTC", [1, 2])
    assert missing_day_dirs(book_root=tmp_path, now_ms=now_ms,
                            lookback_days=3) == []


def test_read_timestamps_plain_and_gz(tmp_path):
    _write_book(tmp_path, "2026-10-06", "BTC", [3, 1, 2])
    p = tmp_path / "date=2026-10-06" / "BTC.jsonl"
    assert read_timestamps(p, cap_lines=0) == [1, 2, 3]
    pg = _write_book(tmp_path, "2026-10-06", "ETH", [5, 4], gz=True)
    assert read_timestamps(pg, cap_lines=0) == [4, 5]


def test_run_check_integration(tmp_path):
    import calendar
    now_ms = calendar.timegm((2026, 10, 6, 12, 0, 0, 0, 0, 0)) * 1000
    # 历史日齐全
    for day in ("2026-10-03", "2026-10-04", "2026-10-05"):
        _write_book(tmp_path, day, "BTC", [1, 2])
    # 今日 BTC 新鲜（每秒一行直到 now 附近）
    _write_book(tmp_path, "2026-10-06", "BTC",
                list(range(now_ms - 5 * S, now_ms, S)))
    reports, missing = run_check(book_root=tmp_path, now_ms=now_ms,
                                 stale_s=30, max_gap_s=15,
                                 lookback_days=3, cap_lines=0)
    assert missing == []
    assert reports[0].coins_checked == 1
    assert reports[0].issues == []


def test_run_check_ignores_rotated_out_coin(tmp_path):
    import calendar
    now_ms = calendar.timegm((2026, 10, 6, 12, 0, 0, 0, 0, 0)) * 1000
    for day in ("2026-10-03", "2026-10-04", "2026-10-05"):
        _write_book(tmp_path, day, "BTC", [1, 2])
    # BTC 仍在采集（新鲜）
    _write_book(tmp_path, "2026-10-06", "BTC",
                list(range(now_ms - 5 * S, now_ms, S)))
    # ONDO 12 分钟前被轮换出（文件停在 720s 前）
    drop = now_ms - 720 * S
    _write_book(tmp_path, "2026-10-06", "ONDO",
                list(range(drop - 5 * S, drop, S)))
    reports, missing = run_check(book_root=tmp_path, now_ms=now_ms,
                                 stale_s=30, max_gap_s=15,
                                 lookback_days=3, cap_lines=0)
    assert missing == []
    flagged = [i.coin for i in reports[0].issues]
    assert "ONDO" not in flagged and "BTC" not in flagged


def test_run_check_flags_total_outage(tmp_path):
    import calendar
    now_ms = calendar.timegm((2026, 10, 6, 12, 0, 0, 0, 0, 0)) * 1000
    for day in ("2026-10-03", "2026-10-04", "2026-10-05"):
        _write_book(tmp_path, day, "BTC", [1, 2])
    # 所有币都停在 10 分钟前 = 整体中断
    dead = now_ms - 600 * S
    _write_book(tmp_path, "2026-10-06", "BTC",
                list(range(dead - 5 * S, dead, S)))
    reports, missing = run_check(book_root=tmp_path, now_ms=now_ms,
                                 stale_s=30, max_gap_s=15,
                                 lookback_days=3, cap_lines=0)
    assert any(i.coin == "BTC" and i.kind == "stale"
               for i in reports[0].issues)
