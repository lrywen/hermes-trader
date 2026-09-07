"""C13 — stage-tagged structured errors for the scan / research HTTP surface.

Locks in the C13 contract:

* ``perception._scan_stage`` tags any exception raised inside the block with a
  ``scan_stage`` attribute (and ``ScanStageError`` carries ``stage``), re-raising
  the original exception unchanged — scan/strategy logic is untouched.
* ``scan_once`` tags the mid-prefetch stage ("prefetch.mids") and the internal
  universe fetch ("universe") when those calls blow up, so the HTTP layer in
  server.py can return ``{"stage": ...}`` instead of a bare string.
* ``research()`` tags prefetch failures by source ("prefetch.funding" /
  "prefetch.candles" / ...) and LLM-stage failures ("llm") on the raised
  exception's ``research_stage`` attribute.
"""

from __future__ import annotations

import pytest


# ── _scan_stage tagging primitive ───────────────────────────────────────────


def test_scan_stage_tags_exception_attribute():
    from hermes_trader.agents.perception import _scan_stage

    with pytest.raises(RuntimeError) as exc:
        with _scan_stage("prefetch.mids"):
            raise RuntimeError("boom")

    assert getattr(exc.value, "scan_stage", None) == "prefetch.mids"
    assert "boom" in str(exc.value)


def test_scan_stage_does_not_overwrite_existing_tag():
    from hermes_trader.agents.perception import _scan_stage

    with pytest.raises(ValueError) as exc:
        with _scan_stage("outer"):
            try:
                raise ValueError("inner")
            except Exception as e:
                e.scan_stage = "inner"  # type: ignore[attr-defined]
                raise

    assert getattr(exc.value, "scan_stage", None) == "inner"


def test_scan_stage_error_carries_stage():
    from hermes_trader.agents.perception import ScanStageError

    e = ScanStageError("universe", "meta fetch failed")
    assert e.stage == "universe"
    assert isinstance(e, RuntimeError)


# ── scan_once stage tagging ─────────────────────────────────────────────────


def test_scan_once_tags_mids_prefetch_failure(monkeypatch):
    """When fetch_all_mids raises, the exception reaching the caller must be
    tagged scan_stage="prefetch.mids"."""
    import hermes_trader.agents.perception as perception

    class _Boom(RuntimeError):
        pass

    def _explode(*_a, **_kw):
        raise _Boom("mids http boom")

    monkeypatch.setattr(perception, "fetch_all_mids", _explode)

    with pytest.raises(_Boom) as exc:
        perception.scan_once(universe=[{"coin": "BTC", "type": "perp"}])

    assert getattr(exc.value, "scan_stage", None) == "prefetch.mids"


def test_scan_once_tags_internal_universe_failure(monkeypatch):
    """When scan_once fetches the universe itself (universe=None) and
    get_universe raises, the exception must carry scan_stage="universe"."""
    import hermes_trader.agents.perception as perception

    class _Boom(RuntimeError):
        pass

    def _explode_mids(*_a, **_kw):
        return {}

    def _explode_universe(*_a, **_kw):
        raise _Boom("meta http boom")

    monkeypatch.setattr(perception, "fetch_all_mids", _explode_mids)
    monkeypatch.setattr(perception, "get_universe", _explode_universe)

    with pytest.raises(_Boom) as exc:
        perception.scan_once(universe=None)

    assert getattr(exc.value, "scan_stage", None) == "universe"


# ── research() stage tagging ────────────────────────────────────────────────


def test_research_tags_prefetch_funding_failure(monkeypatch):
    """A prefetch failure whose message names funding must surface as
    research_stage="prefetch.funding"."""
    import hermes_trader.agents.research as research

    def _explode(_coin, _skip):
        raise RuntimeError(
            "parallel data-fetch for BTC failed/timed out (funding timed out after 8s)"
        )

    monkeypatch.setattr(research, "_parallel_prefetch", _explode)

    with pytest.raises(RuntimeError) as exc:
        research.research("BTC", {"coin": "BTC"})

    assert getattr(exc.value, "research_stage", None) == "prefetch.funding"


def test_research_tags_llm_stage_failure(monkeypatch):
    """A failure from the in-process debate must surface as
    research_stage="llm"."""
    import hermes_trader.agents.research as research
    from hermes_trader.models.types import Candle

    now_ms = 1_700_000_000_000

    def _bar(i: int) -> Candle:
        base = 100.0 + i
        return Candle(t=now_ms + i * 3_600_000, o=base, h=base + 1,
                      l=base - 1, c=base + 0.5, v=1000.0)

    bars = [_bar(i) for i in range(40)]

    def _fake_prefetch(_coin, _skip):
        return {
            "c1h": bars,
            "c4h": bars,
            "c1d": bars,
            "funding_raw": {"funding_rate": 0.0},
            "news": "",
            "signals_block": "none",
        }

    class _LLMBoom(RuntimeError):
        pass

    def _explode_debate(*_a, **_kw):
        raise _LLMBoom("debate gateway 500")

    import hermes_trader.client.hl_client as hl_client
    import hermes_trader.agents.perception as perception_mod

    monkeypatch.setattr(research, "_parallel_prefetch", _fake_prefetch)
    monkeypatch.setattr(research, "_debate_research", _explode_debate)
    monkeypatch.setattr(research, "_debate_cfg",
                        lambda: {"enabled": True, "parallel": False,
                                 "max_latency_s": 30, "use_structured_output": True})
    # The synthetic bars are not real market data (1h spacing judged as 4h,
    # frozen timestamps), so neutralise the C9 candle-quality gate — research()
    # lazy-imports both names from their defining modules, where monkeypatch
    # makes the patch visible.
    monkeypatch.setattr(hl_client, "assess_candle_quality",
                        lambda *a, **k: {"ok": True, "issues": []})
    monkeypatch.setattr(perception_mod, "_drop_forming_bar",
                        lambda candles, _tf: (candles, None))
    # Neutralise the heavy post-prefetch steps so the test reaches the debate
    # call without touching real indicators / account / prompts.
    monkeypatch.setattr(research, "_compute_indicators",
                        lambda _c: {"atr14": 1.0})
    monkeypatch.setattr(research, "_account_context",
                        lambda _snap: (10_000.0, 0.0, []))
    monkeypatch.setattr(research, "build_system_prompt", lambda *_a, **_kw: "sys")
    monkeypatch.setattr(research, "_build_user_message", lambda *_a, **_kw: "user")
    monkeypatch.setattr(research, "read_agent_config", lambda: {"mode": "OFF"})
    monkeypatch.setattr(research.memory, "get_win_rate",
                        lambda: {"rate": 0.0, "total": 0})

    with pytest.raises(_LLMBoom) as exc:
        research.research("BTC", {"coin": "BTC", "type": "perp"})

    assert getattr(exc.value, "research_stage", None) == "llm"
