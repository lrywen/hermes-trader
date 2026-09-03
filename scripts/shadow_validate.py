#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHADOW 模式回测 / 验证脚本
==========================
用途：调整入场参数后，量化验证信号是否真的走通全链路、并进入 shadow_book 纸面账本。

两种模式：
  1) snapshot（默认）：扫描日志中某时间点之后的记录，输出信号漏斗 + 闸门拦截分布
     + shadow_book 纸面成交/持仓/胜率统计。适合"改完参数跑一段时间后回看"。
  2) watch：tail -F 实时滚动统计，Ctrl+C 时打印汇总。适合"改完立刻盯着看"。

信号漏斗各层（与交易循环一致）：
  scan 候选 -> TA 过滤(REJECTED/WEAK/CONFIRMED) -> LLM(研究/熔断) ->
  verdict 路由(PASS/LONG/SHORT) -> runner_entry_gate -> 22 闸门 -> shadow_book.OPEN(paper)

用法（容器内）：
  python3 scripts/shadow_validate.py                 # snapshot，统计全天
  python3 scripts/shadow_validate.py --since "2026-09-03 01:00"
  python3 scripts/shadow_validate.py --watch
  python3 scripts/shadow_validate.py --log /data/trading-loop.log --since "2026-09-03 01:00"
