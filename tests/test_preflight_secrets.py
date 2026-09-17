"""P1-6 — deploy-secret preflight (scripts/preflight_secrets.py).

Replaces the out-of-tree, untested /home/ldy/hermes-deploy/check_keys.py.
Contract pinned here:

  * the spec table lives in the repo as data;
  * empty / self-referential / ``$VAR`` / templated placeholders are
    recognised rather than regex-checked;
  * secret VALUES ARE NEVER ECHOED — output carries key names + status
    only (the old script printed the first 20 chars of the private key);
  * a missing required item exits 1; optional items only warn.
"""
from __future__ import annotations

import pytest

from scripts import preflight_secrets as ps

WALLET = "0x" + "a" * 40
PRIV = "f" * 64
OPENROUTER = "sk-or-v1-" + "z" * 40
OPENROUTER_CURRENT = "sk-" + "0a1b" * 12  # deployed shape: sk- + 48 chars
BRAVE = "BSA" + "9" * 30
MASTER = "0x" + "b" * 40


def _write_env(path, entries):
    lines = [f'{k}="{v}"' for k, v in entries.items()]
    path.write_text("# header comment\n" + "\n".join(lines) + "\n")


def _good_env(tmp_path, extra=None):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    entries = {
        "OPENROUTER_API_KEY": OPENROUTER,
        "HYPERLIQUID_WALLET_ADDRESS": WALLET,
        "HYPERLIQUID_PRIVATE_KEY": PRIV,
    }
    if extra:
        entries.update(extra)
    _write_env(deploy / ".env.local", entries)
    return deploy


# ---------------------------------------------------------------------------
# Spec table: required set for the LIVE 10x deployment (regression guard)
# ---------------------------------------------------------------------------

def test_spec_table_pins_live_required_keys() -> None:
    required = {(s.env_file, s.key) for s in ps.SPECS if s.required}
    assert required == {
        (".env.local", "OPENROUTER_API_KEY"),
        (".env.local", "HYPERLIQUID_WALLET_ADDRESS"),
        (".env.local", "HYPERLIQUID_PRIVATE_KEY"),
    }


def test_spec_table_has_no_dead_deepseek_entry() -> None:
    # .env.hermes is a broken symlink and no runtime code reads
    # DEEPSEEK_API_KEY anymore (OpenRouter is the live path).
    assert all(s.key != "DEEPSEEK_API_KEY" for s in ps.SPECS)


def test_spec_patterns_compile() -> None:
    import re

    for s in ps.SPECS:
        re.compile(s.pattern)


# ---------------------------------------------------------------------------
# parse_env
# ---------------------------------------------------------------------------

def test_parse_env_handles_quotes_comments_export_and_inline_equals(tmp_path) -> None:
    f = tmp_path / ".env.local"
    f.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=abc\n"
        'SINGLE=\'xyz\'\n'
        'DOUBLE="q"\n'
        "export EXPORTED=e\n"
        "INLINE=a=b=c\n"
        "  SPACED  =  v  \n"
    )
    env = ps.parse_env(f)
    assert env == {
        "PLAIN": "abc",
        "SINGLE": "xyz",
        "DOUBLE": "q",
        "EXPORTED": "e",
        "INLINE": "a=b=c",
        "SPACED": "v",
    }


def test_parse_env_missing_file_returns_empty(tmp_path) -> None:
    assert ps.parse_env(tmp_path / "nope") == {}


# ---------------------------------------------------------------------------
# Placeholder recognition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "HYPERLIQUID_PRIVATE_KEY",  # value == key name
        "$HYPERLIQUID_PRIVATE_KEY",
        "${HYPERLIQUID_PRIVATE_KEY}",
        "<your-private-key>",
        "sk-or-...",
        "0x...",
        "CHANGEME",
        "changeme123",
        "your-key-here",
        "REPLACE_ME",
        "xxxx",
        "TODO",
        "fill_in_now",
    ],
)
def test_placeholder_recognition(value: str) -> None:
    assert ps.is_placeholder("HYPERLIQUID_PRIVATE_KEY", value)


@pytest.mark.parametrize(
    "value",
    [PRIV, OPENROUTER, OPENROUTER_CURRENT, WALLET, BRAVE, "sk-or-v1-abcd-1234", "BSAabcDEF"],
)
def test_real_shaped_values_are_not_placeholders(value: str) -> None:
    assert not ps.is_placeholder("WHATEVER_KEY", value)


def test_openrouter_pattern_accepts_legacy_and_current_key_shapes() -> None:
    import re

    pat = next(s.pattern for s in ps.SPECS if s.key == "OPENROUTER_API_KEY")
    assert re.match(pat, OPENROUTER)
    assert re.match(pat, OPENROUTER_CURRENT)
    assert not re.match(pat, "sk-short")
    assert not re.match(pat, "sk-or-...")


