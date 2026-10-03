"""scripts/clean_trades_raw.py 清理逻辑测试。"""
from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

from scripts.clean_trades_raw import (archive_book_raw, archive_dir, cleanup,
                                      expired_dirs)


def _make(base: Path, day: str) -> Path:
    d = base / f"date={day}"
    d.mkdir(parents=True)
    (d / "BTC.jsonl").write_text("{}")
    return d


def test_expired_dirs_keeps_retention_window() -> None:
    base = Path("/tmp/ctr_test1")
    if base.exists():
        import shutil
        shutil.rmtree(base)
    base.mkdir(parents=True)
    today = date(2026, 9, 26)
    old = _make(base, "2026-09-10")   # 16天前 → 过期
    boundary = _make(base, "2026-09-13")  # cutoff当天 → 保留
    recent = _make(base, "2026-09-25")
    _ = (old, boundary, recent)

    expired = expired_dirs(base, today=today, retention_days=14)
    names = [p.name for p in expired]
    assert names == ["date=2026-09-10"]


def test_cleanup_ignores_non_date_entries() -> None:
    base = Path("/tmp/ctr_test2")
    import shutil
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    (base / "random.txt").write_text("x")
    (base / "notadate=foo").mkdir()
    removed = cleanup(base, today=date(2026, 9, 26),
                      retention_days=14, dry_run=False)
    assert removed == []
    assert (base / "random.txt").exists()


def test_cleanup_dry_run_removes_nothing() -> None:
    base = Path("/tmp/ctr_test3")
    import shutil
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    target = _make(base, "2026-01-01")
    removed = cleanup(base, today=date(2026, 9, 26),
                      retention_days=14, dry_run=True)
    assert removed == ["date=2026-01-01"]
    assert target.exists()


def test_cleanup_actually_removes() -> None:
    base = Path("/tmp/ctr_test4")
    import shutil
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    target = _make(base, "2026-01-01")
    removed = cleanup(base, today=date(2026, 9, 26),
                      retention_days=14, dry_run=False)
    assert removed == ["date=2026-01-01"]
    assert not target.exists()


def test_archive_dir_gzips_and_removes_original(tmp_path: Path) -> None:
    import shutil
    day = tmp_path / "date=2026-09-01"
    day.mkdir()
    rows = ['{"t":1}\n', '{"t":2}\n', '{"t":3}\n']
    (day / "BTC.jsonl").write_text("".join(rows))
    out = archive_dir(day, dry_run=False)
    assert out == ["BTC.jsonl.gz"]
    gz = day / "BTC.jsonl.gz"
    assert gz.exists()
    assert not (day / "BTC.jsonl").exists()
    assert not (day / "BTC.jsonl.gz.tmp").exists()
    with gzip.open(gz, "rt") as fh:
        assert fh.readlines() == rows


def test_archive_dir_dry_run_changes_nothing(tmp_path: Path) -> None:
    day = tmp_path / "date=2026-09-01"
    day.mkdir()
    f = day / "BTC.jsonl"
    f.write_text('{"t":1}\n')
    assert archive_dir(day, dry_run=True) == ["BTC.jsonl.gz"]
    assert f.exists()
    assert not (day / "BTC.jsonl.gz").exists()


def test_archive_book_raw_skips_today(tmp_path: Path) -> None:
    today = date(2026, 10, 3)
    old = tmp_path / "book-raw" / "date=2026-10-02"
    cur = tmp_path / "book-raw" / "date=2026-10-03"
    old.mkdir(parents=True)
    cur.mkdir(parents=True)
    (old / "BTC.jsonl").write_text('{"t":1}\n{"t":2}\n')
    cur_file = cur / "BTC.jsonl"
    cur_file.write_text('{"t":9}\n')
    out = archive_book_raw(tmp_path / "book-raw", today=today, dry_run=False)
    assert out == ["date=2026-10-02/BTC.jsonl.gz"]
    assert (old / "BTC.jsonl.gz").exists()
    assert not (old / "BTC.jsonl").exists()
    # 当日目录原封不动
    assert cur_file.exists()
    assert not (cur / "BTC.jsonl.gz").exists()


def test_archive_book_raw_idempotent_on_gz(tmp_path: Path) -> None:
    old = tmp_path / "book-raw" / "date=2026-10-01"
    old.mkdir(parents=True)
    (old / "BTC.jsonl").write_text('{"t":1}\n')
    base = tmp_path / "book-raw"
    archive_book_raw(base, today=date(2026, 10, 3), dry_run=False)
    # 再跑一次：已无 .jsonl，不应重复处理
    assert archive_book_raw(base, today=date(2026, 10, 3), dry_run=False) == []
