"""P0-6: CONFIG.md 顶层扁平键的 `default X` 必须等于 CANONICAL_DEFAULTS。

修复动机：docs/CONFIG.md 里多个 `default` 值与代码 canonical 默认长期漂移
（如 max_total_notional_pct 曾写 "10%" 实际是权益倍数、leverage 写 5 实际 10、
min_ai_confidence 写 0.7 实际 0.6 等）。这些过期值会让按文档配置的 operator
得到与代码不一致的默认行为，甚至放大敞口。本测试把「文档 default == 代码
canonical」固化为硬断言：任何未来改默认值或改文档造成漂移，当次即红。

只校验「顶层扁平标量键」——文档里用 `### key (type, default X)` 且该键是
CANONICAL_DEFAULTS 的直接标量子项（int/float）。嵌套块（如 dsl_exit.*）与
列表/布尔/off 语义不在本测试范围（其文档措辞另有约定，见 CONFIG.md 各节）。
"""
from __future__ import annotations

import re
from pathlib import Path

from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_MD = REPO_ROOT / "docs" / "CONFIG.md"

# `### key (type, default VALUE)` — 顶层扁平标量键。
_HEADER_PAT = re.compile(
    r"^###\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s+\(([^)]*default\s+([^)]+?))\)",
    re.MULTILINE,
)


def _iter_documented_scalar_defaults():
    text = CONFIG_MD.read_text(encoding="utf-8")
    for m in _HEADER_PAT.finditer(text):
        key, valstr = m.group(1), m.group(3).strip().strip("`")
        if key not in CANONICAL_DEFAULTS:
            continue  # 非顶层扁平键，跳过
        canon = CANONICAL_DEFAULTS[key]
        if not isinstance(canon, (int, float)) or isinstance(canon, bool):
            continue  # 只校验 int/float 标量
        try:
            parsed = int(valstr) if isinstance(canon, int) else float(valstr)
        except ValueError:
            continue  # 非数值措辞（如 "0 = off"、"unset"），本测试不覆盖
        yield key, parsed, canon


def test_every_documented_scalar_default_matches_canonical():
    mismatches = []
    checked = 0
    for key, doc_val, canon in _iter_documented_scalar_defaults():
        checked += 1
        if doc_val != canon:
            mismatches.append(f"{key}: doc={doc_val!r} canonical={canon!r}")
    assert checked > 0, "CONFIG.md 未解析到任何标量 default，测试可能失效"
    assert not mismatches, (
        "CONFIG.md 的 `default` 与 CANONICAL_DEFAULTS 漂移：\n"
        + "\n".join(f"  {x}" for x in mismatches)
        + "\n文档默认值必须等于代码 canonical 默认；如需表达生产 pin 值，"
        "请单独注明而不放在 `default` 位置。"
    )


def test_sensitive_equity_multiple_doc_not_legacy_percentage():
    """P0-6 回归：max_total_notional_pct 的文档措辞必须为权益倍数而非 % 分数。"""
    text = CONFIG_MD.read_text(encoding="utf-8")
    assert "multiple of aggregate equity" in text
    assert "the `_pct` suffix is historical and misleading" in text
    assert "1000%" in text  # 提醒 10.0 = 1000% equity 而非 10%


# ---------------------------------------------------------------------------
# recheck 2026-10-18 §5.2: 正文 "Production pins `X`" 必须等于 live 配置
# ---------------------------------------------------------------------------
import json as _json
import os as _os

# `Production pins `X`.` — 在正文句子末尾声明的生产部署值。
_PIN_PAT = re.compile(r"Production pins\s+`([^`]+)`")

_LIVE_CANDIDATES = (
    _os.environ.get("HERMES_LIVE_CONFIG_PATH"),
    "/home/ldy/hermes-deploy/.agent-config.json",
    "/data/.agent-config.json",
)


