"""Versioned Pydantic contracts for cross-process read boundaries.

The package root is the stable public API; domain modules can also be imported
directly. Parsing never raises into a read endpoint, unknown forward fields are
ignored, and malformed position rows are skipped individually.

Versioning: bump MAJOR for a breaking reader/writer change, MINOR for an
additive backward-compatible change, and PATCH for documentation or fixture
clarifications that do not change validation semantics.
"""
from .heartbeat import LoopHeartbeat, parse_heartbeat
from .positions import EnvelopePosition, SnapshotMeta, parse_snapshot

CONTRACT_VERSION = "1.0.0"

__all__ = [
    "CONTRACT_VERSION",
    "EnvelopePosition",
    "LoopHeartbeat",
    "SnapshotMeta",
    "parse_heartbeat",
    "parse_snapshot",
]
