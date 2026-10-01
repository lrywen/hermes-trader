#!/usr/bin/env python3
"""清理过期的公共原始落盘（trades 逐笔 + L2 order book 快照）。

trades_capture 会按 UTC 日期把逐笔成交写入：
    ${HERMES_DATA_DIR:-/data}/trades-raw/date=YYYY-MM-DD/COIN.jsonl
book_capture 会按 UTC 日期把 L2 盘口快照写入：
    ${HERMES_DATA_DIR:-/data}/book-raw/date=YYYY-MM-DD/COIN.jsonl
长期运行会持续占用磁盘。本脚本删除保留期之外的 ``date=`` 目录（只按目录名
中的日期判定，不依赖 mtime），由 scheduler 每日触发。

安全边界：
  * 只处理形如 ``date=YYYY-MM-DD`` 的目录，其它文件/目录一律不碰。
  * 默认保留近3天（book-raw 为 90 天，保护 M-1 检验窗口）；``--dry-run`` 只打印不删除。
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("clean_trades_raw")

_DIR_RE = re.compile(r"^date=(\d{4})-(\d{2})-(\d{2})$")
# trades-raw 是公开逐笔成交，仅用于当天/近期的 dashboard 微结构调试，不被 M-1
# 检验或回放依赖（M-2 用 /mnt/tick Binance）。扩到 Top20 币后每天约 7G，
# 14 天会逼近磁盘上限，故只保留 3 天。可用 HERMES_TRADES_RETENTION_DAYS 覆盖。
DEFAULT_RETENTION_DAYS = 3
# book-raw 是 M-1 L2 OFI 的检验样本：采集器 2026-09 上线，首次检验 2026-10-28、
# 60 天确认 2026-11-27。14 天滚动删除会把预注册窗口截断，故 book-raw 单独保留
# 90 天（覆盖确认日并留余量）。
DEFAULT_BOOK_RETENTION_DAYS = 90


def retention_for(base: Path) -> int:
    if base.name == "book-raw":
        env = os.environ.get("HERMES_BOOK_RETENTION_DAYS")
        return int(env) if env and env.isdigit() else DEFAULT_BOOK_RETENTION_DAYS
    env = os.environ.get("HERMES_TRADES_RETENTION_DAYS")
    return int(env) if env and env.isdigit() else DEFAULT_RETENTION_DAYS


def raw_dir() -> Path:
    return Path(os.environ.get("HERMES_DATA_DIR", "/data")) / "trades-raw"


def raw_dirs() -> list[Path]:
    """本脚本负责清理的全部原始根目录（trades-raw + book-raw）。"""
    data_root = Path(os.environ.get("HERMES_DATA_DIR", "/data"))
    return [data_root / "trades-raw", data_root / "book-raw"]


def expired_dirs(base: Path, *, today: date, retention_days: int
                 ) -> list[Path]:
    """返回严格早于 cutoff 的 date= 目录。cutoff=今天-(retention-1)，
    即保留近 retention_days 天（含今天）。"""
    cutoff = today - timedelta(days=int(retention_days) - 1)
    out: list[Path] = []
    if not base.is_dir():
        return out
    for child in base.iterdir():
        m = _DIR_RE.match(child.name)
        if not m or not child.is_dir():
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d < cutoff:
            out.append(child)
    return sorted(out)


def cleanup(base: Path, *, today: date, retention_days: int,
            dry_run: bool, remover=shutil.rmtree) -> list[str]:
    """删除过期目录，返回被处理的目录名列表。"""
    removed: list[str] = []
    for path in expired_dirs(base, today=today, retention_days=retention_days):
        if dry_run:
            logger.info("[dry-run] would remove %s", path)
        else:
            remover(str(path))
            logger.info("removed %s", path)
        removed.append(path.name)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--retention-days", type=int, default=None,
                    help="覆盖全部原始目录的保留天数；缺省按类型默认（trades-raw 14 / book-raw 90）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.retention_days is not None and args.retention_days < 1:
        ap.error("--retention-days 必须 >= 1")
    today = datetime.now(timezone.utc).date()
    all_removed: list[str] = []
    for base in raw_dirs():
        retention_days = args.retention_days or retention_for(base)
        removed = cleanup(base, today=today,
                          retention_days=retention_days, dry_run=args.dry_run)
        if removed:
            print(f"{base}: 清理 {len(removed)} 个过期目录: {removed}")
        all_removed.extend(f"{base.name}/{n}" for n in removed)
    print(f"{'[dry-run] ' if args.dry_run else ''}合计清理 {len(all_removed)} 个过期目录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
