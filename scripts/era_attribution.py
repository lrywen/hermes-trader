#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阈值 era 分段归因（CS-E 第二切口，纯离线 / 只读）
=================================================
把每笔真实成交 / 风控拦截归到其下单时刻**实际生效**的阈值配置版本（era），
按 era 分段对比，避免用事后阈值去解释阈值生效前的交易。

本脚本绝不连接交易所、绝不下单、绝不写交易状态；只读取 events.jsonl
（权威、长留、hash 链）与可选的当前配置文件，输出 JSON 报告 + 控制台简表。

era 指纹
  由 config_store.compute_config_era / ERA_TRACKED_PATHS 单一事实源决定：
  仅对入场选择 / 风控熔断 / 仓位与 DSL 退出 / 实验臂这几类阈值取 canonical
  JSON 的 SHA-256(12)。无关键（mode、日志路径等）改动不产生新 era。

埋点与历史口径
  - 埋点后（executor 在 order/close 携带 config_era_id / config_era_at_entry）：
    直接采用成交自带 era_id，confidence=high。
  - 埋点前：用配置变更事件（config_update / config_write，含 old/new）把时间轴
    切段，并从当前配置终态逆序回放重建各段阈值；回滚链证据完整的段标 medium，
    遇到仅 changed_keys 无值（2026-09-08 CS-A 老审计）等证据缺口，其更早各段
    一律降为 low；早于第一个已知变更边界的成交归 pre_instrumentation 单桶。
    任何情况下都不伪造精确边界。

风控 / 实验臂
  - risk_gate（不产生成交的拦截）按其时间戳所落区间归因，按 block_reasons 聚合。
  - 实验臂 off 臂无真实成交；仅当显式 --xs-shadow 指向 xs_reversal 影子日志时
    做 would-be 信号计数，并明确标注非真实成交。

用法（容器内）：
  python3 scripts/era_attribution.py                       # 打印简表
  python3 scripts/era_attribution.py --json /data/era_report.json
  python3 scripts/era_attribution.py --since "2026-09-08 00:00"
  python3 scripts/era_attribution.py --xs-shadow /data/xs_reversal_shadow.jsonl
