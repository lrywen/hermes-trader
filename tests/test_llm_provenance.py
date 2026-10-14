"""P0-3 LLM 训练截止日污染标注单测。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from hermes_trader.validation.llm_provenance import (
    CLEAN,
    CONTAMINATED,
    UNKNOWN,
    annotate_contamination,
    parse_utc_date,
)


def test_clean_when_window_after_cutoff():
    p = annotate_contamination(
        model_id="m",
        window_start=date(2026, 6, 1),
        window_end=date(2026, 7, 1),
        training_cutoff=date(2026, 3, 1),
    )
    assert p.contamination == CLEAN
    assert p.training_cutoff == "2026-03-01"
    assert p.to_dict()["model_id"] == "m"


def test_contaminated_when_window_fully_within_training():
    p = annotate_contamination(
        model_id="m",
        window_start=date(2025, 1, 1),
        window_end=date(2025, 2, 1),
        training_cutoff=date(2026, 3, 1),
    )
    assert p.contamination == CONTAMINATED
    assert "fully within" in p.reason


def test_contaminated_on_partial_overlap_and_boundary():
    # 窗口起点 <= cutoff < 窗口终点 → 部分重叠，保守计入污染。
    p = annotate_contamination(
        model_id="m",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 6, 1),
        training_cutoff=date(2026, 3, 1),
    )
    assert p.contamination == CONTAMINATED
    assert "partially overlaps" in p.reason
    # 边界：window_start == cutoff 也算污染（cutoff 当日可能已见）。
    p2 = annotate_contamination(
        model_id="m",
        window_start=date(2026, 3, 1),
        window_end=date(2026, 4, 1),
        training_cutoff=date(2026, 3, 1),
    )
    assert p2.contamination == CONTAMINATED


def test_unknown_when_cutoff_missing_is_fail_closed():
    # 模型不在注册表且未显式传 cutoff → unknown，绝不默认 clean。
    p = annotate_contamination(
        model_id="deepseek-v4-flash",
        window_start=date(2026, 6, 1),
        window_end=date(2026, 7, 1),
    )
    assert p.contamination == UNKNOWN
    assert p.training_cutoff is None
    assert "fail-closed" in p.reason

    # 空 model_id 同样 unknown
    p2 = annotate_contamination(
        model_id=None,
        window_start=date(2026, 6, 1),
        window_end=date(2026, 7, 1),
    )
    assert p2.contamination == UNKNOWN


def test_explicit_cutoff_overrides_registry():
    p = annotate_contamination(
        model_id="anything",
        window_start=date(2027, 1, 1),
        window_end=date(2027, 2, 1),
        training_cutoff=date(2026, 12, 31),
    )
    assert p.contamination == CLEAN


def test_rejects_inverted_window():
    with pytest.raises(ValueError):
        annotate_contamination(
            model_id="m",
            window_start=date(2026, 7, 1),
            window_end=date(2026, 6, 1),
            training_cutoff=date(2026, 1, 1),
        )


def test_parse_utc_date_variants():
    assert parse_utc_date("2026-03-01") == date(2026, 3, 1)
    assert parse_utc_date("2026-03-01T12:00:00Z") == date(2026, 3, 1)
    assert parse_utc_date(date(2026, 3, 1)) == date(2026, 3, 1)
    # 毫秒 epoch：2026-03-01T00:00:00Z ≈ 1772323200000（只验可解析为 date）
    d = parse_utc_date(datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert d == date(2026, 3, 1)
    with pytest.raises(ValueError):
        parse_utc_date("not-a-date")
