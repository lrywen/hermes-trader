#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B-6：回测币池 vs 实盘可交易币池一致性校验。

统一技术改造文档 §2.4「差异 1：币池」：回测若包含已下架币（凭空成交）或漏掉
实盘新币（漏评），其结果都不能映射到生产。本脚本做自动 diff，供 W2 闸门与
日常 CI/cron 调用。

两个币池来源：
  * 回测币池（--bt）：
      - 指向回测 run_meta.jsonl 目录或文件 → 从每行的 coins/coin 字段提取实际
        参与币；
      - 指向 JSON（含 bt_ready_pool 列表或纯列表）→ 直接读取；
      - 缺省回退到 logs/funding_cache 同级的研究池推断（见下）。
  * 实盘币池（--live）：
      - 默认离线：用 logs/funding_cache 目录里仍在更新的 funding 历史文件名
        （代表当前 HL 上线且本系统在采的币）；
      - JSON 文件（纯币名列表 / {name:...} 结构）→ 直接读取；
      - --online：实时拉 metaAndAssetCtxs（需网络，marker=online，脚本本身不
        强加 pytest 标记，CI 离线路径不调用）。

输出两类差异：
  STALE   回测有、实盘无 —— 已退市/未上线却在回测里成交（危险，必须处理）；
  MISSING 实盘有、回测无 —— 新上线币未纳入回测（漏评）。

退出码：
  0  完全一致
  4  存在 STALE（硬错误：回测含不可交易币）
  5  仅 MISSING（告警：回测漏币）
  2  参数/数据源错误
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent


# ── 回测币池 ──────────────────────────────────────────────────────────────

def _coins_from_run_meta(path: Path) -> set[str]:
    coins: set[str] = set()
    files = [path] if path.is_file() else sorted(path.glob("**/run_meta*.jsonl"))
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = row.get("coins") or row.get("coin")
            if isinstance(c, list):
                coins.update(str(x) for x in c)
            elif isinstance(c, str):
                coins.add(c)
    return coins


def _coins_from_json(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("bt_ready_pool", "pool", "coins"):
            if isinstance(data.get(key), list):
                return {str(x) for x in data[key]}
        raise ValueError(f"{path}: 未找到币名列表字段（bt_ready_pool/pool/coins）")
    if isinstance(data, list):
        return {str(x) for x in data}
    raise ValueError(f"{path}: 不支持的 JSON 结构 {type(data).__name__}")


def resolve_bt_pool(arg: Optional[str]) -> set[str]:
    if arg is None:
        # 缺省：优先用研究池 JSON，再退到 funding_cache。
        recon = Path("/tmp/p6b1_recon.json")
        if recon.exists():
            return _coins_from_json(recon)
        return _live_pool_from_funding_cache()
    p = Path(arg)
    if not p.exists():
        raise FileNotFoundError(arg)
    if p.is_dir() or p.suffix == ".jsonl":
        return _coins_from_run_meta(p)
    return _coins_from_json(p)


# ── 实盘币池 ──────────────────────────────────────────────────────────────

def _live_pool_from_funding_cache() -> set[str]:
    d = REPO / "logs/funding_cache"
    return {f.stem for f in d.glob("*.json") if f.stem}


def _live_pool_online() -> set[str]:
    from hermes_trader.client.hl_client import _http_post
    meta = _http_post("/info", {"type": "meta"})
    if not isinstance(meta, dict):
        raise RuntimeError(f"meta 返回非对象: {meta!r}")
    return {str(u.get("name")) for u in meta.get("universe", []) if u.get("name")}


def resolve_live_pool(arg: Optional[str], online: bool) -> set[str]:
    if online:
        return _live_pool_online()
    if arg is None:
        return _live_pool_from_funding_cache()
    p = Path(arg)
    if not p.exists():
        raise FileNotFoundError(arg)
    return _coins_from_json(p)


# ── 报告 ──────────────────────────────────────────────────────────────────

def diff_pools(bt: set[str], live: set[str]) -> tuple[set[str], set[str]]:
    return bt - live, live - bt  # stale, missing


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bt", help="回测币池来源（run_meta 文件/目录、研究 JSON）")
    ap.add_argument("--live", help="实盘币池 JSON（缺省用 logs/funding_cache）")
    ap.add_argument("--online", action="store_true",
                    help="实时拉取 HL meta 作为实盘币池（需联网）")
    args = ap.parse_args()

    try:
        bt = resolve_bt_pool(args.bt)
        live = resolve_live_pool(args.live, args.online)
    except Exception as e:
        print(f"[pool-check] 数据源错误: {e}", file=sys.stderr)
        return 2

    stale, missing = diff_pools(bt, live)
    print("=" * 64)
    print(f"B-6 币池一致性 | 回测 {len(bt)} 币 / 实盘 {len(live)} 币")
    print(f"STALE（回测有、实盘无，已退市凭空成交）: {len(stale)}")
    for c in sorted(stale):
        print(f"  - {c}")
    print(f"MISSING（实盘有、回测无，漏评新币）: {len(missing)}")
    for c in sorted(missing):
        print(f"  + {c}")
    print("=" * 64)

    if stale:
        print("结果：不一致（STALE，硬错误）")
        return 4
    if missing:
        print("结果：回测漏币（MISSING，告警）")
        return 5
    print("结果：一致 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
