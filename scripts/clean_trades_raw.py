#!/usr/bin/env python3
"""清理过期的公共原始落盘（trades 逐笔 + L2 order book 快照）。

trades_capture 会按 UTC 日期把逐笔成交写入：
    ${HERMES_DATA_DIR:-/data}/trades-raw/date=YYYY-MM-DD/COIN.jsonl
book_capture 会按 UTC 日期把 L2 盘口快照写入：
    ${HERMES_DATA_DIR:-/data}/book-raw/date=YYYY-MM-DD/COIN.jsonl
长期运行会持续占用磁盘。本脚本：
  * 删除保留期之外的 ``date=`` 目录（只按目录名中的日期判定，不依赖 mtime）；
  * 对 book-raw 中**已滚过的非当日**目录里的 ``*.jsonl`` 做 gzip 归档
    （``COIN.jsonl`` -> ``COIN.jsonl.gz``），不缩短 90 天窗口。JSONL 文本可压
    5-10×，消除 M-1 确认窗口（2026-11-27）前磁盘写满的风险。
由 scheduler 每日 00:00 UTC 触发。

安全边界：
  * 只处理形如 ``date=YYYY-MM-DD`` 的目录，其它文件/目录一律不碰。
  * 默认保留近3天（book-raw 为 90 天，保护 M-1 检验窗口）；``--dry-run`` 只打印不删除。
  * 绝不压缩当日目录：book flusher 只以 append 方式持有当日 ``*.jsonl`` 文件句柄，
    旧日期目录不会被重新打开；先写 ``.tmp`` 再原子 rename，校验通过后才删原文件。
"""
from __future__ import annotations

import argparse
import gzip
import logging
import os
import re
import shutil
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


def archive_dir(day_dir: Path, *, dry_run: bool) -> list[str]:
    """gzip 归档单个 date= 目录内尚未压缩的 ``*.jsonl``，返回归档文件名列表。

    每个文件：流式写到 ``COIN.jsonl.gz.tmp`` -> 校验 gzip 完整且行数一致 ->
    原子 rename 为 ``.jsonl.gz`` -> 删除原 ``.jsonl``。调用方必须保证
    ``day_dir`` 不是当日目录（flusher 不会再写）。"""
    archived: list[str] = []
    for path in sorted(day_dir.glob("*.jsonl")):
        gz_path = path.with_name(path.name + ".gz")
        tmp_path = gz_path.with_name(gz_path.name + ".tmp")
        if dry_run:
            logger.info("[dry-run] would gzip %s", path)
            archived.append(gz_path.name)
            continue
        n_in = 0
        with open(path, "rb") as src, gzip.open(tmp_path, "wb", compresslevel=6) as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                n_in += chunk.count(b"\n")
                dst.write(chunk)
        # 校验：gzip 可完整解压且行数一致，避免半截归档冒充成功。
        n_out = 0
        with gzip.open(tmp_path, "rb") as chk:
            while True:
                chunk = chk.read(1 << 20)
                if not chunk:
                    break
                n_out += chunk.count(b"\n")
        if n_out != n_in:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"archive line mismatch for {path}: {n_in} -> {n_out}")
        os.replace(tmp_path, gz_path)
        path.unlink()
        logger.info("gzipped %s (%d lines)", gz_path, n_out)
        archived.append(gz_path.name)
    return archived


def archive_book_raw(base: Path, *, today: date, dry_run: bool) -> list[str]:
    """压缩 book-raw 下所有非当日 date= 目录中的 jsonl，返回 "日期/文件" 列表。"""
    out: list[str] = []
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        m = _DIR_RE.match(child.name)
        if not m or not child.is_dir():
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d >= today:  # 当日目录绝不动
            continue
        for name in archive_dir(child, dry_run=dry_run):
            out.append(f"{child.name}/{name}")
    return out



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
        if base.name == "book-raw":
            # 先删过期目录，再压缩剩余旧日期；两者作用于不同日期，互不重叠。
            archived = archive_book_raw(base, today=today, dry_run=args.dry_run)
            if archived:
                print(f"{base}: 压缩归档 {len(archived)} 个文件")
            all_removed.extend(f"{base.name}::{n}" for n in archived)
    print(f"{'[dry-run] ' if args.dry_run else ''}合计处理 {len(all_removed)} 个目录/文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
