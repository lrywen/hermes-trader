#!/usr/bin/env python3
"""Push Hermes P0/P1/P2 optimization report to a Feishu (Lark) custom bot.

The bot is configured with a signature secret; this script computes the
HMAC-SHA256 timestamp/signature pair and posts an interactive card.

Card contents:
  * A/B backtest comparison across max_loss 1.0% / 2.5% / 4.0%
  * 48h live observation blocked-signal breakdown
  * RSI>75 rule verdict + proposed dynamic-threshold plan
  * Link/reference to the generated PNG charts (paths in card footer)

Usage:
  python3 scripts/push_optimization_feishu.py
  # or override:
  python3 scripts/push_optimization_feishu.py \\
      --webhook-url https://open.feishu.cn/open-apis/bot/v2/hook/XXX \\
      --secret     jLRhRh5oWiypzOYKpgc1nb \\
      --dry-run
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/55e07104-3211-43a9-9eeb-40bda015f749"
DEFAULT_SECRET = "jLRhRh5oWiypzOYKpgc1nb"

P0_P1_P2_RULES: Dict[str, List[str]] = {
    "P0 (hard safety vetoes)": [
        "RSI(14) on 4h > 75 → reject long; < 25 → reject short",
        "|close − ema21| / atr > 2.5 → reject chase (overextended)",
    ],
    "P1 (confirmation & momentum quality)": [
        "breakout needs 2 consecutive closes outside prior high (confirm_bars=2)",
        "momentumBurst can only bypass WEAK, no longer bypasses REJECTED",
        "OBV slope confirmation adds up to +8 score points",
        "volume confirm threshold raised 0.8× → 1.2× average",
    ],
    "P2 (regime & conviction)": [
        "ADX > 45 halves trend_strength score (avoid euphoric late entries)",
        "squeeze + breakout coupling gives +2 breakout bonus",
        "chop regime (ADX < 20 + EMA-neutral + score < 55) blocks low-conviction entries",
    ],
}

# Three backtests at varying max_loss; metrics copied from logs in /tmp.
BACKTEST_RUNS: List[Dict[str, Any]] = [
    {
        "label": "max_loss 1.0%",
        "dsl": "1.0% / 1.25% / 0.2",
        "old": {"trades": 678, "win": 42.3, "payoff": 1.80, "exp": 0.716, "pnl": 485.24, "roe": 242.6},
        "new": {"trades": 348, "win": 36.5, "payoff": 1.59, "exp": -0.201, "pnl": -69.87, "roe": -34.9},
    },
    {
        "label": "max_loss 2.5% (prod default)",
        "dsl": "2.5% / 1.25% / 0.2",
        "old": {"trades": 421, "win": 67.0, "payoff": 0.82, "exp": 1.856, "pnl": 781.40, "roe": 390.7},
        "new": {"trades": 212, "win": 59.4, "payoff": 0.64, "exp": -0.248, "pnl": -52.58, "roe": -26.3},
    },
    {
        "label": "max_loss 4.0% (loose)",
        "dsl": "4.0% / 1.25% / 0.2",
        "old": {"trades": 347, "win": 76.1, "payoff": 0.59, "exp": 2.377, "pnl": 824.84, "roe": 412.4},
        "new": {"trades": 172, "win": 70.9, "payoff": 0.47, "exp": 0.541, "pnl": 93.06, "roe": 46.5},
    },
]

BLOCKS_48H: Dict[str, Any] = {
    "window": "2026-08-19 21:31 → 2026-08-21 21:31 (Asia/Shanghai)",
    "total_ta_skip": 1095,
    "signals": [
        ("REJECTED (TA hard veto)", 584, 53.3),
        ("RESEARCH_THROTTLE (low-cap/new coin)", 482, 44.0),
        ("HELD_THROTTLE (already in position)", 29, 2.6),
    ],
    "rejected_reasons": [
        ("late_long_rsi_over75", 531, 90.9),
        ("late_long_ext_only", 53, 9.1),
    ],
    "top_coins": [("GALA", 56), ("kNEIRO", 54), ("BOME", 53), ("ZORA", 47), ("HEMI", 46)],
}

RSI_VERDICT: List[Dict[str, str]] = [
    {"k": "结论", "v": "**过于激进，需要放宽 + 例外逻辑**"},
    {"k": "证据 1", "v": "48h 实盘 REJECTED 中 90.9%（531/584）由 RSI>75 拒多触发，其他 P0/P2 规则几乎闲置"},
    {"k": "证据 2", "v": "三组回测 OLD 策略 RSI>75 入场占比 49.9%–54.2%，反而贡献正 PnL（$485 / $781 / $825），说明强趋势中 RSI 可长期钝化"},
    {"k": "证据 3", "v": "把止损从 1.0% 放宽到 4.0% 后，NEW PnL 从 -$70 改善到 +$93，但仍落后 OLD $732；放宽止损只能止血，不能解释全部差距"},
    {"k": "根因", "v": "硬阈值 75 不区分趋势强度/4h 与 1d 双周期 RSI 对齐/量价确认；在强趋势中把高质量突破全部拦截"},
]

RSI_PLAN: List[str] = [
    "① **动态阈值（按 ADX/趋势强度）**：ADX≥30 时阈值上调到 80；ADX≥45 上调到 85；ADX<20 维持 70/30；ADX 20–30 维持 75/25",
    "② **4h/1d 双周期 RSI 例外**：4h RSI>75 但 1d RSI<70 且 1d EMA21 上行 → 降级为 score −5 而非硬否决（保留趋势中段）",
    "③ **量价确认例外**：RSI 在 75–80 区间且 volume ≥1.5× 均值、OBV 斜率同步上行 → 允许入场，仓位减半（fraction 0.2 → 0.1）",
    "④ **二次确认**：RSI 首次触 75 不立刻拦截，等下一根 1h K 线收盘若回落到 72 以下则放行，保持 75 以上才拒（避免追瞬时尖峰）",
    "⑤ **熔断兜底**：单笔同时满足 RSI>75 + ext_atr>2.5 + ADX>45（极端拥挤）仍硬否决，不接受任何例外",
    "⑥ **灰度上线**：先在 paper/observe 模式跑 7 天，对比「严格 75」vs「动态阈值」的拦截率、win rate、payoff，确认改善后再上实盘",
]

CHART_FILES = [
    "/tmp/hermes_ab_metrics.png",
    "/tmp/hermes_ab_vetoes.png",
]


def gen_sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def _fmt_money(v: float) -> str:
    return f"${v:+.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v:+.1f}%"


def _color_for_pnl(v: float) -> str:
    return "green" if v >= 0 else "red"


def build_card() -> Dict[str, Any]:
    elements: List[Dict[str, Any]] = []

    # ---- Header divider / intro -----------------------------------------
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                "**Hermes P0/P1/P2 优化回测 & 实盘观察周报**\n"
                f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)　|　"
                "样本：14 天 1h K 线，top-15 币种"
            ),
        },
    })
    elements.append({"tag": "hr"})

    # ---- Backtest comparison table --------------------------------------
    bt_lines = [
        "**A/B 回测对比（OLD vs NEW，三组 max_loss 对照）**",
        "",
        "| max_loss | 版本 | 交易数 | 胜率 | 盈亏比 | 期望/笔 | PnL | ROE |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in BACKTEST_RUNS:
        for side_key, side_label in (("old", "OLD"), ("new", "NEW")):
            d = run[side_key]
            bt_lines.append(
                f"| {run['label']} | {side_label} | {d['trades']} | "
                f"{d['win']:.1f}% | {d['payoff']:.2f} | "
                f"{d['exp']:+.3f} | {_fmt_money(d['pnl'])} | {_fmt_pct(d['roe'])} |"
            )
        bt_lines.append(f"| _DSL_ | _{run['dsl']}_ |  |  |  |  |  |  |")
    bt_lines.append("")
    bt_lines.append(
        "**结论**：放宽止损 1.0%→4.0% 让 NEW 的 PnL 从 -$70 回升到 +$93，"
        "但仍落后 OLD 约 $732；**止损不是主因，入场闸门（尤其 RSI>75）过严才是主因。**"
    )
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(bt_lines)}})
    elements.append({"tag": "hr"})

    # ---- 48h blocked signals -------------------------------------------
    b = BLOCKS_48H
    sig_lines = [
        f"**48 小时实盘拦截统计**　窗口：{b['window']}",
        "",
        f"被拦截 ta_skip 总数：**{b['total_ta_skip']}**",
        "",
        "**按 signal 类型**",
    ]
    for name, count, pct in b["signals"]:
        bar = "█" * int(pct / 5)
        sig_lines.append(f"`{name:<32}` {count:>4} ({pct:.1f}%) {bar}")
    sig_lines.append("")
    sig_lines.append("**REJECTED 按拦截原因**")
    for name, count, pct in b["rejected_reasons"]:
        bar = "█" * int(pct / 5) or "·"
        sig_lines.append(f"`{name:<32}` {count:>4} ({pct:.1f}%) {bar}")
    top = "、".join(f"{c} {n}" for c, n in b["top_coins"])
    sig_lines.append("")
    sig_lines.append(f"被拦最多的币：{top}")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(sig_lines)}})
    elements.append({"tag": "hr"})

    # ---- RSI analysis & plan -------------------------------------------
    rsi_lines = ["**RSI>75 规则评估**"]
    for item in RSI_VERDICT:
        rsi_lines.append(f"- **{item['k']}**：{item['v']}")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rsi_lines)}})
    elements.append({"tag": "hr"})

    plan_lines = ["**动态 RSI 阈值 & 例外逻辑方案**"]
    plan_lines.extend(RSI_PLAN)
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(plan_lines)}})
    elements.append({"tag": "hr"})

    # ---- P0/P1/P2 rules ------------------------------------------------
    rule_lines = ["**当前 P0/P1/P2 配置（NEW 闸门规则）**"]
    for title, bullets in P0_P1_P2_RULES.items():
        rule_lines.append(f"\n_{title}_")
        for x in bullets:
            rule_lines.append(f"• {x}")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rule_lines)}})
    elements.append({"tag": "hr"})

    # ---- Charts note ---------------------------------------------------
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": "关键指标对比图 & veto 分布图：" + " ， ".join(CHART_FILES),
        }],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "Hermes 优化报告：A/B 回测 · 48h 拦截 · RSI 规则评估",
                },
                "template": "blue",
            },
            "elements": elements,
        },
    }


def post_to_feishu(webhook_url: str, secret: str, payload: Dict[str, Any],
                   timeout: int = 10) -> int:
    ts = int(time.time())
    sign = gen_sign(secret, ts)
    body = dict(payload)
    body["timestamp"] = str(ts)
    body["sign"] = sign
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    print(f"[feishu] HTTP {resp.status}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[feishu] non-JSON response: {raw[:300]}")
        return resp.status
    print(f"[feishu] code={parsed.get('code')} msg={parsed.get('msg')}")
    if parsed.get("code") not in (0, None):
        print(f"[feishu] full response: {raw}")
    return resp.status if parsed.get("code") in (0, None) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--webhook-url", default=os.environ.get("FEISHU_WEBHOOK_URL", DEFAULT_WEBHOOK))
    ap.add_argument("--secret", default=os.environ.get("FEISHU_SECRET", DEFAULT_SECRET))
    ap.add_argument("--dry-run", action="store_true",
                    help="Build & print payload but do not POST")
    ap.add_argument("--dump-payload", default=None,
                    help="Write payload JSON to this path")
    args = ap.parse_args()

    payload = build_card()

    if args.dump_payload:
        Path(args.dump_payload).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[payload] wrote {args.dump_payload}")

    if args.dry_run:
        print(f"[dry-run] would POST to {args.webhook_url}")
        print(json.dumps({"elements_count": len(payload["card"]["elements"])}, ensure_ascii=False))
        return 0

    return 0 if post_to_feishu(args.webhook_url, args.secret, payload) == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
