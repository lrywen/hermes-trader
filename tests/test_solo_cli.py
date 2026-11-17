from __future__ import annotations

import pytest

from hermes_trader import solo


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("10", 10),
        ("0.65", 0.65),
        ("SHADOW", "SHADOW"),
    ],
)
def test_coerce(raw, expected):
    assert solo._coerce(raw) == expected
    assert type(solo._coerce(raw)) is type(expected)


def test_set_dotted_creates_nested_blocks():
    target = {}
    solo._set_dotted(target, "dsl_exit.max_loss_pct", 1.5)
    assert target == {"dsl_exit": {"max_loss_pct": 1.5}}


def test_tune_allowlist_rejects_non_core_keys():
    parser = solo.build_parser()
    args = parser.parse_args(["tune", "coin_blocklist=[]"])
    assert args.assignments == ["coin_blocklist=[]"]
    assert "coin_blocklist" not in solo._SAFE_KEYS