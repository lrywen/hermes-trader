from __future__ import annotations

import logging

from hermes_trader.agents import config_store


def test_schema_warning_is_deduplicated_per_config_version(tmp_path, monkeypatch, caplog):
    path = tmp_path / ".agent-config.json"
    path.write_text('{"leverage": "bad"}', encoding="utf-8")
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(path))
    config_store._invalidate_raw_cache()

    with caplog.at_level(logging.WARNING, logger=config_store.logger.name):
        first = config_store._log_validation_warnings(
            {"leverage": "bad"}, source="unit_test", strict_keys=False
        )
        second = config_store._log_validation_warnings(
            {"leverage": "bad"}, source="unit_test", strict_keys=False
        )

    assert first
    assert second == first
    messages = [
        rec.message for rec in caplog.records
        if "schema warning" in rec.message
    ]
    assert len(messages) == 1


def test_new_config_version_warns_again(tmp_path, monkeypatch, caplog):
    path = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(path))

    with caplog.at_level(logging.WARNING, logger=config_store.logger.name):
        path.write_text('{"leverage": "bad"}', encoding="utf-8")
        config_store._invalidate_raw_cache()
        config_store._log_validation_warnings(
            {"leverage": "bad"}, source="unit_test", strict_keys=False
        )

        path.write_text('{"leverage": "still-bad"}', encoding="utf-8")
        config_store._invalidate_raw_cache()
        config_store._log_validation_warnings(
            {"leverage": "still-bad"}, source="unit_test", strict_keys=False
        )

    messages = [
        rec.message for rec in caplog.records
        if "schema warning" in rec.message
    ]
    assert len(messages) == 2