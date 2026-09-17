#!/usr/bin/env python3
"""P1-6 — deploy-secret preflight, in-repo and unit-tested.

Replaces the out-of-tree /home/ldy/hermes-deploy/check_keys.py, which
(a) lived outside the repo so it could not be tested, (b) still pointed
at the dead ``.env.hermes`` symlink and the long-unused DEEPSEEK_API_KEY,
and (c) printed the first 20 characters of the Hyperliquid private key
to the terminal (and any captured log). This version:

  * drives checks from a declarative spec table;
  * recognises unfilled placeholders (empty, self-referential,
    ``$VAR`` references, ``<...>`` / CHANGEME / your-* templates);
  * NEVER echoes secret values — output contains key names and status
    words only;
  * exits 1 when a required item is missing, placeholder, or malformed;
    optional items only produce warnings.

Run on the host before a deploy::

    uv run python scripts/preflight_secrets.py
    uv run python scripts/preflight_secrets.py --deploy-dir /path/to/deploy
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_DEPLOY_DIR = Path("/home/ldy/hermes-deploy")

# Status vocabulary (kept terse on purpose — they end up in deploy logs).
OK = "ok"
MISSING = "missing"
PLACEHOLDER = "placeholder"
BAD_FORMAT = "bad_format"

_PLACEHOLDER_PATTERNS = (
    re.compile(r"\.\.\."),                       # sk-or-..., 0x...
    re.compile(r"^<.*>$"),                       # <your-private-key>
    re.compile(r"changeme", re.IGNORECASE),
    re.compile(r"your[-_]", re.IGNORECASE),      # your-key-here
    re.compile(r"replace", re.IGNORECASE),       # REPLACE_ME
    re.compile(r"x{3,}", re.IGNORECASE),         # xxxx
    re.compile(r"^todo$", re.IGNORECASE),
    re.compile(r"fill[ _-]?in", re.IGNORECASE),  # fill_in_now
)


@dataclass(frozen=True)
class SecretSpec:
    env_file: str
    key: str
    pattern: str
    required: bool
    sensitive: bool = True


@dataclass(frozen=True)
class FileState:
    path: Path
    exists: bool


@dataclass(frozen=True)
class CheckResult:
    spec: SecretSpec
    status: str


# The LIVE 10x deployment reads secrets from .env.local only.
# .env.hermes is a broken symlink and DEEPSEEK_API_KEY has no runtime
# consumer (OpenRouter is the live LLM path), so both are deliberately
# absent here.
SPECS: tuple[SecretSpec, ...] = (
    # OpenRouter issues both the legacy "sk-or-v1-..." shape and the
    # current shorter "sk-<48 chars>" keys; accept either.
    SecretSpec(
        ".env.local", "OPENROUTER_API_KEY", r"^sk-[A-Za-z0-9_-]{16,}$",
        required=True,
    ),
    SecretSpec(
        ".env.local", "HYPERLIQUID_WALLET_ADDRESS", r"^0x[a-fA-F0-9]{40}$",
        required=True, sensitive=False,
    ),
    SecretSpec(
        ".env.local", "HYPERLIQUID_PRIVATE_KEY", r"^[a-fA-F0-9]{64}$",
        required=True,
    ),
    SecretSpec(".env.local", "BRAVE_API_KEY", r"^BSA.+", required=False),
    SecretSpec(
        ".env.local", "HYPERLIQUID_MASTER_ADDRESS", r"^0x[a-fA-F0-9]{40}$",
        required=False, sensitive=False,
    ),
)


def parse_env(path: Path) -> dict[str, str]:
    """Parse a small KEY=VALUE .env file; missing file -> {}."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key] = value
    return result


def is_placeholder(key: str, value: Optional[str]) -> bool:
    """True for unfilled template values that must never pass a regex gate."""
    if value is None or not value.strip():
        return True
    v = value.strip()
    if v == key or v.startswith("$"):  # self name or $VAR / ${VAR} reference
        return True
    return any(p.search(v) for p in _PLACEHOLDER_PATTERNS)


def check_spec(spec: SecretSpec, env: dict[str, str]) -> CheckResult:
    value = env.get(spec.key)
    if value is None:
        return CheckResult(spec, MISSING)
    if is_placeholder(spec.key, value):
        return CheckResult(spec, PLACEHOLDER)
    if re.match(spec.pattern, value):
        return CheckResult(spec, OK)
    return CheckResult(spec, BAD_FORMAT)


def evaluate(deploy_dir: Path) -> tuple[dict[str, FileState], list[CheckResult]]:
    files: dict[str, FileState] = {}
    parsed: dict[str, dict[str, str]] = {}
    for name in {s.env_file for s in SPECS}:
        path = deploy_dir / name
        state = FileState(path=path, exists=path.is_file())
        files[name] = state
        parsed[name] = parse_env(path) if state.exists else {}
    results = [check_spec(s, parsed[s.env_file]) for s in SPECS]
    return files, results


def exit_code(results: list[CheckResult]) -> int:
    return 0 if all(
        r.status == OK or not r.spec.required for r in results
    ) else 1


def format_report(files: dict[str, FileState], results: list[CheckResult]) -> str:
    lines = ["=== secret preflight ==="]
    for name, state in sorted(files.items()):
        lines.append(f"[{name}] {'present' if state.exists else 'MISSING FILE'}")
        for r in (x for x in results if x.spec.env_file == name):
            req = "required" if r.spec.required else "optional"
            lines.append(f"  {r.status.upper():11s} {r.spec.key} ({req})")
    failed = [r for r in results if r.spec.required and r.status != OK]
    warned = [
        r for r in results
        if not r.spec.required and r.status != OK and r.status != MISSING
    ]
    lines.append("=== summary ===")
    if not failed:
        lines.append("PASS: all required secrets present and well-formed")
    else:
        lines.append(
            "FAIL: " + ", ".join(f"{r.spec.key} {r.status}" for r in failed)
        )
    for r in warned:
        lines.append(f"WARN: optional {r.spec.key} {r.status}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy-secret preflight")
    parser.add_argument(
        "--deploy-dir",
        type=Path,
        default=DEFAULT_DEPLOY_DIR,
        help="directory holding the .env files (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    files, results = evaluate(args.deploy_dir)
    print(format_report(files, results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