def _live_config() -> dict | None:
    for p in _LIVE_CANDIDATES:
        if p and Path(p).is_file():
            try:
                return _json.loads(Path(p).read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                return None
    return None


def test_production_pins_match_live_config():
    """§5.2: 文档正文的 "Production pins `X`" 是生产部署值的声明，必须与真实
    live 配置一致。此前 max_total_notional_pct 正文写 "pins 4.0" 而 live=2.0，
    默认值测试抓不到（它只查 `default X`）。本测试把正文 pin 声明也纳入硬断言，
    任何文档 pin 与 live 漂移当次即红。

    解析策略：pin 声明紧跟对应 `### key` 标题，取该标题的键与声明值比对。
    """
    live = _live_config()
    if live is None:
        return  # 无 live 配置时跳过（非本机/容器）
    text = CONFIG_MD.read_text(encoding="utf-8")
    mismatches = []
    checked = 0
    # 逐段扫描：每个 `### key (...)` 标题到下一个标题之间的正文。
    for m in _HEADER_PAT.finditer(text):
        key = m.group(1)
        seg_end = text.find("### ", m.end())
        seg = text[m.end(): seg_end if seg_end != -1 else len(text)]
        pin_m = _PIN_PAT.search(seg)
        if not pin_m:
            continue
        valstr = pin_m.group(1).strip()
        if key not in live:
            continue
        live_val = live[key]
        if isinstance(live_val, bool):
            continue
        try:
            parsed = int(valstr.replace("_", "")) if isinstance(live_val, int) \
                else float(valstr.replace("_", ""))
        except ValueError:
            continue
        checked += 1
        if parsed != live_val:
            mismatches.append(f"{key}: doc pins {valstr!r} vs live {live_val!r}")
    assert checked > 0, "CONFIG.md 未解析到任何 'Production pins' 声明，测试可能失效"
    assert not mismatches, (
        "CONFIG.md 的 'Production pins' 与 live 配置漂移：\n"
        + "\n".join(f"  {x}" for x in mismatches)
        + "\n文档 'Production pins' 必须等于生产部署值；若生产值变更，须同步文档。"
    )


# ---------------------------------------------------------------------------
# P1-14: README 顶层扁平键的 "Fallback/default" 列必须等于 CANONICAL_DEFAULTS
# ---------------------------------------------------------------------------

README_MD = REPO_ROOT / "README.md"

# markdown 表格行: `| key | ... | value |`
_ROW_PAT = re.compile(
    r"^\|\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\|(.*?)\|\s*`?([^`\n|]+)`?\s*\|$",
    re.MULTILINE,
)


def _readme_defaults_rows():
    """从 README 的默认值表（'| Key | What it does | Fallback/default |' 起
    到 '**Nested blocks**' 标题前）解析 `key -> 默认值字符串` 行。"""
    text = README_MD.read_text(encoding="utf-8")
    start = text.find("| Key | What it does | Fallback/default |")
    end = text.find("**Nested blocks**", start)
    assert start != -1, "README 默认值表头未找到"
    table = text[start: end if end != -1 else start + 6000]
    for m in _ROW_PAT.finditer(table):
        key, val = m.group(1), m.group(3).strip()
        if key in CANONICAL_DEFAULTS:
            yield key, val


def test_readme_defaults_table_matches_canonical():
    """P1-14：README 默认值表的 Fallback/default 必须等于 CANONICAL_DEFAULTS，
    防止与 CONFIG.md / 代码再次漂移误导运维。"""
    mismatches = []
    checked = 0
    for key, val in _readme_defaults_rows():
        canon = CANONICAL_DEFAULTS[key]
        if isinstance(canon, bool):
            if val.lower() not in ("true", "false"):
                continue
            parsed = val.lower() == "true"
        elif isinstance(canon, int):
            try:
                parsed = int(val.replace("_", ""))
            except ValueError:
                continue
        elif isinstance(canon, float):
            try:
                parsed = float(val.replace("_", ""))
            except ValueError:
                continue
        else:
            continue  # list / dict / 非标量语义，本测试不覆盖
        checked += 1
        if parsed != canon:
            mismatches.append(f"{key}: README={val!r} canonical={canon!r}")
    assert checked > 0, "README 默认值表未解析到任何可校验键，测试可能失效"
    assert not mismatches, (
        "README 默认值表与 CANONICAL_DEFAULTS 漂移：\n"
        + "\n".join(f"  {x}" for x in mismatches)
        + "\nREADME 的 Fallback/default 必须等于代码 canonical 默认；"
        "如需表达生产 pin 或示例值，请另作说明而不放在该列。"
    )
