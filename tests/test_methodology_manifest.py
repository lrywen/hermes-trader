"""P4-1 methodology manifest fingerprint tests."""
from __future__ import annotations

from hermes_trader.validation import methodology_manifest as mm


def test_fingerprint_is_deterministic_sha256():
    fp1 = mm.methodology_fingerprint()
    fp2 = mm.methodology_fingerprint()
    assert fp1 == fp2 and len(fp1) == 64 and int(fp1, 16) >= 0


def test_components_capture_prompts_skills_deps():
    comp = mm.collect_components()
    assert comp["manifest_version"] == mm.MANIFEST_VERSION
    assert set(comp["prompts"]) == {
        "system_prompt", "debate_bull_prompt", "debate_bear_prompt",
        "debate_synth_prompt"}
    # In the dev tree a skill exists; value must be a hash or unavailable.
    assert isinstance(comp["skills"], dict) and comp["skills"]
    deps = comp["dependencies"]
    assert deps["package_version"]
    assert deps["package_version"] == "0.3.0" or deps["package_version"] == mm.UNAVAILABLE


def test_missing_component_marks_unavailable_instead_of_crashing(monkeypatch):
    # Force the package root to a nonexistent path: skills/deps degrade to
    # 'unavailable' but a fingerprint is still produced.
    from pathlib import Path

    monkeypatch.setattr(mm, "_PKG_ROOT", Path("/nonexistent/hermes-root"))
    comp = mm.collect_components()
    assert comp["skills"] == {"skills": mm.UNAVAILABLE}
    assert comp["dependencies"]["pyproject_dependencies"] == mm.UNAVAILABLE
    assert len(mm.methodology_fingerprint(comp)) == 64


def test_statistical_methods_excluded_from_hash():
    comp = mm.collect_components()
    assert "statistical_methods" in comp  # present for humans
    import json

    body = json.dumps(mm._hashable_components(comp), sort_keys=True)
    assert "statistical_methods" not in body
    # Mutating an informational gate constant must NOT move the fingerprint.
    comp2 = mm.collect_components()
    comp2["statistical_methods"] = {"tampered": True}
    assert mm.methodology_fingerprint(comp) == mm.methodology_fingerprint(comp2)


def test_prompt_change_moves_fingerprint(monkeypatch):
    base = mm.collect_components()
    changed = mm.collect_components()
    changed["prompts"]["system_prompt"] = "x" * 64
    assert mm.methodology_fingerprint(base) != mm.methodology_fingerprint(changed)


def test_build_manifest_embeds_fingerprint():
    m = mm.build_manifest()
    assert m["methodology_sha256"] == mm.methodology_fingerprint(m["components"])
