"""CS-A (2026-09-09): config_write 强化审计。

write_agent_config 落 config_write 审计：
* backup=True 时带 old/new/era_id，且只含发生变化的顶层键；
* backup=False 时 old/new/era 均为 None，新值 era 仍可计算。

era 指纹本身的测试见 tests/test_cs_e_config_era.py（随 CS-E 落地）。
"""
import json

from hermes_trader import session_log
from hermes_trader.agents import config_store as cs
from hermes_trader.agents.config_store import write_agent_config


def _isolate_config(monkeypatch, tmp_path):
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(cs, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(cs, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(cs, "_BACKUP_PATH", str(cfg_file) + ".bak")


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_config_write_audit_carries_old_new_and_era(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    sess_file = tmp_path / "session.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess_file))

    write_agent_config({"mode": "PAPER", "leverage": 10}, backup=False, via="test")
    write_agent_config({"mode": "PAPER", "leverage": 20}, backup=True, via="test")

    rows = _read_jsonl(sess_file)
    writes = [r for r in rows if r.get("event") == "config_write"]
    assert len(writes) == 2
    second = writes[-1]
    # 审计字段全部在场；old/new 只含发生变化的顶层键。
    assert second["changed_keys"] == ["leverage"]
    assert second["old"] == {"leverage": 10}
    assert second["new"] == {"leverage": 20}
    assert second["prev_era_id"] != second["era_id"]
    assert len(second["era_id"]) == 12
    # 无关键改动（mode 未变）不得进入 old/new。
    assert "mode" not in second["old"]


def test_config_write_audit_nulls_when_backup_disabled(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    sess_file = tmp_path / "session.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess_file))

    write_agent_config({"mode": "PAPER", "leverage": 33}, backup=False, via="test")
    row = _read_jsonl(sess_file)[0]
    assert row["event"] == "config_write"
    assert row["backup"] is False
    assert row["changed_keys"] is None
    assert row["old"] is None and row["new"] is None
    assert row["prev_era_id"] is None
    # 新值 era 仍然可计算（不依赖 prior）。
    assert len(row["era_id"]) == 12


def test_deep_merge_does_not_alias_base_nested_objects():
    # N4 root cause: a shallow dict(base) copy shared nested objects, so an
    # in-place mutation of a merged CANONICAL_DEFAULTS view wrote back into the
    # global defaults. A deep merge must never alias a mutable node of base.
    from hermes_trader.agents.config_store import _deep_merge

    base = {"sec": {"nested": {"m": 0}, "x": 1}}
    merged = _deep_merge(base, {})
    merged["sec"]["nested"]["m"] = 99
    assert base["sec"]["nested"]["m"] == 0
    # overlay scalars/dicts are also copied (no alias back into overlay).
    ov = {"sec": {"nested": {"m": 7}}}
    m2 = _deep_merge(base, ov)
    m2["sec"]["nested"]["m"] = 700
    assert ov["sec"]["nested"]["m"] == 7


def test_rmw_nested_default_change_is_visible_and_does_not_poison_defaults(
        monkeypatch, tmp_path):
    # N4 end-to-end: flipping a nested key that exists ONLY in CANONICAL_DEFAULTS
    # (absent from the raw file) must (a) appear in changed_keys/old/new and
    # (b) leave CANONICAL_DEFAULTS untouched. Pre-fix the shallow merge mutated
    # the module defaults, so the prior-side effective view also read True and
    # the audit logged changed_keys == [].
    from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS
    from hermes_trader.agents.config_store import update_agent_config

    _isolate_config(monkeypatch, tmp_path)
    sess_file = tmp_path / "session.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess_file))
    # Seed a sparse raw file: launch_capture present but WITHOUT spread_observe,
    # so the arm default comes purely from CANONICAL_DEFAULTS.
    write_agent_config({"mode": "SHADOW", "launch_capture": {}},
                       backup=False, via="seed")

    canon_before = json.loads(json.dumps(
        CANONICAL_DEFAULTS["launch_capture"]["spread_observe"]))
    with update_agent_config(backup=True, via="n4_regression") as cfg:
        cfg["launch_capture"]["spread_observe"]["enabled"] = True

    assert CANONICAL_DEFAULTS["launch_capture"]["spread_observe"] == canon_before
    assert CANONICAL_DEFAULTS["launch_capture"]["spread_observe"]["enabled"] is False

    writes = [r for r in _read_jsonl(sess_file)
              if r.get("event") == "config_write" and r.get("via") == "n4_regression"]
    assert len(writes) == 1
    ev = writes[0]
    assert "launch_capture" in ev["changed_keys"]
    assert ev["old"]["launch_capture"]["spread_observe"] == {"enabled": False}
    assert ev["new"]["launch_capture"]["spread_observe"] == {"enabled": True}
