"""P0-2: agent config schema rejects silently-dangerous values.

Before this change the schema accepted any string for ``mode`` and any
non-negative float for the safety thresholds. A typo like
``max_daily_loss_usd=0`` silently disabled the daily-loss kill switch
(``ctx.daily_pnl > 0`` becomes True for any non-positive PnL), and
``mode=ON`` left the loop in OFF without telling the operator. These
tests pin the new sane-floors and mode enum.

Coverage:
  * mode enum: only OFF / LIVE / SHADOW pass; every other value rejected
  * max_daily_loss_usd sane-floor: >0 rejected (not a loss cap), < -100k rejected (typo)
  * max_total_notional_pct sane-floor: 0<x<0.5 rejected; 0 and >=0.5 ok
  * max_trade_notional_usd sane-floor: 0<x<10 rejected; 0 and >=10 ok
  * non-mode / non-threshold keys are unaffected (regression guard)
  * safety floors also fire under strict_keys=False (store-gate mode);
    as of D-FCFG-4 both HTTP write paths use strict_keys=True
"""

from __future__ import annotations

import pytest

from hermes_trader.agents.config_schema import validate_config_updates


# ── mode enum ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("good_mode", ["OFF", "LIVE", "SHADOW"])
def test_mode_enum_accepts_canonical(good_mode):
    errs = validate_config_updates({"mode": good_mode})
    assert errs == [], f"mode={good_mode!r} should be accepted, got {errs}"


@pytest.mark.parametrize(
    "bad_mode",
    [
        "on", "ON", "live", "Live", "ENABLED", "disabled", "auto", "shadow ",
        " LIVE", "", "off", "Off",
    ],
)
def test_mode_enum_rejects_typos(bad_mode):
    errs = validate_config_updates({"mode": bad_mode})
    assert any("mode" in e and "must be one of" in e for e in errs), (
        f"mode={bad_mode!r} should be rejected, got {errs}"
    )


# ── max_daily_loss_usd sane-floor ────────────────────────────────────────


def test_max_daily_loss_positive_rejected():
    """A loss cap cannot be positive — that would mean 'stop trading when
    the day is profitable >X', which is the opposite of the kill switch.
    Operators who want that should use daily_giveback_halt_pct."""
    errs = validate_config_updates({"max_daily_loss_usd": 100})
    assert any("max_daily_loss_usd" in e and "<= 0" in e for e in errs), errs


def test_max_daily_loss_absurdly_negative_rejected():
    """Anything more negative than -100k per day is almost certainly a
    typo (account is 1k/10k). We reject, not silently clamp, so the
    operator gets a loud signal."""
    errs = validate_config_updates({"max_daily_loss_usd": -1_000_000})
    assert any("max_daily_loss_usd" in e and "-100000" in e for e in errs), errs


@pytest.mark.parametrize("v", [-30, -500, -1_000, -50_000, -100_000, 0])
def test_max_daily_loss_normal_range_accepted(v):
    errs = validate_config_updates({"max_daily_loss_usd": v})
    assert errs == [], f"max_daily_loss_usd={v} should be accepted, got {errs}"


# ── max_total_notional_pct sane-floor ─────────────────────────────────────


def test_max_total_notional_pct_tiny_value_rejected():
    """A value in (0, 0.5) effectively freezes the account — one trade
    fills the cap and the rest of the day is no-op. 0 is the explicit
    'disable' signal and stays accepted."""
    errs = validate_config_updates({"max_total_notional_pct": 0.1})
    assert any(
        "max_total_notional_pct" in e and ">= 0.5" in e for e in errs
    ), errs


def test_max_total_notional_pct_zero_accepted_as_disabled():
    """0 is the explicit 'disable' signal (matches the gate's own
    convention). We must not reject it."""
    errs = validate_config_updates({"max_total_notional_pct": 0})
    assert errs == [], f"0 should be accepted as disabled, got {errs}"


@pytest.mark.parametrize("v", [0.5, 1.0, 2.5, 10.0, 50.0])
def test_max_total_notional_pct_normal_range_accepted(v):
    errs = validate_config_updates({"max_total_notional_pct": v})
    assert errs == [], f"max_total_notional_pct={v} should be accepted, got {errs}"


# ── max_trade_notional_usd sane-floor ─────────────────────────────────────


def test_max_trade_notional_dust_rejected():
    """Anything < $10 in the positive range is below the HL minimum
    order size and would silently disable the cap (gate treats 0/None
    as 'no cap')."""
    errs = validate_config_updates({"max_trade_notional_usd": 5})
    assert any(
        "max_trade_notional_usd" in e and ">= 10" in e for e in errs
    ), errs


def test_max_trade_notional_zero_accepted_as_disabled():
    errs = validate_config_updates({"max_trade_notional_usd": 0})
    assert errs == [], f"0 should be accepted as disabled, got {errs}"


@pytest.mark.parametrize("v", [10, 50, 800, 5_000, 50_000])
def test_max_trade_notional_normal_range_accepted(v):
    errs = validate_config_updates({"max_trade_notional_usd": v})
    assert errs == [], f"max_trade_notional_usd={v} should be accepted, got {errs}"


# ── regression: existing keys unaffected ──────────────────────────────────


def test_unrelated_keys_pass_through():
    """Adding sane-floors must not change behaviour for keys that were
    already valid."""
    errs = validate_config_updates({
        "leverage": 5,
        "min_ai_confidence": 0.75,
        "max_concurrent": 3,
        "equity_fraction_per_trade": 0.1,
    })
    assert errs == [], errs


def test_multiple_errors_accumulate():
    """A single update with several bad values should surface every
    error so the operator can fix them in one round-trip."""
    errs = validate_config_updates({
        "mode": "ENABLED",                 # bad mode
        "max_daily_loss_usd": 100,         # positive
        "max_total_notional_pct": 0.01,    # too small
        "max_trade_notional_usd": 1,       # dust
    })
    keys = [e.split(":", 1)[0] for e in errs]
    assert "mode" in keys
    assert "max_daily_loss_usd" in keys
    assert "max_total_notional_pct" in keys
    assert "max_trade_notional_usd" in keys
    assert len(errs) >= 4


def test_legacy_endpoint_still_runs_safety_checks():
    """The schema gate's enum / sane-floor checks are independent of the
    ``strict_keys`` unknown-key flag: they run in lenient mode too (the
    on-disk store gate still validates with strict_keys=False). D-FCFG-4
    moved both HTTP write paths to strict; lenient remains for store use."""
    errs = validate_config_updates(
        {"mode": "ENABLED", "_my_custom_key": 1},
        strict_keys=False,
    )
    assert any("mode" in e for e in errs), (
        "safety check should run even when strict_keys=False"
    )
    # unknown key is NOT flagged in lenient mode (store round-trip policy)
    assert not any("unknown key" in e for e in errs)


def test_string_mode_value_passes_type_check_then_enum_rejects():
    """The type system accepts ``str`` for mode (Pydantic str field).
    The new enum check runs *after* the type check, so a non-canonical
    string still produces a single enum error, not a type error +
    enum error."""
    errs = validate_config_updates({"mode": "garbage"})
    assert len(errs) == 1
    assert "mode: must be one of" in errs[0]
