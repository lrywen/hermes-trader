"""独立 dead-man 看门狗（风控第 4 层）。

设计原则（据 NexusFi / TierZero / MarketClutch 的 kill-switch 分层）：
看门狗**绝不能**运行在它所监控的同一进程/容器内，否则进程挂起时它自己也
一起死。本模块只提供"检查 + 告警"的纯逻辑与 CLI，由**外部调度器**（另一台
主机上的 cron、或容器外的 systemd timer）周期性调用：

    .venv/bin/python -m hermes_trader.watchdog \
        --heartbeat /data/session-log.jsonl --max-age 300

判定心跳新鲜度，STALE 时：
  1) 以非零退出（让外部调度/监控标记故障）；
  2) 通过既有 notify 层发送告警（``--alert``）；
  3) 预留 ``--emergency-close-cmd`` 钩子：外部传入一条平仓命令，在连续
     N 次 STALE 后执行（真正的紧急平仓需操作者显式配置，本模块默认不做）。

心跳来源支持：
  - session-log.jsonl：读末 N 行中循环事件的最新 ts（与容器内 healthcheck 同口径）；
  - 纯心跳文件：读文件内容为毫秒时间戳，或取其 mtime。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LOOP_EVENTS = ("loop_heartbeat", "scan", "research", "execute", "ta_skip",
               "near_miss")
DEFAULT_MAX_AGE_S = 300


@dataclass(frozen=True)
class WatchdogResult:
    ok: bool
    last_ts_ms: int
    age_s: float
    reason: str


def latest_heartbeat_ms(path: str, *, tail_lines: int = 200) -> tuple[int, str]:
    """从心跳来源读取最新时间戳（毫秒）与来源类型。

    优先按 session-log.jsonl 解析循环事件；若文件内容是单个数字则按心跳
    时间戳；否则退回文件 mtime。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"心跳来源不存在: {path}")

    # 1) JSONL：找最新循环事件 ts
    try:
        lines = p.read_text(errors="ignore").strip().splitlines()
    except OSError:
        lines = []
    last = 0
    for line in reversed(lines[-tail_lines:]):
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if d.get("event") in LOOP_EVENTS:
            ts = int(d.get("ts", 0) or 0)
            if ts > last:
                last = ts
    if last:
        return last, "session-log"

    # 2) 纯数字内容 = 毫秒时间戳
    body = p.read_text(errors="ignore").strip()
    if body.isdigit():
        return int(body), "heartbeat-file"

    # 3) 退回 mtime（秒→毫秒）
    return int(p.stat().st_mtime * 1000), "mtime"


def check(path: str, max_age_s: int, *, now_ms: int | None = None,
          tail_lines: int = 200) -> WatchdogResult:
    last_ms, source = latest_heartbeat_ms(path, tail_lines=tail_lines)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    age = (now - last_ms) / 1000
    if age < 0:
        return WatchdogResult(False, last_ms, age,
                              f"心跳时间在未来（时钟漂移?）source={source}")
    if age > max_age_s:
        return WatchdogResult(False, last_ms, age,
                              f"心跳过期 {age:.0f}s > {max_age_s}s source={source}")
    return WatchdogResult(True, last_ms, age,
                          f"心跳新鲜 age={age:.0f}s source={source}")


def _send_alert(text: str) -> bool:
    try:
        from hermes_trader.notify import send_text
        return bool(send_text(text, category="report"))
    except Exception as e:
        print(f"watchdog: 告警发送失败: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _run_cmd(cmd: str) -> int:
    return subprocess.run(cmd, shell=True, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="独立 dead-man 看门狗（外部调度调用）")
    ap.add_argument("--heartbeat", required=True, help="心跳来源路径")
    ap.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_S,
                    help="心跳最大允许年龄（秒），默认 300")
    ap.add_argument("--alert", action="store_true", help="STALE 时发送告警")
    ap.add_argument("--emergency-close-cmd", default=None,
                    help="STALE 时执行的外部平仓命令（默认不执行任何平仓）")
    args = ap.parse_args()

    try:
        r = check(args.heartbeat, args.max_age)
    except FileNotFoundError as e:
        msg = f"[dead-man] {e}"
        print(msg, file=sys.stderr)
        if args.alert:
            _send_alert(msg)
        if args.emergency_close_cmd:
            _run_cmd(args.emergency_close_cmd)
        return 2

    print(f"[dead-man] {'OK' if r.ok else 'STALE'}: {r.reason}")
    if r.ok:
        return 0

    if args.alert:
        _send_alert(f"[dead-man] STALE: {r.reason}")
    if args.emergency_close_cmd:
        _run_cmd(args.emergency_close_cmd)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
