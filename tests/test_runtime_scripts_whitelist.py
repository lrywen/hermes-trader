"""P0-1 — runtime image scripts/ whitelist guard.

The production image used to ``COPY scripts/ scripts/`` wholesale, shipping
~70 files including nine ``backtest_*`` research/replay scripts and many
one-off ops tools. Only the scripts with a proven in-container runtime caller
must ship. This test pins that set from three independent angles so the
whitelist cannot silently drift:

  1. the single source of truth (``scripts/runtime_whitelist.py``) lists a
     file that does not exist on disk  -> fail (stale whitelist);
  2. a research/backtest script is whitelisted -> fail (isolation regression);
  3. the Dockerfile does not COPY exactly the whitelisted set -> fail
     (whitelist and image contents diverged).

Adding a new runtime script therefore requires touching BOTH the constant and
the Dockerfile, and proves the file exists.
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.runtime_whitelist import RUNTIME_SCRIPTS

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def test_whitelist_entries_are_real_files():
    missing = [name for name in RUNTIME_SCRIPTS if not (SCRIPTS_DIR / name).is_file()]
    assert not missing, f"whitelisted runtime scripts missing from scripts/: {missing}"


def test_whitelist_has_no_duplicates():
    assert len(RUNTIME_SCRIPTS) == len(set(RUNTIME_SCRIPTS)), (
        "RUNTIME_SCRIPTS contains duplicate entries"
    )


def test_no_research_or_backtest_scripts_whitelisted():
    forbidden = [
        name
        for name in RUNTIME_SCRIPTS
        if name.startswith("backtest_")
        or name.startswith("backfill_")
        or name in {"calibrate_regime_thresholds.py"}
    ]
    assert not forbidden, (
        f"research/backtest/calibration scripts must not ship in the runtime "
        f"image: {forbidden}"
    )


def _dockerfile_copied_scripts() -> set[str]:
    """Extract the explicit per-file ``COPY scripts/<x> scripts/<x>`` lines.

    Only the explicit single-source form is recognised; a blanket
    ``COPY scripts/ scripts/`` is intentionally NOT matched so reverting to the
    wholesale copy fails this test.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    copied: set[str] = set()
    pattern = re.compile(r"^COPY\s+scripts/(\S+)\s+scripts/\S*\s*$")
    for line in text.splitlines():
        line = line.strip()
        match = pattern.match(line)
        if match and match.group(1).endswith(".py"):
            # The privilege-drop docker-entrypoint.sh is infra copied for the
            # ENTRYPOINT, not a Python runtime script, so only .py entries are
            # reconciled against RUNTIME_SCRIPTS.
            copied.add(match.group(1))
    return copied


def test_dockerfile_copies_exactly_the_whitelist():
    copied = _dockerfile_copied_scripts()
    whitelist = set(RUNTIME_SCRIPTS)
    assert copied, (
        "Dockerfile has no explicit `COPY scripts/<file> scripts/...` lines; "
        "a blanket COPY scripts/ is forbidden by P0-1"
    )
    missing_in_dockerfile = whitelist - copied
    assert not missing_in_dockerfile, (
        f"whitelisted scripts not copied into the image: {sorted(missing_in_dockerfile)}"
    )
    extra_in_dockerfile = copied - whitelist
    assert not extra_in_dockerfile, (
        f"Dockerfile copies scripts not on the runtime whitelist: "
        f"{sorted(extra_in_dockerfile)}"
    )


def test_dockerfile_has_no_wholesale_scripts_copy():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(r"^COPY\s+scripts/?\s+scripts/?\s*$", text, re.MULTILINE), (
        "blanket `COPY scripts/ scripts/` is forbidden by P0-1; enumerate the "
        "whitelist explicitly"
    )
