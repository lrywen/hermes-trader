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
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
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
_JOB_ID_RE = re.compile(r"^bt-\d{8}T\d{6}Z-[0-9a-f]{10}$")


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

def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _job_root() -> Path:
    configured = os.environ.get("HERMES_SOLO_JOB_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "hermes-solo-jobs"


def _validate_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise SystemExit(f"invalid backtest job id {job_id!r}")
    return job_id


def _job_paths(job_id: str) -> tuple[Path, Path, Path]:
    _validate_job_id(job_id)
    root = _job_root() / job_id
    return root / "job.json", root / "backtest.log", root


def _write_job_meta(job_id: str, payload: dict[str, Any]) -> None:
    meta_path, _, job_dir = _job_paths(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = meta_path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, meta_path)


def _read_job_meta(job_id: str) -> dict[str, Any]:
    meta_path, _, _ = _job_paths(job_id)
    if not meta_path.is_file():
        raise SystemExit(f"backtest job not found: {job_id}")
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"backtest job metadata is unreadable: {job_id}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"backtest job metadata is invalid: {job_id}")
    return payload


def _update_job_meta(job_id: str, **changes: Any) -> dict[str, Any]:
    payload = _read_job_meta(job_id)
    payload.update(changes)
    _write_job_meta(job_id, payload)
    return payload


def _pid_is_running(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _backtest_command(args: argparse.Namespace) -> list[str]:
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
    return command


def _backtest_env(config_path: str) -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_AGENT_CONFIG_FILE"] = config_path
    env["HERMES_BACKTEST"] = "1"
    return env


def _active_result_state(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") in {"queued", "running"} and not _pid_is_running(payload.get("pid")):
        payload = dict(payload)
        payload["status"] = "lost"
        payload["message"] = "worker exited before recording a terminal state"
    return payload


def _print_backtest_result(job_id: str, *, as_json: bool) -> int:
    meta_path, log_path, _ = _job_paths(job_id)
    payload = _active_result_state(_read_job_meta(job_id))
    log_text = ""
    if log_path.is_file():
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log_text = f"<unable to read log: {exc}>"
    view = {
        "job_id": job_id,
        "status": payload.get("status", "unknown"),
        "pid": payload.get("pid"),
        "exit_code": payload.get("exit_code"),
        "created_at": payload.get("created_at"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "parameters": payload.get("parameters", {}),
        "meta_path": str(meta_path),
        "log_path": str(log_path),
        "result": log_text,
    }
    if payload.get("message"):
        view["message"] = payload["message"]
    if as_json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
    else:
        print(
            f"job={job_id} status={view['status']} pid={view['pid']} "
            f"exit_code={view['exit_code']} log={log_path}"
        )
        if view.get("message"):
            print(f"message={view['message']}")
        if log_text:
            print(log_text, end="" if log_text.endswith("\n") else "\n")
    return 0 if payload.get("status") == "completed" else 2


def _run_background_worker(job_id: str, args: argparse.Namespace, config_path: str) -> int:
    _, log_path, job_dir = _job_paths(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    _update_job_meta(job_id, status="running", pid=os.getpid(), started_at=_utc_now())
    command = _backtest_command(args)
    env = _backtest_env(config_path)
    try:
        with log_path.open("wb") as output:
            completed = subprocess.run(
                command,
                cwd=_REPO_ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        exit_code = int(completed.returncode)
    except Exception as exc:
        with log_path.open("a", encoding="utf-8") as output:
            output.write(f"worker error: {type(exc).__name__}: {exc}\n")
        exit_code = 127
    _update_job_meta(
        job_id,
        status="completed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        finished_at=_utc_now(),
    )
    return exit_code


def _start_background_backtest(args: argparse.Namespace, config_path: str) -> int:
    now = datetime.now(timezone.utc)
    job_id = f"bt-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:10]}"
    parameters = {
        "days": args.days,
        "coins": args.coins,
        "coins_count": args.coins_count,
        "interval": args.interval,
    }
    _write_job_meta(
        job_id,
        {
            "version": 1,
            "job_id": job_id,
            "status": "queued",
            "pid": 0,
            "exit_code": None,
            "created_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "parameters": parameters,
        },
    )
    worker_args = [
        sys.executable,
        "-m",
        "hermes_trader.solo",
        "backtest",
        "--days",
        str(args.days),
        "--coins-count",
        str(args.coins_count),
        "--interval",
        args.interval,
        "--job-id",
        job_id,
    ]
    if args.coins:
        worker_args.extend(["--coins", args.coins])
    try:
        worker = subprocess.Popen(
            worker_args,
            cwd=_REPO_ROOT,
            env=_backtest_env(config_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        _update_job_meta(
            job_id,
            status="failed",
            exit_code=127,
            finished_at=_utc_now(),
            message=f"failed to start worker: {type(exc).__name__}: {exc}",
        )
        raise SystemExit(f"failed to start backtest worker: {exc}") from exc
    print(f"job={job_id}")
    print("status=queued")
    print(f"result=hermes-solo backtest --result {job_id}")
    print(f"log={_job_paths(job_id)[1]}")
    return 0

def _human_status(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    risk = payload.get("risk") or {}
    shadow = payload.get("shadow") or {}
    mode = risk.get("mode") or summary.get("mode") or "?"
    return (
        f"profile={payload.get('profile', 'full')} mode={mode} "
        f"equity=${float(summary.get('equity') or 0):.2f} "
        f"daily_pnl=${float(summary.get('daily_pnl') or 0):.2f} "
        f"positions={summary.get('open_positions', 0)} "
        f"feed={risk.get('feed_status', '?')} "
        f"shadow_equity=${float(shadow.get('equity') or 0):.2f} "
        f"shadow_trades={shadow.get('closed_count', 0)}"
    )


def cmd_status(args: argparse.Namespace) -> int:
    read_agent_config, _, _ = _load_runtime()
    payload: dict[str, Any] = {
        "profile": os.environ.get("HERMES_PROFILE", "full"),
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
    if args.result:
        return _print_backtest_result(args.result, as_json=args.json)
    if args.job_id:
        return _run_background_worker(args.job_id, args, config_path)
    if args.background:
        return _start_background_backtest(args, config_path)
    return subprocess.run(
        _backtest_command(args),
        cwd=_REPO_ROOT,
        env=_backtest_env(config_path),
        check=False,
    ).returncode


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
    p.add_argument("--background", action="store_true", help="start a detached quick-backtest job")
    p.add_argument("--result", metavar="JOB_ID", help="read the latest state and output of a background job")
    p.add_argument("--json", action="store_true", help="print --result output as JSON")
    p.add_argument("--job-id", help=argparse.SUPPRESS)
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
