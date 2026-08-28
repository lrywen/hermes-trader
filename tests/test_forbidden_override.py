"""P0-3: FORBIDDEN_OVERRIDE — five force-execute / bypass switches must
never be armed without an explicit AI agreement in the same config write.

Each of the five flags can disable a safety gate by itself:
  * composite_force_execute   — bypasses the confidence floor on high
                               composite score
  * breakout_force_execute    — bypasses AI confirmation on breakout
  * whale_force_execute       — bypasses AI confirmation on whale signal
  * whale_regime_bypass       — lets a whale signal clear the
                               counter-regime gate
  * spread_gate_fail_open     — turns the spread/impact gate from
                               fail-CLOSED into fail-OPEN (the most
                               dangerous of the five — a single bool
                               flip lets an unprotected trade through
                               when the data feed is down)

The contract: arming any of them requires ``override_requires_ai=true``
in the SAME update. The schema enforces this; the runtime caller is
expected to log a ``force_override_armed`` audit line on each consult.
"""

from __future__ import annotations

import pytest

from hermes_trader.agents.config_schema import validate_config_updates


_FORCE_OVERRIDE_KEYS = (
    "composite_force_execute",
    "breakout_force_execute",
    "whale_force_execute",
    "whale_regime_bypass",
    "spread_gate_fail_open",
)


# ── per-key negative tests (arming without override_requires_ai) ──────────


@pytest.mark.parametrize("fkey", _FORCE_OVERRIDE_KEYS)
def test_force_switch_alone_is_rejected(fkey):
    """Arming any of the five without ``override_requires_ai=true`` is the
    exact attack the schema guard exists to prevent. Must be rejected
    with a single, named error."""
    errs = validate_config_updates({fkey: True})
    assert any(
        fkey in e and "override_requires_ai" in e for e in errs
    ), f"{fkey}=true alone should be rejected, got {errs}"


# ── positive cases ────────────────────────────────────────────────────────


@pytest.mark.parametrize("fkey", _FORCE_OVERRIDE_KEYS)
def test_force_switch_with_override_requires_ai_is_accepted(fkey):
    errs = validate_config_updates({
        fkey: True,
        "override_requires_ai": True,
    })
    assert errs == [], (
        f"{fkey}=true with override_requires_ai=true should be accepted, "
        f"got {errs}"
    )


def test_all_five_can_be_armed_in_one_update():
    errs = validate_config_updates({
        **{k: True for k in _FORCE_OVERRIDE_KEYS},
        "override_requires_ai": True,
    })
    assert errs == [], errs


# ── disarm path is not affected ──────────────────────────────────────────


@pytest.mark.parametrize("fkey", _FORCE_OVERRIDE_KEYS)
def test_force_switch_false_is_always_accepted(fkey):
    """Turning a force switch OFF is a safety improvement, never a risk;
    it must be accepted with or without override_requires_ai."""
    for override in (True, False):
        errs = validate_config_updates({
            fkey: False,
            "override_requires_ai": override,
        })
        assert errs == [], f"{fkey}=False override={override} should pass, got {errs}"


# ── falsey values do NOT arm the override ────────────────────────────────


@pytest.mark.parametrize("bad_val", [None, 0, 1, "true", "1", 0.0, [], {}])
@pytest.mark.parametrize("fkey", _FORCE_OVERRIDE_KEYS)
def test_only_literal_true_arms_the_override(fkey, bad_val):
    """The schema guard is intentionally strict: only ``True`` (the Python
    bool) arms the override. A string ``"true"`` / integer 1 / None is
    not enough — that's how accidental string-from-JSON bugs sneak in.
    Those values will trip the standard type-check rather than the
    override check."""
    # Strings and 0/None will fail the bool type-check; that's fine.
    if isinstance(bad_val, bool):
        # bool True IS caught (covered by the negative test above)
        # bool False is allowed (covered above)
        pytest.skip("bool tested separately")
    errs = validate_config_updates({fkey: bad_val})
    if bad_val is None:
        # None is treated as "delete key" on the legacy endpoint; the
        # strict path may reject; both are acceptable as long as the
        # override check does NOT fire (we want only True to trigger it).
        assert not any("FORBIDDEN_OVERRIDE" in e for e in errs)
    else:
        # Type mismatch is fine; override guard must not fire.
        assert not any("FORBIDDEN_OVERRIDE" in e or
                       "override_requires_ai" in e
                       for e in errs), (
            f"{fkey}={bad_val!r} tripped override guard, got {errs}"
        )


# ── multi-error accumulation ──────────────────────────────────────────────


def test_two_force_switches_each_report_their_own_error():
    """If the operator tries to arm two switches at once, every offending
    switch gets its own error so the operator can fix them in one
    round-trip rather than whack-a-mole."""
    errs = validate_config_updates({
        "composite_force_execute": True,
        "whale_force_execute": True,
    })
    of_related = [e for e in errs if "override_requires_ai" in e]
    assert len(of_related) >= 2, (
        f"expected two override-guard errors, got {errs}"
    )
    assert any("composite_force_execute" in e for e in of_related)
    assert any("whale_force_execute" in e for e in of_related)


# ── regression: existing keys still behave ───────────────────────────────


def test_unrelated_keys_unaffected():
    errs = validate_config_updates({
        "leverage": 5,
        "max_concurrent": 3,
        "max_daily_loss_usd": -50,
        "min_ai_confidence": 0.7,
    })
    assert errs == [], errs


def test_override_requires_ai_alone_is_accepted():
    """Setting ``override_requires_ai`` on its own (without arming any
    force switch) is harmless — it just declares the operator wants AI
    sign-off on future overrides. Must not be rejected."""
    errs = validate_config_updates({"override_requires_ai": True})
    assert errs == [], errs
