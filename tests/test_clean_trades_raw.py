"""scripts/clean_trades_raw.py 清理逻辑测试。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from scripts.clean_trades_raw import cleanup, expired_dirs


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