# ---------------------------------------------------------------------------
# evaluate + exit code
# ---------------------------------------------------------------------------

def _status_map(deploy_dir):
    files, results = ps.evaluate(deploy_dir)
    return {r.spec.key: r.status for r in results}, files


def test_all_required_present_passes(tmp_path) -> None:
    deploy = _good_env(tmp_path, {"BRAVE_API_KEY": BRAVE, "HYPERLIQUID_MASTER_ADDRESS": MASTER})
    statuses, files = _status_map(deploy)
    assert files[".env.local"].exists is True
    assert all(v == "ok" for v in statuses.values()), statuses
    assert ps.exit_code(results=_results(deploy)) == 0


def test_missing_required_key_fails(tmp_path) -> None:
    deploy = _good_env(tmp_path)
    (deploy / ".env.local").write_text(f'OPENROUTER_API_KEY="{OPENROUTER}"\n')
    statuses, _ = _status_map(deploy)
    assert statuses["HYPERLIQUID_PRIVATE_KEY"] == "missing"
    assert statuses["HYPERLIQUID_WALLET_ADDRESS"] == "missing"
    assert ps.exit_code(results=_results(deploy)) == 1


def test_missing_env_file_fails_required(tmp_path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    statuses, files = _status_map(deploy)
    assert files[".env.local"].exists is False
    assert statuses["HYPERLIQUID_PRIVATE_KEY"] == "missing"
    assert ps.exit_code(results=_results(deploy)) == 1


@pytest.mark.parametrize(
    "placeholder",
    ["", "HYPERLIQUID_PRIVATE_KEY", "$HYPERLIQUID_PRIVATE_KEY", "xxxx", "<fill-me>"],
)
def test_placeholder_required_fails(tmp_path, placeholder: str) -> None:
    deploy = _good_env(tmp_path, {"HYPERLIQUID_PRIVATE_KEY": placeholder})
    statuses, _ = _status_map(deploy)
    assert statuses["HYPERLIQUID_PRIVATE_KEY"] == "placeholder"
    assert ps.exit_code(results=_results(deploy)) == 1


def test_bad_format_required_fails(tmp_path) -> None:
    deploy = _good_env(tmp_path, {"HYPERLIQUID_PRIVATE_KEY": "too-short"})
    statuses, _ = _status_map(deploy)
    assert statuses["HYPERLIQUID_PRIVATE_KEY"] == "bad_format"
    assert ps.exit_code(results=_results(deploy)) == 1


def test_optional_missing_or_bad_still_passes(tmp_path) -> None:
    deploy = _good_env(tmp_path, {"BRAVE_API_KEY": "not-a-brave-key"})
    statuses, _ = _status_map(deploy)
    assert statuses["BRAVE_API_KEY"] == "bad_format"
    assert statuses["HYPERLIQUID_MASTER_ADDRESS"] == "missing"
    assert ps.exit_code(results=_results(deploy)) == 0


def test_private_key_with_0x_prefix_is_bad_format(tmp_path) -> None:
    # check_keys.py demanded 64 bare hex chars; keep that contract.
    deploy = _good_env(tmp_path, {"HYPERLIQUID_PRIVATE_KEY": "0x" + "c" * 64})
    statuses, _ = _status_map(deploy)
    assert statuses["HYPERLIQUID_PRIVATE_KEY"] == "bad_format"


# ---------------------------------------------------------------------------
# Zero secret echo — the whole reason this script exists in-repo
# ---------------------------------------------------------------------------

def test_output_never_contains_secret_values(tmp_path, capsys) -> None:
    deploy = _good_env(tmp_path, {"BRAVE_API_KEY": BRAVE})
    rc = ps.main(["--deploy-dir", str(deploy)])
    out = capsys.readouterr().out
    assert rc == 0
    for secret in (PRIV, OPENROUTER, WALLET, BRAVE):
        assert secret not in out
        assert secret[:20] not in out  # old script leaked first 20 chars


def test_output_reports_key_names_and_status(tmp_path, capsys) -> None:
    deploy = _good_env(tmp_path)
    ps.main(["--deploy-dir", str(deploy)])
    out = capsys.readouterr().out
    assert "HYPERLIQUID_PRIVATE_KEY" in out
    assert "ok" in out.lower()


def test_failure_output_does_not_leak_bad_value(tmp_path, capsys) -> None:
    secretish = "deadbeef" * 8  # 64 hex but flip to a bad-format variant
    deploy = _good_env(tmp_path, {"HYPERLIQUID_PRIVATE_KEY": secretish + "OOPS"})
    rc = ps.main(["--deploy-dir", str(deploy)])
    out = capsys.readouterr().out
    assert rc == 1
    assert secretish not in out


def _results(deploy_dir):
    return ps.evaluate(deploy_dir)[1]
