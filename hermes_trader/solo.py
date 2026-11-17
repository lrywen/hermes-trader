#!/usr/bin/env python3
"""Single-operator entry point for the Hermes SHADOW workflow.

The command intentionally wraps existing modules instead of creating a second
strategy engine. It provides a short, predictable path for status checks,
parameter changes, quick backtests and watch summaries.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_BASE = os.environ.get("HERMES_SOLO_API_BASE", "http://127.0.0.1:8000")
_SAFE_KEYS = {
    "min_ai_confidence",
    "equity_fraction_per_trade",
    "max_trade_notional_usd",
    "max_daily_loss_usd",
    "cooldown_min",
    "dsl_exit.max_loss_pct",
    "dsl_exit.protect_pct",
    "dsl_exit.retrace_threshold",
    "scan.minCompositeScore",
}


def _install_config_path() -> None:
    """Resolve one runtime config before importing config_store.

    The container uses /data. Host-side developer runs use the deployment
    directory when present, otherwise the repository-local config snapshot.
    """
    if os.environ.get("HERMES_AGENT_CONFIG_FILE"):
        return
    for candidate in (
        Path("/data/.agent-config.json"),
        Path("/home/ldy/hermes-deploy/.agent-config.json"),
        _REPO_ROOT / ".agent-config.json",
    ):
        if candidate.is_file():
            os.environ["HERMES_AGENT_CONFIG_FILE"] = str(candidate)
            return


def _load_runtime() -> tuple[Any, Any, Any]:
    from hermes_trader.agents import shadow_book
    from hermes_trader.agents.config_store import read_agent_config, update_agent_config
    from hermes_trader.agents.memory import memory

    memory.load()
    return read_agent_config, update_agent_config, shadow_book


def _api_json(path: str, timeout: float = 5.0) -> dict[str, Any] | list[Any] | None:
    try:
        with urllib.request.urlopen(_API_BASE + path, timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    node = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _coerce(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _human_status(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    risk = payload.get("risk") or {}
    shadow = payload.get("shadow") or {}
    mode = risk.get("mode") or summary.get("mode") or "?"
    return (
        f"mode={mode} equity=${float(summary.get('equity') or 0):.2f} "
        f"daily_pnl=${float(summary.get('daily_pnl') or 0):.2f} "
        f"positions={summary.get('open_positions', 0)} "
        f"feed={risk.get('feed_status', '?')} "
        f"shadow_equity=${float(shadow.get('equity') or 0):.2f} "
        f"shadow_trades={shadow.get('closed_count', 0)}"
    )


def cmd_status(args: argparse.Namespace) -> int:
    read_agent_config, _, _ = _load_runtime()
    payload: dict[str, Any] = {
        "summary": _api_json("/api/dashboard/summary") or {},
        "risk": _api_json("/api/dashboard/risk-status") or {},
        "shadow": _api_json("/api/dashboard/shadow/account") or {},
    }
    if args.json:
        payload["config"] = read_agent_config()
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(_human_status(payload))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    summary = _api_json("/api/dashboard/summary") or {}
    risk = _api_json("/api/dashboard/risk-status") or {}
    payload = {
        "mode": risk.get("mode", "?"),
        "status": summary.get("status", "?"),
        "last_scan_triggers": summary.get("last_scan_triggers", 0),
        "last_tick_age_s": summary.get("last_tick_age_s"),
        "feed_status": risk.get("feed_status"),
        "feed_age_s": risk.get("feed_age_s"),
        "global_halt": risk.get("global_halt"),
        "daily_loss_limit": risk.get("daily_loss_limit"),
        "open_positions": summary.get("open_positions", 0),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    read_agent_config, update_agent_config, _ = _load_runtime()
    updates = []
    for assignment in args.assignments:
        if "=" not in assignment:
            raise SystemExit(f"invalid assignment {assignment!r}; use KEY=VALUE")
        key, raw = assignment.split("=", 1)
        if key not in _SAFE_KEYS:
            raise SystemExit(f"key {key!r} is not in the solo allowlist")
        updates.append((key, _coerce(raw.strip())))

    with update_agent_config(via="solo") as config:
        for key, value in updates:
            _set_dotted(config, key, value)
    after = read_agent_config()
    for key, _ in updates:
        node: Any = after
        for part in key.split("."):
            node = node[part] if isinstance(node, dict) else None
        print(f"{key}={node}")
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    read_agent_config, update_agent_config, _ = _load_runtime()
    cfg = read_agent_config()
    if str(cfg.get("mode", "OFF")).upper() == "SHADOW":
        print("mode=SHADOW")
    else:
        with update_agent_config(via="solo_shadow") as config:
            config["mode"] = "SHADOW"
        print("mode=SHADOW")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    config_path = os.environ.get("HERMES_AGENT_CONFIG_FILE")
    if not config_path:
        raise SystemExit("no runtime config found")
    command = [
        sys.executable,
        str(_REPO_ROOT / "scripts" / "backtest_quick.py"),
        "--days",
        str(args.days),
        "--coins-count",
        str(args.coins_count),
        "--interval",
        args.interval,
    ]
    if args.coins:
        command.extend(["--coins", args.coins])
    env = dict(os.environ)
    env["HERMES_AGENT_CONFIG_FILE"] = config_path
    env["HERMES_BACKTEST"] = "1"
    return subprocess.run(command, cwd=_REPO_ROOT, env=env, check=False).returncode


def cmd_preflight(args: argparse.Namespace) -> int:
    read_agent_config, _, shadow_book = _load_runtime()
    cfg = read_agent_config()
    checks = {
        "mode_shadow": str(cfg.get("mode", "")).upper() == "SHADOW",
        "long_only": bool(cfg.get("scan_long_only", False)),
        "shorts_disabled": not bool((cfg.get("runner_entry_gate") or {}).get("allow_shorts", True)),
        "short_shadow_disabled": not bool((cfg.get("s3_shadow_shorts") or {}).get("enabled", False)),
        "single_concurrent": int(cfg.get("max_concurrent", 0) or 0) == 1,
        "notional_lte_30": float(cfg.get("max_trade_notional_usd", 0) or 0) <= 30,
        "daily_loss_lte_zero": float(cfg.get("max_daily_loss_usd", 0) or 0) < 0,
        "shadow_flat": int((shadow_book.get_account() or {}).get("open_positions", 0) or 0) == 0,
    }
    for name, ok in checks.items():
        print(f"{name}={str(ok).lower()}")
    return 0 if all(checks.values()) else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-solo")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show mode, equity, risk and shadow state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="show the compact live-status view")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("tune", help="change one or more allowlisted parameters")
    p.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("shadow", help="return the runtime to SHADOW mode")
    p.set_defaults(func=cmd_shadow)

    p = sub.add_parser("backtest", help="run the minimal quick backtest")
    p.add_argument("--days", type=int, default=7, choices=range(1, 8))
    p.add_argument("--coins", default="", help="comma-separated symbols")
    p.add_argument("--coins-count", type=int, default=10, choices=range(1, 11))
    p.add_argument("--interval", choices=("15m", "1h", "4h"), default="1h")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("preflight", help="validate the small-live safety envelope")
    p.set_defaults(func=cmd_preflight)
    return parser


def main(argv: list[str] | None = None) -> int:
    _install_config_path()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())