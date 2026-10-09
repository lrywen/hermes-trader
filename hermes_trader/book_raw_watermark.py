"""Read-only disk watermark for the L2 order-book capture (P1).

The book-raw tree is deliberately kept for a 90-day pre-registration window
(see scripts/clean_trades_raw.py), so it cannot be trimmed early and its steady
state is several GB. This module only *measures* the footprint and the volume's
free percentage so the daily report can flag capacity pressure; it never
deletes or modifies anything.

Pure/best-effort: any failure yields None values and never raises into the
report path.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Optional

from hermes_trader.loop_runtime import data_dir

# Volume-use fraction at/above which the report should flag a warning.
WARN_VOLUME_USED_PCT = 80.0


def _human_mb(n_bytes: int) -> float:
    return round(n_bytes / (1024 * 1024), 1)


def directory_size(path: str) -> int:
    """Total size of all regular files under ``path`` (symlinks not followed)."""
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if not os.path.islink(fp):
                total += st.st_size
    return total


def book_raw_watermark(*, root: Optional[str] = None) -> dict[str, Any]:
    """Book-raw footprint + volume free percentage + warning flag.

    Keys: book_raw_mb, volume_total_mb, volume_used_pct, volume_free_pct,
    warning. Missing book-raw dir -> book_raw_mb None (nothing captured yet).
    Never raises.
    """
    out: dict[str, Any] = {
        "book_raw_mb": None,
        "volume_total_mb": None,
        "volume_used_pct": None,
        "volume_free_pct": None,
        "warning": False,
    }
    try:
        base = root if root is not None else data_dir()
        book_dir = os.path.join(base, "book-raw")
        if os.path.isdir(book_dir):
            out["book_raw_mb"] = _human_mb(directory_size(book_dir))

        usage = shutil.disk_usage(base)
        total_mb = _human_mb(usage.total)
        used_pct = round(usage.used / usage.total * 100.0, 1)
        free_pct = round(usage.free / usage.total * 100.0, 1)
        out["volume_total_mb"] = total_mb
        out["volume_used_pct"] = used_pct
        out["volume_free_pct"] = free_pct
        out["warning"] = used_pct >= WARN_VOLUME_USED_PCT
    except Exception:
        # Best-effort: leave whatever could be measured, warning stays False.
        pass
    return out


def format_line(wm: dict[str, Any]) -> str:
    """One human-readable report line for the daily report."""
    mb = wm.get("book_raw_mb")
    book = f"{mb}MB" if mb is not None else "无采集"
    used = wm.get("volume_used_pct")
    free = wm.get("volume_free_pct")
    total = wm.get("volume_total_mb")
    vol = (f"卷 {total}MB 已用 {used}%/剩余 {free}%"
           if used is not None else "卷容量未知")
    flag = " ⚠️容量告警" if wm.get("warning") else ""
    return f"L2盘口落盘: {book}；{vol}{flag}"
