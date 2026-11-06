"""Gate for scripts/supply_chain_check.py (ported from pathiel 2026-10-25,
adapted to uv.lock 2026-11-06).

Two halves. The real repo passes every offline check. And each check fails on
a planted copy of the exact mistake it exists for, so a check that silently
stopped looking (a renamed key, a regex that matches nothing) turns this file
red instead of staying green forever.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "supply_chain_check", ROOT / "scripts" / "supply_chain_check.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


@pytest.mark.parametrize("check", sc.OFFLINE_CHECKS, ids=lambda c: c.__name__)
def test_the_repo_passes(check):
    assert check() == []


# ── each check catches its own failure ──────────────────────────────────────

def test_workflow_mistakes_are_caught(tmp_path, monkeypatch):
    wf = tmp_path / "bad.yml"
    wf.write_text(
        "on: pull_request_target\n"
        "jobs:\n  t:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: npm ci\n"
    )
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "WORKFLOWS", tmp_path)
    found = "\n".join(sc.check_workflows())
    assert "not pinned to a commit SHA" in found
    assert "npm install without --ignore-scripts" in found
    assert "no top-level permissions block" in found
    assert "pull_request_target" in found


def test_a_write_permission_is_caught(tmp_path, monkeypatch):
    wf = tmp_path / "bad.yml"
    wf.write_text("permissions:\n  contents: write\n")
    monkeypatch.setattr(sc, "WORKFLOWS", tmp_path)
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    assert any("write permission" in p for p in sc.check_workflows())


def test_a_registry_token_in_npmrc_is_caught(tmp_path, monkeypatch):
    rc = tmp_path / ".npmrc"
    rc.write_text("//registry.npmjs.org/:_authToken=npm_x\n")
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    assert any("registry credential" in p for p in sc.check_no_registry_tokens())


def test_a_uv_pin_without_hashes_is_caught(tmp_path, monkeypatch):
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "requests"\nsource = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "httpx"\nsource = { registry = "https://pypi.org/simple" }\n'
        'sdist = { hash = "sha256:abc" }\n'
    )
    monkeypatch.setattr(sc, "PY_LOCK", lock)
    found = sc.check_uv_lock_hashes()
    assert any("requests" in p and "no sha256" in p for p in found)
    assert not any("httpx" in p for p in found)


def test_editable_project_without_hash_is_allowed(tmp_path, monkeypatch):
    # The project itself (source = { editable/virtual = ... }) carries no
    # registry hash and must NOT be flagged.
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "hermes-trader"\nsource = { editable = "." }\n'
    )
    monkeypatch.setattr(sc, "PY_LOCK", lock)
    assert sc.check_uv_lock_hashes() == []


def test_known_worm_file_names_match():
    for name in ("setup_bun.js", "bun_environment.js", "shai-hulud-workflow.yml",
                 "x/truffleSecrets.json", ".github/workflows/formatter_123.yaml"):
        assert sc.WORM_FILENAMES.search(name), name
    assert not sc.WORM_FILENAMES.search("scripts/build_plugin_skill.py")
