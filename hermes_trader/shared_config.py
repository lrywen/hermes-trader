"""Shared cross-component YAML config loader.

Single source of truth for ``~/.hermes-trading/config.yaml``. Previously this
loader was duplicated verbatim across ``agents/research.py``,
``agents/risk_gates.py`` and ``server.py`` (the last under a private name to
avoid a circular import). All three now delegate here.

The module is intentionally dependency-free (stdlib + optional PyYAML) so it
can be imported from the server layer without creating an import cycle through
the heavy agents subgraph.
"""

from __future__ import annotations

import os
from typing import Any, Dict

SHARED_CONFIG_PATH = os.path.expanduser("~/.hermes-trading/config.yaml")


def load_shared_config(path: str = SHARED_CONFIG_PATH) -> Dict[str, Any]:
    """Load the cross-component shared config YAML.

    Returns an empty dict if the file does not exist, cannot be parsed, or
    PyYAML is unavailable. This best-effort behaviour matches the original
    inlined loaders: missing/corrupt shared config must never take down a
    caller (research pipeline, risk gate, or SSE stream).
    """
    try:
        import yaml  # local import keeps this module importable without PyYAML
    except ImportError:  # pragma: no cover - PyYAML is a hard dep in practice
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data or {}
    except Exception:
        return {}