"""
import argparse
import re
import time
from collections import Counter

DEFAULT_LOG = "/data/trading-loop.log"

# ---- 正则 ----
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RE_VERDICT = re.compile(r"verdict=(PASS|LONG|SHORT|CLOSE)")
RE_TA = re.compile(r"\b(REJECTED|WEAK|CONFIRMED)\b")
RE_RUNNER = re.compile(r"\[runner_gate\]\s+(\S+)\s+BLOCKED:\s*(.*)")
RE_GATE_BLOCK = re.compile(r"🚫\s+(\S+)\s+BLOCKED\s+—(.*)")
RE_TRIGGERS = re.compile(r"Triggers=(\d+),\s*conf=([\d.]+),\s*score=(-?[\d.]+),\s*news_risk=(True|False)")
RE_DEBATE = re.compile(r"(\d+)/(\d+)\s+analysts agree")
RE_VOL = re.compile(r"24h volume\s*\$([\d.]+)M\s+below floor")
RE_SHADOW_OPEN = re.compile(r"\[shadow_book\]\s+OPEN\s+(long|short)\s+(\S+).*notional=\$([\d.]+).*lev=(\d+)")
RE_SHADOW_CLOSE = re.compile(r"\[shadow_book\]\s+CLOSE\s+(long|short)\s+(\S+).*pnl=?\$?(-?[\d.]+)")
RE_LLM_OPEN = re.compile(r"LLM circuit OPEN")
RE_LLM_TRIP = re.compile(r"circuit (?:is )?OPEN for|breaker tripped|OPEN for")
RE_LLM_OK = re.compile(r"OpenRouter-OK|openrouter.*200|chat/completions\" 200")
RE_WOULD = re.compile(r"shadow_mode_would_execute|would execute")


class Stats:
    def __init__(self):
        self.verdict = Counter()
        self.ta = Counter()
        self.runner_block = Counter()       # runner_gate 拦截原因分类
        self.runner_conf = []               # 被 confidence 地板拦的 conf 值
        self.gate_events = 0                # 进入 22 闸评估次数
        self.gate_block = Counter()         # 22 闸各闸门命中次数
        self.shadow_opens = []              # (side, coin, notional, lev)
        self.shadow_closes = []             # (side, coin, pnl)
        self.llm_open = 0
        self.llm_trip = 0
        self.llm_ok = 0

    def classify_runner(self, detail):
        if "confidence" in detail and "<" in detail:
            m = re.search(r"confidence\s+([\d.]+)\s*<", detail)
            if m:
                self.runner_conf.append(float(m.group(1)))
            self.runner_block["confidence_floor"] += 1
        elif "trend-only chase" in detail:
            self.runner_block["late_trend_chase"] += 1
        elif "fresh impulse" in detail or "structure" in detail:
            self.runner_block["needs_impulse_structure"] += 1
        elif "rsi" in detail.lower() or "extension" in detail.lower() or "overbought" in detail.lower():
            self.runner_block["rsi_extension"] += 1
        elif "short" in detail.lower():
            self.runner_block["short_rule"] += 1
        else:
            self.runner_block["other"] += 1

    def classify_gates(self, reasons):
        self.gate_events += 1
        if "drawdown halt" in reasons:
            self.gate_block["drawdown(已修复)"] += 1
        if "multi-agent debate blocked" in reasons or "analysts agree" in reasons:
            self.gate_block["debate"] += 1
        if "counter-regime" in reasons or "counter-trend" in reasons or "chop" in reasons or "against funding" in reasons:
            self.gate_block["market_regime"] += 1
        if "below floor" in reasons or "volume" in reasons:
            self.gate_block["liquidity"] += 1
        if "news" in reasons and "news_risk=True" in reasons:
            self.gate_block["news"] += 1
        if "cooldown" in reasons:
            self.gate_block["cooldown"] += 1
        if "consecutive" in reasons:
            self.gate_block["consecutive_loss"] += 1
        if "correlation" in reasons or "correlated" in reasons:
            self.gate_block["correlation"] += 1
        if "daily loss" in reasons or "daily_loss" in reasons:
            self.gate_block["daily_loss"] += 1
        if "max concurrent" in reasons or "concurrent" in reasons:
            self.gate_block["max_concurrent"] += 1

    def feed(self, line):
        m = RE_VERDICT.search(line)
        if m:
            self.verdict[m.group(1)] += 1
        # TA verdict（只统计明确的 TA 判定行，避免误伤）
        if "TA" in line or "ta_" in line:
            mt = RE_TA.search(line)
            if mt and ("verdict" in line.lower() or "TA" in line):
                self.ta[mt.group(1)] += 1
        m = RE_RUNNER.search(line)
        if m:
            self.classify_runner(m.group(2))
            return
        m = RE_GATE_BLOCK.search(line)
        if m and "runner_gate" not in line and "Triggers=" in line:
            self.classify_gates(m.group(2))
        m = RE_SHADOW_OPEN.search(line)
        if m:
            self.shadow_opens.append((m.group(1), m.group(2), float(m.group(3)), int(m.group(4))))
        m = RE_SHADOW_CLOSE.search(line)
        if m:
            try:
                self.shadow_closes.append((m.group(1), m.group(2), float(m.group(3))))
            except ValueError:
                pass
        if RE_LLM_OPEN.search(line):
            self.llm_open += 1
        if RE_LLM_TRIP.search(line):
            self.llm_trip += 1
        if RE_LLM_OK.search(line):
            self.llm_ok += 1


def report(st: Stats, since: str, tail_label: str = ""):
    line = "=" * 72
    print("\n" + line)
    print(f"SHADOW 验证报告  {tail_label}  (since {since})")
    print(line)

    print("\n■ 信号漏斗")
    v = st.verdict
    total_dir = v["LONG"] + v["SHORT"]
    print(f"  verdict 路由   : PASS={v['PASS']}  LONG={v['LONG']}  SHORT={v['SHORT']}  CLOSE={v['CLOSE']}")
    print(f"  方向信号合计    : {total_dir}  (LONG+SHORT，进入下单评估)")
    if st.ta:
        print(f"  TA 过滤        : CONFIRMED={st.ta['CONFIRMED']}  WEAK={st.ta['WEAK']}  REJECTED={st.ta['REJECTED']}")

    print("\n■ LLM 研究")
    print(f"  成功(200/OK)={st.llm_ok}  熔断短路={st.llm_open}  跳闸(trip)={st.llm_trip}")

    print("\n■ runner_entry_gate 拦截（22 闸之前的总阀门）")
    if st.runner_block:
        for k, n in st.runner_block.most_common():
            print(f"    {k:<24}: {n}")
        if st.runner_conf:
            c = Counter(st.runner_conf)
            dist = "  ".join(f"{k:.2f}×{v}" for k, v in sorted(c.items()))
            print(f"    被 confidence 地板拦的 conf 分布: {dist}")
            print(f"    -> 若当前阈值 0.62，则其中 conf>=0.62 的有 "
                  f"{sum(1 for x in st.runner_conf if x >= 0.62)} 个本应放行")
    else:
        print("    （无 runner 拦截）")

    print("\n■ 22 闸门拦截分布（一次评估可命中多闸）")
    print(f"  进入 22 闸评估次数: {st.gate_events}")
    if st.gate_block:
        for k, n in st.gate_block.most_common():
            print(f"    {k:<20}: {n}")
    else:
        print("    （无 22 闸拦截记录）")

    print("\n■ shadow_book 纸面成交（全链路走通的最终证据）")
    print(f"  OPEN (paper) 笔数 : {len(st.shadow_opens)}")
    if st.shadow_opens:
        side_c = Counter(s for s, _, _, _ in st.shadow_opens)
        coins = Counter(c for _, c, _, _ in st.shadow_opens)
        notional = sum(n for _, _, n, _ in st.shadow_opens)
        print(f"    方向: long={side_c['long']} short={side_c['short']}  "
              f"累计名义本金=${notional:.2f}")
        print(f"    涉及币种: {dict(coins.most_common(10))}")
    print(f"  CLOSE(paper) 笔数 : {len(st.shadow_closes)}")
    if st.shadow_closes:
        pnls = [p for _, _, p in st.shadow_closes]
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        print(f"    胜率: {wins}/{len(pnls)} = {wins/len(pnls)*100:.0f}%  "
              f"累计纸面盈亏=${total:.2f}")

    print("\n■ 结论判定")
    if len(st.shadow_opens) > 0:
        print(f"  ✅ 参数调整生效：已有 {len(st.shadow_opens)} 笔信号走通全链路进入纸面账本。")
    elif total_dir > 0:
        print("  ⚠️ 有方向信号但 0 笔纸面成交 -> 仍被闸门拦截，请看上方拦截分布定位。")
    else:
        print("  ℹ️ 时间窗内暂无方向信号（LONG/SHORT=0），可能市场无机会或观察窗太短。")
    print(line + "\n")


def ts_of(line):
    m = RE_TS.match(line)
    return m.group(1) if m else ""


def run_snapshot(log_path, since):
    st = Stats()
    matched = 0
    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            ts = ts_of(line)
            if since and ts and ts < since:
                continue
            if since and not ts:
                continue
            matched += 1
            st.feed(line)
    print(f"(扫描 {log_path}，命中 {matched} 行)")
    report(st, since or "日志起点")


def run_watch(log_path):
    st = Stats()
    print(f"[watch] tail -F {log_path} ... Ctrl+C 输出汇总")
    try:
        with open(log_path, "r", errors="ignore") as f:
            f.seek(0, 2)  # 跳到末尾，只看新增
            while True:
                line = f.readline()
                if not line:
                    time.sleep(1.0)
                    continue
                st.feed(line)
                if RE_SHADOW_OPEN.search(line):
                    print("  >> 纸面成交: " + line.strip()[-160:])
    except KeyboardInterrupt:
        report(st, "watch 起点", tail_label="[实时]")


def main():
    ap = argparse.ArgumentParser(description="SHADOW 模式回测/验证")
    ap.add_argument("--log", default=DEFAULT_LOG, help="日志路径")
    ap.add_argument("--since", default="", help='起始时间，如 "2026-09-03 01:00:00"')
    ap.add_argument("--watch", action="store_true", help="实时 tail 模式")
    args = ap.parse_args()
    if args.watch:
        run_watch(args.log)
    else:
        run_snapshot(args.log, args.since)


if __name__ == "__main__":
    main()
