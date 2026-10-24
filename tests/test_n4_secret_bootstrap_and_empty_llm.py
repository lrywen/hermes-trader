"""Regression tests for the N4 fixes (2026-10-24 incident).

Covers four gaps exposed when the LLM subscription was recovered and the model
moved to deepseek-v4.1-flash:

* N4-e: ``scripts/trading_loop.py`` must load ``.env.local`` with ``setdefault``
  (never clobber real container env) and must call ``bootstrap_env()`` so
  ``.secrets.json`` is visible to the trading process, not only the server.
* N4-f: a 200 response whose content is EMPTY because reasoning spent the whole
  token budget (``finish_reason="length"``, ``answer_tokens=0``) must be
  classified as a failure (``length_empty``), not counted as outcome="ok".
* N4-a: switching/editing the default LLM profile hot-applies it to the current
  process and warns that the trading loop needs a container recreate.
* N4-c: startup warns when the LLM key differs across env / .env.local /
  .secrets.json, without ever printing the secret.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hermes_trader.agents import research as R
from hermes_trader.agents import secret_store as SS

ROOT = Path(__file__).resolve().parent.parent


# ── N4-e: trading_loop env handling (source contract) ──────────────────────

def test_n4e_trading_loop_uses_setdefault_and_bootstraps():
    src = (ROOT / "scripts" / "trading_loop.py").read_text(encoding="utf-8")
    # .env.local must no longer unconditionally clobber os.environ.
    assert "os.environ.setdefault(key.strip(), val.strip())" in src
    assert not re.search(r"os\.environ\[key\.strip\(\)\]\s*=\s*val\.strip\(\)", src)
    # The trading process must bootstrap .secrets.json and run divergence check.
    assert "bootstrap_env as _bootstrap_secrets" in src
    assert "_bootstrap_secrets()" in src
    assert "warn_on_secret_source_divergence" in src


# ── N4-f: empty-body classification ────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.is_success = 200 <= status_code < 300
        self.headers = {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def post(self, *a, **k):
        return self._resp


def _payload(content, finish_reason, reasoning_tokens):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content},
            }
        ],
        "model": "deepseek-test",
        "usage": {
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


@pytest.fixture
def _llm_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    # Keep breaker/retry side effects out of the way.
    monkeypatch.setattr(R, "_llm_circuit_open", lambda: False)
    monkeypatch.setattr(R, "_llm_record_failure", lambda: None)
    monkeypatch.setattr(R, "_llm_record_success", lambda: None)
    outcomes = []
    monkeypatch.setattr(
        R, "_llm_metric_outcome",
        lambda path, outcome, started: outcomes.append((path, outcome)),
    )
    return outcomes


def test_n4f_length_with_empty_content_is_classified_failure(monkeypatch, _llm_env, caplog):
    outcomes = _llm_env
    resp = _FakeResp(_payload("", "length", reasoning_tokens=800))
    monkeypatch.setattr(R, "_http", lambda: _FakeClient(resp))

    with caplog.at_level("ERROR", logger="hermes_trader.agents.research"):
        out = R._call_openrouter("sys", "user", max_tokens=800, path="debate_direct")

    assert out == ""
    assert ("debate_direct", "length_empty") in outcomes
    assert ("debate_direct", "ok") not in outcomes
    assert any("finish_reason=length" in r.message and "reasoning_tokens=800" in r.message
               for r in caplog.records)


def test_n4f_empty_content_other_finish_is_classified_empty(monkeypatch, _llm_env, caplog):
    outcomes = _llm_env
    resp = _FakeResp(_payload("", "stop", reasoning_tokens=10))
    monkeypatch.setattr(R, "_http", lambda: _FakeClient(resp))

    with caplog.at_level("ERROR", logger="hermes_trader.agents.research"):
        out = R._call_openrouter("sys", "user", max_tokens=800)

    assert out == ""
    assert ("call_ai", "empty") in outcomes
    assert ("call_ai", "ok") not in outcomes


def test_n4f_nonempty_content_still_succeeds(monkeypatch, _llm_env):
    outcomes = _llm_env
    resp = _FakeResp(_payload("VALID", "stop", reasoning_tokens=9))
    monkeypatch.setattr(R, "_http", lambda: _FakeClient(resp))

    out = R._call_openrouter("sys", "user", max_tokens=800)

    assert out == "VALID"
    assert ("call_ai", "ok") in outcomes


# ── N4-a: hot-apply default profile ────────────────────────────────────────

def _write_secrets(tmp_path, doc):
    p = tmp_path / ".secrets.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _secrets_doc(key="sk-new", model="m-new", base="https://x/v1", pid="p1"):
    return {
        "version": 1,
        "llm": {
            "default_profile_id": pid,
            "profiles": {
                pid: {
                    "id": pid, "name": "n", "api_key": key, "model": model,
                    "base_url": base, "temperature": 0.1, "max_tokens": 500,
                    "timeout_sec": 30.0, "enabled": True,
                }
            },
        },
        "exchange": {"testnet": False, "wallet_address": "",
                     "master_address": "", "private_key": ""},
        "feishu": {},
    }


def test_n4a_set_default_hot_applies_and_warns(monkeypatch, tmp_path, caplog):
    import os

    sp = _write_secrets(tmp_path, _secrets_doc())
    monkeypatch.setattr(SS, "SECRETS_PATH", str(sp))
    # live_apply writes os.environ directly (not via monkeypatch), so snapshot
    # and restore by hand to guarantee no leakage into later tests.
    saved = {k: os.environ.get(k) for k in
             ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_BASE_URL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        with caplog.at_level("WARNING", logger="hermes-secrets"):
            errs = SS.set_default_llm_profile("p1")

        assert errs == []
        # set_default already hot-applied once; applying again is idempotent.
        assert SS.live_apply_default_llm_profile() is True
        assert os.environ["OPENROUTER_API_KEY"] == "sk-new"
        assert os.environ["OPENROUTER_MODEL"] == "m-new"
        assert any("container is recreated" in r.getMessage() for r in caplog.records)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── N4-c: source divergence warning ────────────────────────────────────────

def test_n4c_divergence_warns_when_sources_differ(monkeypatch, tmp_path, caplog):
    # .secrets.json has key A
    sp = _write_secrets(tmp_path, _secrets_doc(key="sk-SECRET"))
    monkeypatch.setattr(SS, "SECRETS_PATH", str(sp))
    # process env has key B (simulates .env.local injecting a different value)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-ENV-DIFFERENT")
    # Point the .env.local reader at a file with key C
    env_local = tmp_path / ".env.local"
    env_local.write_text("OPENROUTER_API_KEY=sk-FILE-DIFFERENT\n", encoding="utf-8")
    monkeypatch.setattr(SS, "_PROJECT_ROOT", str(tmp_path))

    with caplog.at_level("WARNING", logger="hermes-secrets"):
        fps = SS.warn_on_secret_source_divergence()

    assert set(fps) == {"env", "file", "secrets"}
    assert len(set(fps.values())) == 3
    # never leak the raw secret into the log
    assert not any("sk-SECRET" in r.getMessage()
                   or "sk-ENV-DIFFERENT" in r.getMessage()
                   or "sk-FILE-DIFFERENT" in r.getMessage()
                   for r in caplog.records)
    assert any("differs across configured sources" in r.getMessage()
               for r in caplog.records)


def test_n4c_no_warning_when_sources_agree(monkeypatch, tmp_path, caplog):
    sp = _write_secrets(tmp_path, _secrets_doc(key="sk-SAME"))
    monkeypatch.setattr(SS, "SECRETS_PATH", str(sp))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-SAME")
    env_local = tmp_path / ".env.local"
    env_local.write_text("OPENROUTER_API_KEY=sk-SAME\n", encoding="utf-8")
    monkeypatch.setattr(SS, "_PROJECT_ROOT", str(tmp_path))

    with caplog.at_level("WARNING", logger="hermes-secrets"):
        fps = SS.warn_on_secret_source_divergence()

    assert len(set(fps.values())) == 1
    assert not any("differs across configured sources" in r.getMessage()
                   for r in caplog.records)