"""

import argparse
import copy
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from hermes_trader.agents import config_store as cs

DEFAULT_EVENTS = os.environ.get("HERMES_EVENTS_FILE", "/data/events.jsonl")
DEFAULT_CONFIG = os.environ.get("HERMES_AGENT_CONFIG_FILE", "/data/.agent-config.json")

CONFIG_CHANGE_EVENTS = ("config_write", "config_update")
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
CONF_PRE = "pre_instrumentation"
PRE_ERA = "pre_instrumentation"
UNKNOWN_ERA = "unknown"


def _iso_to_ms(iso):
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _event_ms(rec):
    return _iso_to_ms(rec.get("timestamp"))


def tracked_top_level_keys():
    out = []
    for p in cs.ERA_TRACKED_PATHS:
        top = p[:-2].split(".")[0] if p.endswith(".*") else p.split(".")[0]
        if top not in out:
            out.append(top)
    return out


_TOP_KEYS = tracked_top_level_keys()


def load_events(path):
    recs = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return recs


def load_terminal_top_view(path):
    try:
        with open(path, "r") as f:
            raw = json.load(f)
        eff = cs._effective_view_for_diff(raw)
        return {k: eff.get(k) for k in _TOP_KEYS}, True
    except (OSError, json.JSONDecodeError):
        return {}, False


def _restrict_top(d):
    if not isinstance(d, dict):
        return None
    out = {k: d[k] for k in _TOP_KEYS if k in d}
    return out or None


def build_boundaries(events):
    by_ts = {}
    for rec in events:
        et = rec.get("event")
        if et not in CONFIG_CHANGE_EVENTS:
            continue
        p = rec.get("payload") or {}
        ts = _event_ms(rec)
        if ts is None:
            continue
        new = _restrict_top(p.get("new") if et == "config_write" else p.get("updates"))
        old = _restrict_top(p.get("old"))
        # CS-A 老审计（2026-09-08 前）只记 changed_keys 键名、无 old 值；它仍是
        # 一条真实边界（保留以切段），但无法逆序重建，回放时该段及更早降 low。
        legacy_keys = [k for k in (p.get("changed_keys") or []) if isinstance(k, str) and k in _TOP_KEYS]
        if new is None and old is None and not p.get("era_id") and not legacy_keys:
            continue
        b = by_ts.setdefault(ts, {"ts": ts, "new": {}, "old": {}, "era_id": None, "src": [], "seq": rec.get("seq")})
        if new:
            b["new"].update(new)
        if old:
            b["old"].update(old)
        # 老审计键进入 new 集但不在 old 集 -> 回放时据此识别证据缺口。
        if not old and legacy_keys:
            b["new"].update({k: None for k in legacy_keys})
        if p.get("era_id"):
            b["era_id"] = p["era_id"]
        b["src"].append(et)
    return [by_ts[k] for k in sorted(by_ts)]


def build_segments(boundaries, terminal_top, terminal_ok):
    segs = []
    if not boundaries:
        return segs
    view = copy.deepcopy(terminal_top) if terminal_ok else {}
    broken = not terminal_ok
    n = len(boundaries)
    for i in range(n - 1, -1, -1):
        b = boundaries[i]
        end_ts = boundaries[i + 1]["ts"] if i + 1 < n else None
        tracked_new = set(b.get("new") or {})
        tracked_old = set(b.get("old") or {})
        # 先判缺口：缺可回滚 old（老审计只记键名 / backup=False）时，该边界
        # 之后那段的重建值本身不可验证 -> 本段及更早一律 low。
        if not tracked_new.issubset(tracked_old):
            broken = True
        era_id = b.get("era_id") or (cs._era_id_from_subset(cs._extract_tracked_subset(view)) if view else UNKNOWN_ERA)
        segs.append(
            {
                "start_ts": b["ts"],
                "end_ts": end_ts,
                "era_id": era_id,
                "confidence": CONF_LOW if broken else CONF_MEDIUM,
                "evidence": [f"{src}@seq{b['seq']}" for src in b["src"]],
            }
        )
        for k, v in (b.get("old") or {}).items():
            view[k] = v
    segs.reverse()
    return segs


def segment_at(ts, segments):
    for s in segments:
        if ts >= s["start_ts"] and (s["end_ts"] is None or ts < s["end_ts"]):
            return s
    return None


def _era_for_ts(ts, segments):
    s = segment_at(ts, segments)
    if s is None:
        return PRE_ERA, CONF_PRE, []
    return s["era_id"], s["confidence"], s["evidence"]


def _new_bucket():
    return {
        "confidence": None,
        "evidence": set(),
        "orders": 0,
        "closes": 0,
        "wins": 0,
        "pnl_usd_sum": 0.0,
        "pnl_usd": [],
        "hold_minutes": [],
        "regimes": Counter(),
        "risk_blocks": 0,
        "block_reasons": Counter(),
        "xs_would_be": 0,
    }


def attribute(events, segments, since_ms=None, xs_shadow_path=None):
    eras = defaultdict(_new_bucket)
    rank = {CONF_HIGH: 3, CONF_MEDIUM: 2, CONF_LOW: 1, CONF_PRE: 0}

    def bucket(era_id, conf, evidence):
        e = eras[era_id]
        if e["confidence"] is None or rank.get(conf, -1) > rank.get(e["confidence"], -1):
            e["confidence"] = conf
        e["evidence"].update(evidence or [])
        return e

    for rec in events:
        et = rec.get("event")
        p = rec.get("payload") or {}

        if et == "order":
            ts = p.get("executed_at") or _event_ms(rec)
            if since_ms and ts < since_ms:
                continue
            era = p.get("config_era_id")
            if era:
                b = bucket(era, CONF_HIGH, ["order.config_era_id"])
            else:
                era, conf, ev = _era_for_ts(ts, segments)
                b = bucket(era, conf, ev)
            b["orders"] += 1

        elif et == "close":
            ts = p.get("entry_time") or p.get("closed_at") or _event_ms(rec)
            if since_ms and ts < since_ms:
                continue
            ce = p.get("config_era_at_entry") or {}
            era = ce.get("era_id")
            if era:
                b = bucket(era, CONF_HIGH, ["close.config_era_at_entry"])
            else:
                era, conf, ev = _era_for_ts(ts, segments)
                b = bucket(era, conf, ev)
            b["closes"] += 1
            pnl = p.get("realized_pnl_usd")
            if isinstance(pnl, (int, float)):
                b["pnl_usd"].append(pnl)
                b["pnl_usd_sum"] += pnl
                if pnl > 0:
                    b["wins"] += 1
            hm = p.get("hold_minutes")
            if isinstance(hm, (int, float)):
                b["hold_minutes"].append(hm)
            if p.get("regime_at_entry"):
                b["regimes"][p["regime_at_entry"]] += 1

        elif et == "risk_gate":
            ts = _event_ms(rec)
            if since_ms and ts < since_ms:
                continue
            era, conf, ev = _era_for_ts(ts, segments)
            b = bucket(era, conf, ev)
            b["risk_blocks"] += 1
            for r in p.get("block_reasons") or []:
                b["block_reasons"][str(r)] += 1

    if xs_shadow_path:
        _attribute_xs_shadow(xs_shadow_path, segments, eras, bucket, since_ms)

    return eras


def _attribute_xs_shadow(path, segments, eras, bucket, since_ms):
    for rec in load_events(path):
        ts = rec.get("ts") or _iso_to_ms(rec.get("timestamp")) or rec.get("time")
        if not isinstance(ts, (int, float)):
            continue
        if since_ms and ts < since_ms:
            continue
        era, conf, ev = _era_for_ts(int(ts), segments)
        bucket(era, conf, ev)["xs_would_be"] += 1


def _mean(xs):
    return round(sum(xs) / len(xs), 4) if xs else None


def build_report(eras, terminal_ok):
    rows = []
    for era_id, e in eras.items():
        n = e["closes"]
        rows.append(
            {
                "era_id": era_id,
                "confidence": e["confidence"],
                "orders": e["orders"],
                "closes": n,
                "win_rate": round(e["wins"] / n, 4) if n else None,
                "pnl_usd_sum": round(e["pnl_usd_sum"], 4),
                "pnl_usd_mean": _mean(e["pnl_usd"]),
                "hold_minutes_mean": _mean(e["hold_minutes"]),
                "regimes": dict(e["regimes"]),
                "risk_blocks": e["risk_blocks"],
                "block_reasons": dict(e["block_reasons"]),
                "xs_would_be_signals": e["xs_would_be"],
                "evidence": sorted(e["evidence"]),
            }
        )
    rows.sort(key=lambda r: (r["confidence"] is None, r["era_id"]))
    return {"terminal_config_used": terminal_ok, "eras": rows}


def print_table(report):
    rows = report["eras"]
    if not rows:
        print("（窗口内无 order/close/risk_gate 记录）")
        return
    hdr = f"{'era_id':<16}{'conf':<18}{'ord':>4}{'cls':>5}{'win%':>7}{'pnl_sum':>11}{'blocks':>7}{'xs?':>5}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        wr = f"{r['win_rate'] * 100:.1f}" if r["win_rate"] is not None else "-"
        print(
            f"{r['era_id']:<16}{r['confidence']:<18}{r['orders']:>4}{r['closes']:>5}"
            f"{wr:>7}{r['pnl_usd_sum']:>11.2f}{r['risk_blocks']:>7}{r['xs_would_be_signals']:>5}"
        )
    print(
        "\n注：high=成交自带 era 指纹；medium=历史逆序重建证据完整；"
        "low=证据缺口；pre_instrumentation=早于已知配置边界。"
    )
    print("    xs? 为实验臂 would-be 影子信号计数（非真实成交）。")


def _parse_since(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"无法解析 --since: {s!r}（用 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM'）")


def main(argv=None):
    ap = argparse.ArgumentParser(description="阈值 era 分段离线归因（只读，不下单）")
    ap.add_argument("--events", default=DEFAULT_EVENTS, help="events.jsonl 路径")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="当前配置文件（终态）")
    ap.add_argument("--since", default=None, help="起始时间(UTC) 'YYYY-MM-DD' 或 '... HH:MM'")
    ap.add_argument("--xs-shadow", default=None, help="xs_reversal 影子日志（可选 would-be 计数）")
    ap.add_argument("--json", dest="json_out", default=None, help="把完整 JSON 报告写入该路径")
    args = ap.parse_args(argv)

    events = load_events(args.events)
    if not events:
        print(f"未读到事件：{args.events}", file=sys.stderr)
    terminal_top, terminal_ok = load_terminal_top_view(args.config)
    boundaries = build_boundaries(events)
    segments = build_segments(boundaries, terminal_top, terminal_ok)
    since_ms = _parse_since(args.since)

    eras = attribute(events, segments, since_ms=since_ms, xs_shadow_path=args.xs_shadow)
    report = build_report(eras, terminal_ok)

    print_table(report)
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n完整报告已写入：{args.json_out}")


if __name__ == "__main__":
    main()
