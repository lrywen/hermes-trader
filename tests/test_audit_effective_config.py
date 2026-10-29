"""M0 (plan G0): effective trigger config <-> canonical drift audit.

The audit distinguishes value-bound-reviewed divergences (the momentumBurst
2/4 deploy-lag) from any NEW unregistered canonical<->effective drift. These
tests are hermetic: resolver functions are monkeypatched, no live /data needed.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "audit_effective_config.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("audit_effective_config", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_aligned_resolvers_yield_no_divergence(monkeypatch):
    """When effective == canonical there is nothing to report."""
    from hermes_trader.agents import config as c

    mod = _load()
    monkeypatch.setattr(c, "trigger_thresholds_params",
                        lambda *a, **k: {s[0]: c.TRIGGER_CONFIG["thresholds"][s[0]]
                                         for s in c._TRIGGER_THRESHOLDS_KEYMAP.values()})
    monkeypatch.setattr(c, "trigger_weights_params",
                        lambda *a, **k: {cam: c.TRIGGER_CONFIG["weights"][cam]
                                         for cam in c._TRIGGER_WEIGHTS_KEYMAP.values()})
    # the script imports names into its own namespace; patch there too
    monkeypatch.setattr(mod, "trigger_thresholds_params", c.trigger_thresholds_params)
    monkeypatch.setattr(mod, "trigger_weights_params", c.trigger_weights_params)
    unregistered, reviewed = mod._audit()
    assert unregistered == []
    assert reviewed == []


def test_momentum_2_4_is_reviewed(monkeypatch):
    """The known momentumBurst deploy-lag (canonical 3/3, live 2/4) is sanctioned."""
    mod = _load()
    from hermes_trader.agents.config import TRIGGER_CONFIG, _TRIGGER_WEIGHTS_KEYMAP
    canon_th = {s[0]: TRIGGER_CONFIG["thresholds"][s[0]]
                for s in mod._TRIGGER_THRESHOLDS_KEYMAP.values()}
    canon_th["momentumLookback"] = 2
    canon_th["momentumPct"] = 4.0
    monkeypatch.setattr(mod, "trigger_thresholds_params", lambda *a, **k: dict(canon_th))
    # provide a weights resolver that returns canonical for every weight
    monkeypatch.setattr(mod, "trigger_weights_params", lambda *a, **k: {
        cam: TRIGGER_CONFIG["weights"][cam] for cam in _TRIGGER_WEIGHTS_KEYMAP.values()
    })
    unregistered, reviewed = mod._audit()
    leaves = {r["leaf"] for r in reviewed}
    assert "thresholds.momentumLookback" in leaves
    assert "thresholds.momentumPct" in leaves
    assert unregistered == []


def test_new_drift_is_unregistered(monkeypatch):
    """An unexpected threshold change must surface as unregistered."""
    from hermes_trader.agents.config import TRIGGER_CONFIG, _TRIGGER_WEIGHTS_KEYMAP
    mod = _load()
    canon = {s[0]: TRIGGER_CONFIG["thresholds"][s[0]]
             for s in mod._TRIGGER_THRESHOLDS_KEYMAP.values()}
    canon["sigmaThreshold"] = 3.0  # drift
    monkeypatch.setattr(mod, "trigger_thresholds_params", lambda *a, **k: canon)
    monkeypatch.setattr(mod, "trigger_weights_params", lambda *a, **k: {
        cam: TRIGGER_CONFIG["weights"][cam] for cam in _TRIGGER_WEIGHTS_KEYMAP.values()
    })
    unregistered, _ = mod._audit()
    assert any(r["leaf"] == "thresholds.sigmaThreshold" for r in unregistered)
