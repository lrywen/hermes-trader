"""R12-E1 — PEP 585 regression sentinel.

Locks in the rename of `typing.List/Dict/Tuple/Set/FrozenSet/Type` to their
PEP 585 builtins across the production tree. Future contributors who try
to re-import the old aliases from `typing` for new annotations will fail
this test.

What we assert (and what we *don't*):

* `from typing import ...` must NOT name `List`/`Dict`/`Tuple`/`Set`/
  `FrozenSet`/`Type` in any source file under `hermes_trader/`.
  (Project requires Python >= 3.11 per `pyproject.toml`, so PEP 585 is
  always available at type-check time and at runtime.)

* In the body of any source file under `hermes_trader/`, the names
  `List`/`Dict`/`Tuple`/`Set` (PEP 585 sentinel regex with `[`) must
  not appear as a type-annotation prefix. (A `List`/`Dict` is also a
  perfectly normal variable name in Python; we only forbid the
  annotation form `Name[`.)

The test does NOT chase stragglers in tests/ — tests are free to import
whichever `typing` names they want for convenience, and we've already
audited production code separately. The cap is on the *production tree*
because that's where type-checker compatibility matters.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_ROOT = REPO_ROOT / "hermes_trader"
DEPRECATED_IMPORT_NAMES = ("List", "Dict", "Tuple", "Set", "FrozenSet", "Type")
ANNOTATION_RE = re.compile(
    r"(?<![\w.])(" + "|".join(DEPRECATED_IMPORT_NAMES) + r")(\[)"
)


def _source_files() -> list[Path]:
    return sorted(p for p in PROD_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _strip_strings_and_comments(src: str) -> str:
    """Return src with string literals and # comments replaced by
    same-length whitespace, so a regex sweep doesn't false-match on
    text inside a docstring or comment."""
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "#":
            j = src.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        if c in ('"', "'"):
            triple = i + 2 < n and src[i + 1] == c and src[i + 2] == c
            if triple:
                end = src.find(c * 3, i + 3)
                if end == -1:
                    out.append(" " * (n - i))
                    break
                out.append(" " * (end + 3 - i))
                i = end + 3
            else:
                j = i + 1
                while j < n and src[j] != c:
                    if src[j] == "\\" and j + 1 < n:
                        j += 2
                    else:
                        j += 1
                j = min(j + 1, n)
                out.append(" " * (j - i))
                i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def test_typing_deprecated_aliases_not_imported_in_production():
    """No `from typing import List/Dict/Tuple/Set/FrozenSet/Type`."""
    offenders: list[tuple[str, int, str]] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if not stripped.startswith("from typing import"):
                continue
            for name in DEPRECATED_IMPORT_NAMES:
                # Match as a bare name token (not part of `MyList`).
                if re.search(rf"(?<![\w]){name}(?![\w])", stripped):
                    offenders.append(
                        (str(path.relative_to(REPO_ROOT)), lineno, line.strip())
                    )
    assert not offenders, (
        "R12-E1 regression: typing.List/Dict/Tuple/Set/FrozenSet/Type "
        "re-imported in production. Use the PEP 585 builtins instead.\n"
        + "\n".join(f"{p}:{ln}: {l}" for p, ln, l in offenders)
    )


def test_pep585_annotation_form_used_in_production():
    """Every type-annotation use of List/Dict/Tuple/Set must be the
    PEP 585 lowercase form (list/dict/tuple/set), not the typing alias."""
    offenders: list[tuple[str, int, str]] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        safe = _strip_strings_and_comments(text)
        for lineno, line in enumerate(safe.splitlines(), start=1):
            m = ANNOTATION_RE.search(line)
            if m:
                offenders.append(
                    (str(path.relative_to(REPO_ROOT)), lineno, m.group(0))
                )
    assert not offenders, (
        "R12-E1 regression: `typing.List/Dict/Tuple/Set[...]` used as an "
        "annotation in production. Use the PEP 585 lowercase form.\n"
        + "\n".join(f"{p}:{ln}: {tok}" for p, ln, tok in offenders)
    )


def test_production_modules_still_import():
    """Sanity: every touched production module still imports cleanly
    after the rename. Catches accidental NameError from a too-greedy
    regex sweep on something like `TYPE = Dict[str, Any]`.

    Filter to only the error classes that the PEP 585 rename could
    plausibly cause (ImportError / NameError / AttributeError /
    SyntaxError / TypeError at module top level). Pre-existing runtime
    failures unrelated to the rename (e.g. prometheus DuplicateTimeseries
    from an import that has its own side effects) are out of scope.
    """
    skip = {
        "hermes_trader.__main__",  # entrypoint, exercised by the e2e suite
    }
    relevant = (ImportError, NameError, AttributeError, SyntaxError, TypeError)
    mod_names = []
    for path in _source_files():
        rel = path.relative_to(REPO_ROOT)
        mod = ".".join(rel.with_suffix("").parts)
        if mod in skip:
            continue
        mod_names.append(mod)
    failed: list[tuple[str, str, str]] = []
    for mod in mod_names:
        # Don't sys.modules.pop — many modules register prometheus
        # timeseries at import time and re-import would fail with
        # DuplicateTimeseries, which is a pre-existing test-environment
        # concern, not a PEP 585 concern.
        try:
            __import__(mod)
        except relevant as e:
            failed.append((mod, type(e).__name__, str(e)))
    assert not failed, (
        "R12-E1 regression: production modules fail to import:\n"
        + "\n".join(f"  {m}: {cls}: {msg}" for m, cls, msg in failed)
    )
