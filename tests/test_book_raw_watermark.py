"""Tests for the read-only L2 book-raw disk watermark (P1)."""
from __future__ import annotations

import os

from hermes_trader import book_raw_watermark as bw


def test_watermark_reports_book_size_and_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    book = tmp_path / "book-raw" / "date=2026-10-09"
    book.mkdir(parents=True)
    # 2 MB of payload across two files.
    payload = b"x" * (1024 * 1024)
    (book / "BTC.jsonl").write_bytes(payload)
    (book / "ETH.jsonl").write_bytes(payload)

    wm = bw.book_raw_watermark()
    assert wm["book_raw_mb"] == 2.0
    assert wm["volume_total_mb"] is not None and wm["volume_total_mb"] > 0
    assert 0.0 <= wm["volume_used_pct"] <= 100.0
    assert 0.0 <= wm["volume_free_pct"] <= 100.0
    # used+free can be < 100 (reserved/root blocks), never more.
    assert wm["volume_used_pct"] + wm["volume_free_pct"] <= 100.01


def test_watermark_missing_book_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    wm = bw.book_raw_watermark()
    assert wm["book_raw_mb"] is None
    # Volume stats are still available without a book-raw tree.
    assert wm["volume_free_pct"] is not None


def test_warning_threshold(monkeypatch):
    # Force a near-full volume by monkeypatching disk_usage.
    class _U:
        total = 100
        used = 85
        free = 15

    monkeypatch.setattr(bw.shutil, "disk_usage", lambda p: _U())
    wm = bw.book_raw_watermark(root="/nonexistent")
    # book dir missing under this root -> None, but warning reflects volume.
    assert wm["book_raw_mb"] is None
    assert wm["volume_used_pct"] == 85.0
    assert wm["warning"] is True


def test_no_warning_below_threshold(monkeypatch):
    class _U:
        total = 100
        used = 50
        free = 50

    monkeypatch.setattr(bw.shutil, "disk_usage", lambda p: _U())
    wm = bw.book_raw_watermark(root="/nonexistent")
    assert wm["warning"] is False


def test_format_line_contains_key_parts():
    line = bw.format_line({
        "book_raw_mb": 896.0,
        "volume_total_mb": 50_000,
        "volume_used_pct": 55.0,
        "volume_free_pct": 45.0,
        "warning": False,
    })
    assert "896.0MB" in line
    assert "55.0%" in line
    assert "L2盘口落盘" in line
    assert "告警" not in line


def test_format_line_warning_flag():
    line = bw.format_line({
        "book_raw_mb": 4000.0,
        "volume_total_mb": 5000,
        "volume_used_pct": 82.0,
        "volume_free_pct": 18.0,
        "warning": True,
    })
    assert "容量告警" in line
