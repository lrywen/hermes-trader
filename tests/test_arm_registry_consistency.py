"""Audit 2026-09-27 (P0): cross-registry arm consistency.

Ported in spirit from Pathiel's ``test_every_live_book_is_gradeable.py``:

    "A live money path the evidence loop cannot switch off is the one shape
    that script exists to rule out."

Hermes had FOUR independently-maintained arm registries that had drifted:

  * canonical config blocks carrying a ``mode`` key (the arms that can be
    off / shadow / enforce),
  * the read-only night rater  (``scripts/shadow_progress.ARMS``),
  * the dashboard backfill/evidence surface (``_BACKFILL_FILES``),
  * the startup gray-enforce guard (``_GRAY_ENFORCE_ARMS``).

A ``mode=enforce`` arm that the rater cannot see is a money path with no
evidence switch — the exact failure shape.

Naming rule (2026-09-27): every rater/evidence label is the CANONICAL config
block name. Legacy short file names (atr_regime_calib, trend_filter) live only
in ``shadow_progress.ARM_ALIASES``; nothing may resolve a pair ad hoc.

Read-only: no trade path, disk writes or network involved.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_script(fname: str):
    spec = importlib.util.spec_from_file_location(
        f"_under_test_{fname.replace('.', '_')}", REPO / "scripts" / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def progress():
    return _load_script("shadow_progress.py")


@pytest.fixture(scope="module")
def aliases(progress):
    return progress.ARM_ALIASES


@pytest.fixture(scope="module")
def canonical_arm_blocks():
    from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS
    return {
        key: blk
        for key, blk in CANONICAL_DEFAULTS.items()
        if isinstance(blk, dict) and "mode" in blk
    }


@pytest.fixture(scope="module")
def rater_labels(progress):
    return {row[0] for row in progress.ARMS}


@pytest.fixture(scope="module")
def backfill_surface():
    from hermes_trader.dashboard_routes.shadow_arms import _BACKFILL_FILES
    return {arm: fname for arm, fname in _BACKFILL_FILES}


def test_every_canonical_mode_arm_is_gradeable(canonical_arm_blocks, rater_labels):
    """Every block that can be mode=enforce must be visible to the INERT rater."""
    missing = sorted(b for b in canonical_arm_blocks if b not in rater_labels)
    assert not missing, (
        f"以下可 enforce 的臂评级器看不见（资金路径无证据开关）：{missing}；"
        "请加入 shadow_progress.ARMS")


def test_every_rater_arm_is_on_evidence_surface(rater_labels, backfill_surface):
    """Every rater arm must appear on the dashboard evidence surface."""
    missing = sorted(set(rater_labels) - set(backfill_surface))
    assert not missing, f"评级臂在证据面缺失：{missing}"


def test_startup_gray_enforce_arms_are_gradeable(rater_labels):
    """The startup guard's enforced arms must resolve to real rater arms."""
    from hermes_trader.agents.config_store import _GRAY_ENFORCE_ARMS
    for block, _surface in _GRAY_ENFORCE_ARMS:
        assert block in rater_labels, (
            f"启动守卫强制 enforce 的臂 {block} 评级器看不见")


def test_rater_rows_have_unique_labels_and_files(progress):
    """No two rater rows may share a canonical label or default file."""
    labels = [row[0] for row in progress.ARMS]
    assert len(labels) == len(set(labels)), "评级器存在重复臂"
    files = [row[3] for row in progress.ARMS]
    assert len(files) == len(set(files)), f"评级器默认文件冲突：{files}"


def test_aliases_resolve_to_real_arms_and_files(progress, canonical_arm_blocks,
                                                rater_labels):
    """Every alias pair must exist: canonical block + rater arm, and the rater's
    default file must actually use the legacy short name."""
    file_by_label = {row[0]: row[3] for row in progress.ARMS}
    for block, short in progress.ARM_ALIASES.items():
        assert block in canonical_arm_blocks, f"别名引用了不存在的配置块 {block}"
        assert block in rater_labels, f"别名长名 {block} 不在 ARMS"
        assert short not in rater_labels, (
            f"短名 {short} 同时是 ARMS label，别名歧义")
        assert short in file_by_label[block], (
            f"{block} 的默认文件 {file_by_label[block]} 未使用短名 {short}")


def test_enforce_arms_all_gradeable(canonical_arm_blocks, rater_labels):
    """Guard against the 2026-09-27 undercount: every mode=enforce block must
    have a gradeable rater arm so the report can never silently drop a gate."""
    enforced = [b for b, blk in canonical_arm_blocks.items()
                if str(blk.get("mode")) == "enforce"]
    ungradeable = sorted(set(enforced) - set(rater_labels))
    assert not ungradeable, f"enforce 臂不可评级（汇总漏报根因）：{ungradeable}"
