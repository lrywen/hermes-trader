#!/usr/bin/env python3
"""Push Hermes P0/P1/P2 optimization config, latest A/B backtest report and
key-metric comparison charts to a Slack channel.

Designed to be run after `backtest_ab_compare.py` and the 48h blocked-signal
analyzer. It:

  1. Reads the latest backtest log (default /tmp/backtest_1p0.log, override
     with --backtest-log).
  2. Reads the blocked-signal summary JSON produced by
     /tmp/analyze_blocks_48h.py (override with --blocks-json).
  3. Reads live DSL config from the `hermes-trader` container (optional,
     disable with --no-live-config).
  4. Renders two PNG charts (metric comparison + veto breakdown) via matplotlib.
  5. Posts a Slack message with Block Kit layout and uploads the PNGs.

Auth (one of, resolved in this order):
  - --webhook-url   Slack incoming webhook URL
  - SLACK_WEBHOOK_URL env var
  - --slack-token + --channel   (uses chat.postMessage + files.upload)
  - SLACK_BOT_TOKEN + SLACK_CHANNEL env vars

Usage:
  python3 scripts/push_optimization_report.py \
      --webhook-url https://hooks.slack.com/services/XXX \
      --backtest-log /tmp/backtest_1p0.log \
      --blocks-json /tmp/blocks_48h_summary.json

  # or with a bot token:
  SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL=#trading python3 scripts/push_optimization_report.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# The metrics table uses fixed-width columns. Header defines column starts:
#   "  Metric                                  OLD            NEW        Delta"
# We locate the column starts from the header and slice by them.
METRIC_HEADER = re.compile(r"^(\s*Metric)(\s+OLD)(\s+NEW)(\s+Delta)\s*$")
SEPARATOR_LINE = re.compile(r"^\s*-{10,}\s+-{10,}\s+-{10,}\s+-{10,}")
SECTION_LINE = re.compile(r"^\s*-{3,}\s+(?P<title>.+?)\s+-{3,}\s*$")


def _to_num(s: str):
    s = s.strip().replace("$", "").replace(",", "").replace("%", "")
    if s.startswith("+"):
        s = s[1:]
    if s in {"-", "—", "n/a", ""}:
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def parse_backtest_log(path: Path) -> Dict[str, Any]:
    """Parse the A/B comparison table and supporting sections."""
    if not path.is_file():
        return {"path": str(path), "available": False}

    text = path.read_text(errors="replace")
    lines = text.splitlines()

    meta: Dict[str, Any] = {"path": str(path), "available": True}
    m = re.search(r"Period:\s*(\d+)\s*days[^|]*\|\s*Universe:\s*([^|]+)\|\s*Equity:\s*\$?([\d.]+)",
                  text)
    if m:
        meta["period_days"] = int(m.group(1))
        meta["universe"] = m.group(2).strip()
        meta["equity"] = float(m.group(3))
    m = re.search(r"DSL:\s*([\d.]+)%?/([\d.]+)%?/([\d.]+)", text)
    if m:
        meta["dsl"] = {"max_loss_pct": float(m.group(1)),
                       "protect_pct": float(m.group(2)),
                       "retrace_threshold": float(m.group(3))}

    metrics: Dict[str, Dict[str, Any]] = {}
    vetoes: Dict[str, int] = {}
    top_veto_coins: List[Tuple[str, Dict[str, int]]] = []
    exit_reasons: Dict[str, Dict[str, int]] = {}
    # Column boundaries derived from the dashed separator line:
    # (name_start, old_start, old_end, new_start, new_end, delta_start)
    col_bounds: Optional[Tuple[int, int, int, int, int, int]] = None
    in_metrics = False
    in_vetoes = False
    in_exit = False
    in_top_coins = False

    for raw in lines:
        line = raw.rstrip()

        # The metrics table is identified directly by its column header
        # (it is not wrapped in a "--- Metrics ---" section in the log).
        mh = METRIC_HEADER.match(line)
        if mh:
            in_metrics = True
            continue

        # Check the separator BEFORE SECTION_LINE: a pure dash separator
        # also satisfies the section-header pattern and would otherwise be
        # consumed there, killing the metrics table immediately.
        if in_metrics and SEPARATOR_LINE.match(line):
            # Determine column boundaries from the dashed runs. The runs
            # cover name / old / new / delta; value columns are right-aligned
            # to the run end.
            runs = [(m.start(), m.end()) for m in re.finditer(r"-{10,}", line)]
            if len(runs) >= 4:
                name_start = len(line) - len(line.lstrip())
                old_s, old_e = runs[1]
                new_s, new_e = runs[2]
                delta_s = runs[3][0]
                col_bounds = (name_start, old_s, old_e, new_s, new_e, delta_s)
            continue

        if in_metrics and col_bounds is not None:
            if not line.strip():
                # blank line ends the metrics table
                in_metrics = False
                continue
            name_start, old_s, old_e, new_s, new_e, delta_s = col_bounds
            name = line[name_start:old_s].strip()
            old_v = line[old_s:old_e].strip()
            new_v = line[new_s:new_e].strip()
            delta_v = line[delta_s:].strip()
            if name and (old_v or new_v):
                metrics[name] = {
                    "old": _to_num(old_v),
                    "new": _to_num(new_v),
                    "delta": delta_v,
                }
                continue
            # Non-metric-looking content while in table -> end it
            in_metrics = False

        if SECTION_LINE.match(line):
            title = SECTION_LINE.match(line).group("title").lower()
            in_vetoes = "veto" in title
            in_exit = "exit reason" in title
            in_top_coins = "top vetoed coins" in title or "vetoed coin" in title
            # Any --- section header terminates the metrics table.
            in_metrics = False
            col_bounds = None
            continue

        if in_exit:
            m2 = re.match(r"\s*(?P<reason>[\w\s.%\-]+?)\s+OLD:\s*(?P<old>\d+)\s+NEW:\s*(?P<new>\d+)",
                          line)
            if m2:
                exit_reasons[m2.group("reason").strip()] = {
                    "old": int(m2.group("old")),
                    "new": int(m2.group("new")),
                }
            elif line.strip() and not line.strip().startswith("-"):
                in_exit = False

        if in_vetoes:
            mv = re.match(r"\s*(?P<tag>[\w\s]+?)\s*:\s*(?P<n>\d+)\s*\((?P<pct>[\d.]+)%\)", line)
            if mv:
                vetoes[mv.group("tag").strip()] = int(mv.group("n"))
            elif "Total vetoes" in line:
                mt = re.search(r"Total vetoes:\s*(\d+)", line)
                if mt:
                    meta["total_vetoes"] = int(mt.group(1))
            elif line.strip() and not line.strip().startswith("-") and not vetoes:
                continue
            elif line.strip() and ":" not in line and vetoes:
                in_vetoes = False

        if in_top_coins:
            mc = re.match(r"\s*(?P<coin>\S+)\s+(?P<n>\d+)x\s+(?P<dist>\{.*\})", line)
            if mc:
                try:
                    dist = json.loads(mc.group("dist").replace("'", '"'))
                except json.JSONDecodeError:
                    dist = {}
                top_veto_coins.append((mc.group("coin"), dist))

    meta["metrics"] = metrics
    meta["vetoes"] = vetoes
    meta["top_veto_coins"] = top_veto_coins
    meta["exit_reasons"] = exit_reasons
    return meta


def load_blocks_summary(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"available": False, "path": str(path)}
    data = json.loads(path.read_text())
    data["available"] = True
    data["path"] = str(path)
    return data


def fetch_live_dsl_config() -> Dict[str, Any]:
    """Read dsl_exit block from the running hermes-trader container."""
    try:
        out = subprocess.check_output(
            ["docker", "exec", "hermes-trader", "python3", "-c",
             "import json; c=json.load(open('/data/.agent-config.json')); "
             "print(json.dumps({'dsl_exit': c.get('dsl_exit', {}), "
             "'equity_fraction_per_trade': c.get('equity_fraction_per_trade'), "
             "'leverage': c.get('leverage'), "
             "'min_confidence': c.get('min_confidence')}))"],
            stderr=subprocess.DEVNULL, timeout=15, text=True,
        )
        return json.loads(out.strip())
    except Exception as e:  # noqa: BLE001
        return {"error": f"unable to read live config: {e}"}


# ---------------------------------------------------------------------------
# P0/P1/P2 rule summary (canonical, mirrors backtest_ab_compare.py docstring)
# ---------------------------------------------------------------------------

P0_P1_P2_RULES = {
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


# ---------------------------------------------------------------------------
# Charts — pure-stdlib PNG renderer (no matplotlib dependency)
# ---------------------------------------------------------------------------
#
# We write an uncompressed RGB PNG via zlib. Font support is intentionally
# minimal: a 5x7 bitmap font covers ASCII so the labels/titles/legends render
# without external font files. This keeps the script usable in minimal
# containers where matplotlib cannot be installed.

import zlib

_FONT = {
    " ": ["00000"]*7,
    "A": ["01110","10001","10001","11111","10001","10001","10001"],
    "B": ["11110","10001","10001","11110","10001","10001","11110"],
    "C": ["01110","10001","10000","10000","10000","10001","01110"],
    "D": ["11110","10001","10001","10001","10001","10001","11110"],
    "E": ["11111","10000","10000","11110","10000","10000","11111"],
    "F": ["11111","10000","10000","11110","10000","10000","10000"],
    "G": ["01110","10001","10000","10111","10001","10001","01111"],
    "H": ["10001","10001","10001","11111","10001","10001","10001"],
    "I": ["11111","00100","00100","00100","00100","00100","11111"],
    "J": ["00111","00010","00010","00010","00010","10010","01100"],
    "K": ["10001","10010","10100","11000","10100","10010","10001"],
    "L": ["10000","10000","10000","10000","10000","10000","11111"],
    "M": ["10001","11011","10101","10101","10001","10001","10001"],
    "N": ["10001","11001","10101","10011","10001","10001","10001"],
    "O": ["01110","10001","10001","10001","10001","10001","01110"],
    "P": ["11110","10001","10001","11110","10000","10000","10000"],
    "Q": ["01110","10001","10001","10001","10101","10010","01101"],
    "R": ["11110","10001","10001","11110","10100","10010","10001"],
    "S": ["01111","10000","10000","01110","00001","00001","11110"],
    "T": ["11111","00100","00100","00100","00100","00100","00100"],
    "U": ["10001","10001","10001","10001","10001","10001","01110"],
    "V": ["10001","10001","10001","10001","10001","01010","00100"],
    "W": ["10001","10001","10001","10101","10101","11011","10001"],
    "X": ["10001","10001","01010","00100","01010","10001","10001"],
    "Y": ["10001","10001","01010","00100","00100","00100","00100"],
    "Z": ["11111","00001","00010","00100","01000","10000","11111"],
    "0": ["01110","10001","10011","10101","11001","10001","01110"],
    "1": ["00100","01100","00100","00100","00100","00100","01110"],
    "2": ["01110","10001","00001","00010","00100","01000","11111"],
    "3": ["11110","00001","00001","01110","00001","00001","11110"],
    "4": ["00010","00110","01010","10010","11111","00010","00010"],
    "5": ["11111","10000","11110","00001","00001","10001","01110"],
    "6": ["00110","01000","10000","11110","10001","10001","01110"],
    "7": ["11111","00001","00010","00100","01000","01000","01000"],
    "8": ["01110","10001","10001","01110","10001","10001","01110"],
    "9": ["01110","10001","10001","01111","00001","00010","01100"],
    ".": ["00000","00000","00000","00000","00000","00100","00100"],
    ",": ["00000","00000","00000","00000","00100","00100","01000"],
    ":": ["00000","00100","00000","00000","00000","00100","00000"],
    "/": ["00001","00010","00010","00100","01000","01000","10000"],
    "\\":["10000","01000","01000","00100","00010","00010","00001"],
    "-": ["00000","00000","00000","11111","00000","00000","00000"],
    "_": ["00000","00000","00000","00000","00000","00000","11111"],
    "+": ["00000","00100","00100","11111","00100","00100","00000"],
    "=": ["00000","00000","11111","00000","11111","00000","00000"],
    "$": ["00100","01111","10100","01110","00101","11110","00100"],
    "%": ["11001","11010","00100","00100","01011","10011","00000"],
    "(": ["00010","00100","01000","01000","01000","00100","00010"],
    ")": ["01000","00100","00010","00010","00010","00100","01000"],
    "[": ["01110","01000","01000","01000","01000","01000","01110"],
    "]": ["01110","00010","00010","00010","00010","00010","01110"],
    "<": ["00010","00100","01000","10000","01000","00100","00010"],
    ">": ["01000","00100","00010","00001","00010","00100","01000"],
    "?": ["01110","10001","00001","00010","00100","00000","00100"],
    "!": ["00100","00100","00100","00100","00100","00000","00100"],
    "&": ["01100","10010","10100","01000","10101","10010","01101"],
    "#": ["01010","01010","11111","01010","11111","01010","01010"],
    "*": ["00000","10101","01110","11111","01110","10101","00000"],
    "'": ["00100","00100","00100","00000","00000","00000","00000"],
    '"': ["01010","01010","01010","00000","00000","00000","00000"],
    "~": ["00000","00000","01001","10110","00000","00000","00000"],
    "|": ["00100","00100","00100","00100","00100","00100","00100"],
    "@": ["01110","10001","10111","10101","10111","10000","01110"],
}


class _Canvas:
    """Minimal RGB canvas with a text drawer and PNG serializer."""

    def __init__(self, w: int, h: int, bg=(255, 255, 255)):
        self.w, self.h = w, h
        self.px = bytearray(bg * w * h)

    def _set(self, x: int, y: int, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i+3] = bytes(c)

    def fill_rect(self, x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                self._set(x, y, c)

    def frame(self, x0, y0, x1, y1, c, t=1):
        self.fill_rect(x0, y0, x1, y0+t, c)
        self.fill_rect(x0, y1-t, x1, y1, c)
        self.fill_rect(x0, y0, x0+t, y1, c)
        self.fill_rect(x1-t, y0, x1, y1, c)

    def hline(self, x0, x1, y, c):
        self.fill_rect(x0, y, x1, y+1, c)

    def vline(self, x, y0, y1, c):
        self.fill_rect(x, y0, x+1, y1, c)

    def text(self, s, x, y, c=(0, 0, 0), scale: int = 1, anchor: str = "lt"):
        s = s.upper()
        width_px = sum((6) for _ in s) * scale
        height_px = 7 * scale
        if anchor.startswith("b"):
            y -= height_px
        if "m" in anchor[1:2] or anchor == "mt":
            x -= width_px // 2
        elif anchor.endswith("r"):
            x -= width_px
        for ch in s:
            glyph = _FONT.get(ch, _FONT["?"] if "?" in _FONT else _FONT[" "])
            for row_idx, row in enumerate(glyph):
                for col_idx, bit in enumerate(row):
                    if bit == "1":
                        self.fill_rect(
                            x + col_idx * scale,
                            y + row_idx * scale,
                            x + (col_idx + 1) * scale,
                            y + (row_idx + 1) * scale,
                            c,
                        )
            x += 6 * scale

    def write_png(self, path: Path):
        # Add filter byte 0 per scanline then zlib-compress.
        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)
            raw.extend(self.px[y * stride:(y + 1) * stride])

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                len(data).to_bytes(4, "big")
                + tag
                + data
                + zlib.crc32(tag + data).to_bytes(4, "big")
            )

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR",
                     self.w.to_bytes(4, "big") + self.h.to_bytes(4, "big")
                     + bytes([8, 2, 0, 0, 0]))
        png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        png += chunk(b"IEND", b"")
        path.write_bytes(png)


# Color palette (RGB)
_OLD = (120, 120, 130)
_NEW_GOOD = (45, 164, 78)
_NEW_BAD = (207, 34, 46)
_GRID = (220, 220, 225)
_INK = (28, 33, 40)
_INK_SOFT = (100, 105, 115)
_ACCENT = (9, 105, 218)
_PIE = [(207, 34, 46), (191, 135, 0), (154, 103, 0), (110, 119, 129),
        (9, 105, 218), (191, 57, 137)]


def _fmt_num(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, str):
        return v
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    if abs(v) >= 1:
        return f"{v:.2f}"
    return f"{v:.3f}"


def _unpack(cell: Optional[Dict[str, Any]]):
    if not cell:
        return (None, None)
    o, n = cell.get("old"), cell.get("new")

    def _conv(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).replace("$", "").replace(",", "").replace("%", "").replace("+", "")
        try:
            return float(s)
        except ValueError:
            return None

    return (_conv(o), _conv(n))


def render_metrics_chart(bt: Dict[str, Any], out_path: Path) -> Optional[Path]:
    metrics = bt.get("metrics", {})
    if not metrics:
        return None

    def find(*cands):
        for c in cands:
            for k, v in metrics.items():
                if c.lower() in k.lower():
                    return v
        return None

    spec = [
        ("Win rate", find("Win rate"), False, "%"),
        ("Payoff", find("Payoff"), False, "x"),
        ("Expectancy", find("Expectancy"), False, "$"),
        ("Total PnL", find("Total PnL"), False, "$"),
        ("ROE", find("Return on equity"), False, "%"),
    ]

    W, H = 1400, 460
    c = _Canvas(W, H)
    title = (f"A/B Backtest  OLD vs NEW (P0/P1/P2)   "
             f"{bt.get('period_days','?')}d, "
             f"max_loss={bt.get('dsl',{}).get('max_loss_pct','?')}%")
    c.text(title, W // 2, 18, _INK, scale=2, anchor="mt")

    # Legend
    lx = W - 300
    c.fill_rect(lx, 18, lx + 16, 30, _OLD); c.text("OLD", lx + 22, 20, _INK)
    c.fill_rect(lx + 100, 18, lx + 116, 30, _ACCENT); c.text("NEW", lx + 122, 20, _INK)

    # Plot area: 5 panels
    pad = 40
    top = 60
    panel_w = (W - pad * 2 - 4 * 16) // 5
    panel_h = H - top - 70

    for i, (label, cell, _lb, unit) in enumerate(spec):
        ov, nv = _unpack(cell)
        x0 = pad + i * (panel_w + 16)
        y0 = top
        x1 = x0 + panel_w
        y1 = y0 + panel_h
        c.frame(x0, y0, x1, y1, _GRID)
        c.text(label, (x0 + x1) // 2, y0 + 10, _INK, anchor="mt")
        if ov is None and nv is None:
            c.text("n/a", (x0 + x1) // 2, (y0 + y1) // 2, _INK_SOFT, anchor="mm")
            continue

        vals = [v for v in (ov, nv) if v is not None]
        lo = min(0.0, min(vals))
        hi = max(0.0, max(vals))
        if hi == lo:
            hi = lo + 1
        span = (hi - lo) * 1.2 or 1
        lo = (lo + hi) / 2 - span / 2
        hi = (lo + hi) / 2 + span / 2
        # zero line
        if lo < 0 < hi:
            zy = int(y1 - (0 - lo) / (hi - lo) * panel_h)
            c.hline(x0, x1, zy, _INK_SOFT)

        def y_for(v):
            return int(y1 - (v - lo) / (hi - lo) * panel_h)

        # y-axis min/max labels
        c.text(_fmt_num(hi), x0 + 4, y0 + 26, _INK_SOFT)
        c.text(_fmt_num(lo), x0 + 4, y1 - 12, _INK_SOFT)

        # bars
        bar_w = panel_w // 4
        bx1 = x0 + panel_w // 4 - bar_w // 2
        bx2 = x0 + 3 * panel_w // 4 - bar_w // 2
        if ov is not None:
            oy = y_for(ov)
            c.fill_rect(bx1, min(oy, y_for(0)), bx1 + bar_w, max(oy, y_for(0)), _OLD)
            c.text(_fmt_num(ov), bx1 + bar_w // 2,
                   oy - 10 if ov >= 0 else oy + 4, _INK, anchor="mt" if ov >= 0 else "mm")
        if nv is not None:
            color = _NEW_GOOD if (ov is not None and nv >= ov) or (ov is None and nv >= 0) else _NEW_BAD
            ny = y_for(nv)
            c.fill_rect(bx2, min(ny, y_for(0)), bx2 + bar_w, max(ny, y_for(0)), color)
            c.text(_fmt_num(nv), bx2 + bar_w // 2,
                   ny - 10 if nv >= 0 else ny + 4, _INK, anchor="mt" if nv >= 0 else "mm")
        c.text("OLD", bx1 + bar_w // 2, y1 + 6, _INK_SOFT, anchor="mt")
        c.text("NEW", bx2 + bar_w // 2, y1 + 6, _INK_SOFT, anchor="mt")

    c.text("5 bps fees, no slippage/funding; deterministic AI substitute.",
           W // 2, H - 22, _INK_SOFT, anchor="mt")
    c.write_png(out_path)
    return out_path


def render_veto_chart(bt: Dict[str, Any], blocks: Dict[str, Any],
                      out_path: Path) -> Optional[Path]:
    bt_vetoes = bt.get("vetoes", {})
    live_rejected = blocks.get("vetoes", {}) if blocks.get("available") else {}
    sig = blocks.get("signal_breakdown", {}) if blocks.get("available") else {}
    if not bt_vetoes and not live_rejected and not sig:
        return None

    W, H = 1400, 520
    c = _Canvas(W, H)
    c.text("Signal vetoes  backtest vs live 48h", W // 2, 18, _INK, scale=2, anchor="mt")

    # Left: horizontal grouped bar chart
    left_x, left_y = 40, 70
    left_w, left_h = 820, H - 110
    c.frame(left_x, left_y, left_x + left_w, left_y + left_h, _GRID)
    tags = sorted(set(list(bt_vetoes.keys()) + list(live_rejected.keys())),
                  key=lambda t: -(bt_vetoes.get(t, 0) + live_rejected.get(t, 0)))
    max_v = max([bt_vetoes.get(t, 0) for t in tags]
                + [live_rejected.get(t, 0) for t in tags] + [1])
    row_h = left_h // max(1, len(tags))
    bar_h = max(6, row_h // 3)
    for i, tag in enumerate(tags):
        cy = left_y + i * row_h + row_h // 2
        bv = bt_vetoes.get(tag, 0)
        lv = live_rejected.get(tag, 0)
        label = tag.replace("_", " ")
        if len(label) > 22:
            label = label[:21] + "..."
        c.text(label, left_x + 6, cy - 4, _INK)
        bx0 = left_x + 180
        max_bar = left_x + left_w - 80 - bx0
        # backtest
        bw1 = int(max_bar * bv / max_v)
        c.fill_rect(bx0, cy - bar_h - 2, bx0 + bw1, cy - 2, _ACCENT)
        c.text(str(bv), bx0 + bw1 + 4, cy - bar_h + 1, _INK)
        # live
        bw2 = int(max_bar * lv / max_v)
        c.fill_rect(bx0, cy + 2, bx0 + bw2, cy + bar_h + 2, (191, 57, 137))
        c.text(str(lv), bx0 + bw2 + 4, cy + 4, _INK)
    # legend
    c.fill_rect(left_x + left_w - 200, left_y + 8,
                left_x + left_w - 188, left_y + 18, _ACCENT)
    c.text("BACKTEST", left_x + left_w - 184, left_y + 8, _INK)
    c.fill_rect(left_x + left_w - 110, left_y + 8,
                left_x + left_w - 98, left_y + 18, (191, 57, 137))
    c.text("LIVE 48H", left_x + left_w - 94, left_y + 8, _INK)

    # Right: pie of live ta_skip signal types
    if sig:
        cx, cy, r = 1170, 250, 130
        labels = list(sig.keys())
        sizes = [sig[k] for k in labels]
        total = sum(sizes) or 1
        angle = -90.0
        for i, (lab, sz) in enumerate(zip(labels, sizes)):
            frac = sz / total
            end = angle + frac * 360
            color = _PIE[i % len(_PIE)]
            # Fill pie wedge by scanline: simple approach - fill bounding square
            # with pixels inside the circle and within the angle range.
            for y in range(cy - r, cy + r):
                for x in range(cx - r, cx + r):
                    dx, dy = x - cx, y - cy
                    if dx * dx + dy * dy <= r * r:
                        import math
                        a = math.degrees(math.atan2(dy, dx))
                        a_norm = a if a >= -90 else a + 360
                        # compare against [angle, end) shifted to start at -90
                        start = angle if angle >= -180 else angle
                        e = end
                        if start <= a < e or (e > 180 and (a < e - 360 or a >= start)):
                            c._set(x, y, color)
            angle = end
        c.text(f"Live 48h ta_skip (n={total})", cx, cy - r - 24, _INK, anchor="mt")
        # legend below the pie
        ly = cy + r + 18
        for i, (lab, sz) in enumerate(zip(labels, sizes)):
            col = _PIE[i % len(_PIE)]
            yy = ly + i * 20
            c.fill_rect(1050, yy + 2, 1066, yy + 14, col)
            c.text(f"{lab}  {sz} ({sz/total*100:.1f}%)", 1072, yy, _INK)

    c.write_png(out_path)
    return out_path


# ---------------------------------------------------------------------------
# Slack delivery
# ---------------------------------------------------------------------------

def _http_post(url: str, data: Dict[str, Any],
               headers: Optional[Dict[str, str]] = None,
               files: Optional[Dict[str, Tuple[str, bytes, str]]] = None
               ) -> Dict[str, Any]:
    """Tiny multipart/form-data or JSON POST."""
    if files:
        boundary = "----hermsslack" + str(int(time.time() * 1000))
        body = bytearray()
        for k, v in data.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
            body.extend(str(v).encode())
            body.extend(b"\r\n")
        for field, (fname, content, ctype) in files.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'
                .encode())
            body.extend(f"Content-Type: {ctype}\r\n\r\n".encode())
            body.extend(content)
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            url, data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     **(headers or {})})
    else:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": True, "raw": raw}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.read().decode(errors="replace")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def build_blocks(bt: Dict[str, Any], blocks_data: Dict[str, Any],
                 live_cfg: Dict[str, Any], charts: List[Path]) -> List[Dict[str, Any]]:
    bl: List[Dict[str, Any]] = []
    dsl_label = "?"
    if bt.get("dsl"):
        d = bt["dsl"]
        dsl_label = f"max_loss {d['max_loss_pct']}% / protect {d['protect_pct']}% / retrace {d['retrace_threshold']}"

    bl.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f":chart_with_upwards_trend: Hermes A/B backtest — "
                    f"{bt.get('period_days','?')}d, universe {bt.get('universe','?').strip()}",
        },
    })
    bl.append({"type": "context", "elements": [
        {"type": "mrkdwn",
         "text": f"Generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                 f"DSL: `{dsl_label}` | Equity `${bt.get('equity','?')}`"}
    ]})
    bl.append({"type": "divider"})

    # ---- Key metrics
    metrics = bt.get("metrics", {})

    def _m(*names):
        for n in names:
            for k, v in metrics.items():
                if n.lower() in k.lower():
                    return v
        return None

    win_rate = _m("Win rate")
    payoff = _m("Payoff")
    exp = _m("Expectancy")
    pnl = _m("Total PnL")
    roe = _m("Return on equity")

    def _fmt(cell, key="new"):
        if not cell:
            return "n/a"
        return str(cell.get(key, "n/a"))

    def _delta(cell):
        if not cell:
            return ""
        d = str(cell.get("delta", ""))
        if "lower=better" in d:
            return _colour_for_metric(cell, lower_better=True)
        return _colour_for_metric(cell, lower_better=False)

    def _colour_for_metric(cell, lower_better=False):
        d = str(cell.get("delta", ""))
        m = re.search(r"([+\-]?\d+(?:\.\d+)?)", d.replace(",", ""))
        if not m:
            return d
        try:
            val = float(m.group(1))
        except ValueError:
            return d
        if lower_better:
            val = -val
        if val > 0:
            return f":large_green_circle: {d}"
        if val < 0:
            return f":red_circle: {d}"
        return d

    bl.append({"type": "section", "text": {"type": "mrkdwn",
               "text": "*Key metrics — OLD vs NEW (P0/P1/P2)*"}})

    rows = [
        ("Win rate", win_rate, False),
        ("Payoff ratio", payoff, False),
        ("Expectancy/trade", exp, False),
        ("Total PnL", pnl, False),
        ("Return on equity", roe, False),
    ]
    table = ["*Metric*        *OLD*     *NEW*     *Delta*"]
    for name, cell, lb in rows:
        table.append(
            f"{name:<16} {_fmt(cell,'old'):<8} {_fmt(cell,'new'):<8} {_delta(cell)}"
        )
    bl.append({"type": "section", "text": {"type": "mrkdwn",
                                           "text": "```\n" + "\n".join(table) + "\n```"}})

    # Entry quality summary
    rsi_mean = _m("RSI at entry")
    ext_mean = _m("extension.*ATR") or _m("extension")
    late_entries = _m("Late entries")
    if any([rsi_mean, ext_mean, late_entries]):
        eq = ["*Entry quality (lower extreme = better)*", "```"]
        if rsi_mean:
            eq.append(f"RSI at entry (mean)  OLD {_fmt(rsi_mean,'old')}  NEW {_fmt(rsi_mean,'new')}  {_delta(rsi_mean)}")
        if late_entries:
            eq.append(f"Late entries         OLD {_fmt(late_entries,'old')}  NEW {_fmt(late_entries,'new')}  {_delta(late_entries)}")
        eq.append("```")
        bl.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(eq)}})

    bl.append({"type": "divider"})

    # ---- P0/P1/P2 rules
    rule_lines = ["*P0 / P1 / P2 optimization config*"]
    for section, items in P0_P1_P2_RULES.items():
        rule_lines.append(f"\n:small_blue_diamond: *{section}*")
        for it in items:
            rule_lines.append(f"• {it}")
    bl.append({"type": "section", "text": {"type": "mrkdwn",
                                           "text": "\n".join(rule_lines)}})
    bl.append({"type": "divider"})

    # ---- Vetoes
    vetoes = bt.get("vetoes", {})
    if vetoes:
        total = bt.get("total_vetoes", sum(vetoes.values()))
        lines = [f"*Backtest vetoes (total {total})*"]
        for tag, n in sorted(vetoes.items(), key=lambda x: -x[1]):
            pct = n / total * 100 if total else 0
            bar = "█" * int(pct / 4) or "·"
            lines.append(f"`{tag:<26}` {n:>4} ({pct:4.1f}%) {bar}")
        bl.append({"type": "section", "text": {"type": "mrkdwn",
                                               "text": "\n".join(lines)}})

    # ---- Live 48h blocks
    if blocks_data.get("available"):
        sb = blocks_data.get("signal_breakdown", {})
        total_skip = sum(sb.values())
        veto_live = blocks_data.get("vetoes", {})
        total_rej = blocks_data.get("total_rejected", sum(veto_live.values()))
        lines = [f"*Live observation — last {blocks_data.get('window_hours','?')}h* "
                 f"(`{Path(blocks_data.get('path','')).name}`)",
                 f"Total ta_skip: *{total_skip}*  |  REJECTED: *{total_rej}*  |  "
                 f"Near-miss: {blocks_data.get('total_near_miss','?')}"]
        if sb:
            lines.append("Signal types: " + ", ".join(f"{k}={v}" for k, v in sb.items()))
        if veto_live and total_rej:
            lines.append("REJECTED reasons:")
            for tag, n in sorted(veto_live.items(), key=lambda x: -x[1]):
                pct = n / total_rej * 100 if total_rej else 0
                lines.append(f"  • `{tag}` {n} ({pct:.1f}%)")
        bl.append({"type": "section", "text": {"type": "mrkdwn",
                                               "text": "\n".join(lines)}})
    else:
        bl.append({"type": "section", "text": {"type": "mrkdwn",
                                               "text": "_Live blocked-signal summary unavailable._"}})

    # ---- Live config (brief)
    if live_cfg and "dsl_exit" in live_cfg:
        d = live_cfg["dsl_exit"]
        bl.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f":gear: Live container DSL: `max_loss_pct={d.get('max_loss_pct')}`, "
                     f"`protect_pct={d.get('protect_pct')}`, "
                     f"`retrace_threshold={d.get('retrace_threshold')}` "
                     f"(equity_fraction={live_cfg.get('equity_fraction_per_trade')}, "
                     f"leverage={live_cfg.get('leverage')})"}
        ]})

    bl.append({"type": "divider"})
    bl.append({"type": "context", "elements": [
        {"type": "mrkdwn",
         "text": "_Caveats: deterministic AI-verdict substitute, 5 bps round-trip fee, "
                 "no slippage/funding, one position per coin, no compounding._"}
    ]})
    return bl


def post_to_slack(webhook_url: Optional[str], token: Optional[str],
                  channel: Optional[str], blocks: List[Dict[str, Any]],
                  charts: List[Path]) -> int:
    text = "Hermes A/B backtest + P0/P1/P2 optimization report"
    rc = 0

    if webhook_url:
        payload = {"text": text, "blocks": blocks}
        r = _http_post(webhook_url, payload)
        if r.get("ok") or r.get("raw") == "ok":
            print(f"[slack] webhook post ok ({len(blocks)} blocks)")
        else:
            print(f"[slack] webhook post failed: {r}", file=sys.stderr)
            rc = 1
        # Webhooks cannot attach files; upload images via token if available.
        if token and channel:
            _upload_files(token, channel, charts)
        return rc

    if token and channel:
        r = _http_post(
            "https://slack.com/api/chat.postMessage",
            {"channel": channel, "text": text, "blocks": json.dumps(blocks)},
            headers={"Authorization": f"Bearer {token}"},
        )
        if not r.get("ok"):
            print(f"[slack] chat.postMessage failed: {r}", file=sys.stderr)
            return 1
        ts = r.get("ts")
        print(f"[slack] posted message ts={ts}")
        _upload_files(token, channel, charts, thread_ts=ts)
        return 0

    print("ERROR: provide --webhook-url or (--slack-token + --channel) "
          "or the SLACK_WEBHOOK_URL / SLACK_BOT_TOKEN+SLACK_CHANNEL env vars.",
          file=sys.stderr)
    return 2


def _upload_files(token, channel, charts, thread_ts=None):
    for p in charts:
        if not p or not p.exists():
            continue
        content = p.read_bytes()
        data = {"channels": channel}
        if thread_ts:
            data["thread_ts"] = thread_ts
        r = _http_post(
            "https://slack.com/api/files.upload",
            data,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (p.name, content, "image/png")},
        )
        if r.get("ok"):
            print(f"[slack] uploaded {p.name}")
        else:
            print(f"[slack] file upload failed for {p.name}: {r}",
                  file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backtest-log", default="/tmp/backtest_1p0.log",
                    help="Path to backtest_ab_compare.py output log")
    ap.add_argument("--blocks-json", default="/tmp/blocks_48h_summary.json",
                    help="Path to blocked-signal summary JSON")
    ap.add_argument("--charts-dir", default="/tmp",
                    help="Directory to write PNG charts into")
    ap.add_argument("--webhook-url", default=os.environ.get("SLACK_WEBHOOK_URL"))
    ap.add_argument("--slack-token", default=os.environ.get("SLACK_BOT_TOKEN"))
    ap.add_argument("--channel", default=os.environ.get("SLACK_CHANNEL"))
    ap.add_argument("--no-live-config", action="store_true",
                    help="Skip reading DSL config from the hermes-trader container")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build payload & charts but do not POST to Slack")
    ap.add_argument("--dump-payload", help="Write the Slack payload JSON to this path")
    args = ap.parse_args()

    bt = parse_backtest_log(Path(args.backtest_log))
    if not bt.get("available"):
        print(f"WARNING: backtest log not found: {args.backtest_log}",
              file=sys.stderr)
    blocks_data = load_blocks_summary(Path(args.blocks_json))
    live_cfg = {} if args.no_live_config else fetch_live_dsl_config()

    charts_dir = Path(args.charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    metric_png = charts_dir / "hermes_ab_metrics.png"
    veto_png = charts_dir / "hermes_ab_vetoes.png"
    charts = [p for p in [render_metrics_chart(bt, metric_png),
                          render_veto_chart(bt, blocks_data, veto_png)] if p]
    for c in charts:
        print(f"[chart] wrote {c}")

    slack_blocks = build_blocks(bt, blocks_data, live_cfg, charts)

    if args.dump_payload:
        Path(args.dump_payload).write_text(
            json.dumps({"blocks": slack_blocks,
                        "chart_files": [str(c) for c in charts]}, indent=2))
        print(f"[payload] wrote {args.dump_payload}")

    if args.dry_run:
        print("[dry-run] skipping Slack POST")
        print(json.dumps({"blocks_count": len(slack_blocks),
                          "charts": [str(c) for c in charts]}, indent=2))
        return 0

    return post_to_slack(args.webhook_url, args.slack_token, args.channel,
                         slack_blocks, charts)


if __name__ == "__main__":
    sys.exit(main())
