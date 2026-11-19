from __future__ import annotations

import json
import os
import subprocess

import pytest

from hermes_trader import solo


def test_invalid_job_id_cannot_escape_job_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SOLO_JOB_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="invalid backtest job id"):
        solo._job_paths("../../data")


def test_background_job_is_created_and_returns_job_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SOLO_JOB_DIR", str(tmp_path))

    class _Worker:
        pid = 43210

    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _Worker()

    monkeypatch.setattr(solo.subprocess, "Popen", fake_popen)
    args = solo.build_parser().parse_args(
        ["backtest", "--days", "3", "--coins", "BTC,ETH", "--background"]
    )
    assert solo._start_background_backtest(args, "/tmp/runtime.json") == 0

    output = capsys.readouterr().out
    job_id = next(line.split("=", 1)[1] for line in output.splitlines() if line.startswith("job="))
    meta = solo._read_job_meta(job_id)
    assert meta["status"] == "queued"
    assert meta["pid"] == 0  # the worker records its real PID as its first action
    assert meta["parameters"] == {
        "days": 3,
        "coins": "BTC,ETH",
        "coins_count": 10,
        "interval": "1h",
    }
    command, kwargs = calls[0]
    assert "--job-id" in command
    assert command[command.index("--job-id") + 1] == job_id
    assert command[-2:] == ["--coins", "BTC,ETH"]
    assert kwargs["env"]["HERMES_AGENT_CONFIG_FILE"] == "/tmp/runtime.json"
    assert kwargs["env"]["HERMES_BACKTEST"] == "1"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_background_worker_records_exit_code_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SOLO_JOB_DIR", str(tmp_path))
    job_id = "bt-20261119T120000Z-0123456789"
    solo._write_job_meta(job_id, {"version": 1, "job_id": job_id, "status": "queued", "pid": 0})
    args = solo.build_parser().parse_args(["backtest", "--days", "3", "--coins", "BTC"])

    class _Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        assert command[:2] == [
            solo.sys.executable,
            str(solo._REPO_ROOT / "scripts" / "backtest_quick.py"),
        ]
        kwargs["stdout"].write(b"trades        : 2\n")
        return _Completed()

    monkeypatch.setattr(solo.subprocess, "run", fake_run)
    assert solo._run_background_worker(job_id, args, "/tmp/runtime.json") == 0
    meta = solo._read_job_meta(job_id)
    assert meta["status"] == "completed"
    assert meta["exit_code"] == 0
    assert meta["finished_at"].endswith("Z")
    assert "trades" in solo._job_paths(job_id)[1].read_text()


def test_result_reads_completed_job_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SOLO_JOB_DIR", str(tmp_path))
    job_id = "bt-20261119T120000Z-abcdef1234"
    solo._write_job_meta(
        job_id,
        {
            "version": 1,
            "job_id": job_id,
            "status": "completed",
            "pid": 99999,
            "exit_code": 0,
            "parameters": {"days": 3},
        },
    )
    solo._job_paths(job_id)[1].write_text(
        "=== SUMMARY ===\ntrades        : 2\n",
        encoding="utf-8",
    )
    assert solo.main(["backtest", "--result", job_id, "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["status"] == "completed"
    assert view["exit_code"] == 0
    assert view["result"].startswith("=== SUMMARY ===")
    assert view["meta_path"].endswith(os.path.join(job_id, "job.json"))


def test_result_rejects_unknown_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SOLO_JOB_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="backtest job not found"):
        solo.main(["backtest", "--result", "bt-20261119T120000Z-abcdef1234"])


def test_foreground_backtest_remains_compatible(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_AGENT_CONFIG_FILE", str(config_path))
    seen = {}

    class _Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return _Completed()

    monkeypatch.setattr(solo.subprocess, "run", fake_run)
    assert solo.main(["backtest", "--days", "3", "--coins", "BTC"]) == 0
    assert seen["command"][-2:] == ["--coins", "BTC"]
    assert seen["env"]["HERMES_BACKTEST"] == "1"
    assert seen["env"]["HERMES_AGENT_CONFIG_FILE"] == str(config_path)
