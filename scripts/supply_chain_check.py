#!/usr/bin/env python3
"""Supply-chain checks for the repo, as code instead of a checklist.

Ported from upstream pathiel (security review 2026-10-25) and adapted to this
repo's toolchain: dependencies are locked with **uv.lock** (TOML, every pin
carries a sha256) and installed with `uv sync --frozen`, not a pip
requirements.lock. The workflow / worm-artifact / registry-token checks are
toolchain-agnostic and ported verbatim.

    python scripts/supply_chain_check.py            # offline, every check

Offline mode is deterministic, needs no network, and is what
tests/test_supply_chain.py runs on every commit. Each check names the gap it
closes (the 2025-2026 npm/PyPI compromises: the Qix maintainer phish, the
Shai-Hulud install-hook worms, and the autumn 2026 wave).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PY_LOCK = ROOT / "uv.lock"

# File names the Shai-Hulud worms (2025-09, 2025-11) dropped into repos and
# packages. Sources: CISA alert 2025-09-23; Microsoft guidance 2025-12-09.
WORM_FILENAMES = re.compile(
    r"(^|/)(setup_bun\.js|bun_environment\.js|shai-hulud(-workflow)?\.ya?ml|"
    r"formatter_[^/]*\.ya?ml|truffleSecrets\.json|actionsSecrets\.json)$")

_SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.y*ml"))


def check_workflows() -> list[str]:
    """CI pins every action to a commit SHA, declares least-privilege
    permissions, and never runs untrusted PR code with secrets
    (pull_request_target)."""
    problems = []
    for wf in _workflow_files():
        text = wf.read_text()
        rel = wf.relative_to(ROOT)
        # Strip full-line and inline comments so prose mentioning a dangerous
        # key (e.g. a comment explaining why pull_request_target is banned)
        # does not itself trip the check.
        code_lines = []
        for line in text.splitlines():
            stripped = line.split("#", 1)[0]
            code_lines.append(stripped)
        code_text = "\n".join(code_lines)
        for line_no, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            m = re.search(r"\buses:\s*(\S+)", code)
            if m and not m.group(1).startswith("./") and not _SHA_PIN.match(m.group(1)):
                problems.append(f"{rel}:{line_no}: action not pinned to a commit SHA: {m.group(1)}")
            if re.search(r"\bnpm\s+(ci|install|i)\b", code) and "--ignore-scripts" not in code:
                problems.append(f"{rel}:{line_no}: npm install without --ignore-scripts")
        if not re.search(r"(?m)^permissions:", code_text):
            problems.append(f"{rel}: no top-level permissions block")
        if re.search(r"(?m)^\s*\w[\w-]*:\s*write\s*$", code_text):
            problems.append(f"{rel}: grants a write permission")
        if re.search(r"(?m)^\s*pull_request_target\s*:", code_text) or \
                re.search(r"(?m)^on:\s*pull_request_target\s*$", code_text):
            problems.append(f"{rel}: uses pull_request_target")
    return problems


def check_worm_artifacts() -> list[str]:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.splitlines()
    return [f"tracked file matches a Shai-Hulud artifact name: {f}"
            for f in files if WORM_FILENAMES.search(f)]


def check_no_registry_tokens() -> list[str]:
    """No tracked .npmrc carries a registry token."""
    problems = []
    for rc in [ROOT / ".npmrc"]:
        if rc.exists() and re.search(r"_authToken|_auth\s*=|//.*:_password", rc.read_text()):
            problems.append(f"{rc.relative_to(ROOT)} carries a registry credential")
    return problems


def check_uv_lock_hashes() -> list[str]:
    """Every package in uv.lock carries at least one sha256 hash (sdist or a
    wheel), so `uv sync --frozen` installs in hash-verified mode. uv.lock is
    TOML; we scan for `hash = "sha256:..."` entries per [[package]] block."""
    problems = []
    if not PY_LOCK.exists():
        return ["uv.lock is missing"]
    text = PY_LOCK.read_text()
    blocks = re.split(r"(?m)^\[\[package\]\]\s*$", text)
    for block in blocks[1:]:
        name_m = re.search(r'(?m)^name\s*=\s*"([^"]+)"', block)
        name = name_m.group(1) if name_m else "<unknown>"
        # The project itself is installed editable from source (no registry
        # artifact, hence no hash) — only registry-pinned deps need a sha256.
        if 'source = { registry =' not in block:
            continue
        if 'hash = "sha256:' not in block:
            problems.append(f"uv.lock: package {name} has no sha256 hash")
    if len(blocks) <= 1:
        problems.append("uv.lock has no [[package]] pins")
    return problems


OFFLINE_CHECKS = (
    check_workflows,
    check_worm_artifacts,
    check_no_registry_tokens,
    check_uv_lock_hashes,
)


def main() -> int:
    problems: list[str] = []
    for check in OFFLINE_CHECKS:
        problems.extend(check())
    if problems:
        print("Supply-chain problems found:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("Supply-chain checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
