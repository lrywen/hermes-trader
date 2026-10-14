"""P4-1 方法论指纹（methodology manifest hash）。

研究纪律借鉴自 Vibe-Trading：一条策略结论的可信度，依赖于产出它的
**方法论**——提示词模板、辩论 system prompt、操作 skill 说明、以及代码/依赖
版本。这些成分任一发生变化，历史评级与未来决策就不再严格可比，应当被重新
审视。本模块把这些"会改变研究方法的输入"归并成一个确定性的 SHA-256 指纹。

设计原则
--------
* **纯标准库、纯函数、无副作用**：同一份代码树永远得到同一个 hash。
* **缺失成分确定性标记，绝不崩溃**：容器内并未 COPY ``skills/``（见
  Dockerfile），此时 skill 指纹记为 ``"unavailable"`` 而不是抛异常——指纹仍
  可计算，但 ``components`` 里如实暴露这一事实，供操作员/CI 判别。
* **只指纹方法论文本与版本，不指纹运行时数据**：行情、持仓、reflections、
  win-rate 等动态上下文不参与（它们每个周期都变，纳入会让指纹失去意义）。
* **统计方法与门槛常量仅作信息字段**：``statistical_methods`` 被收集进
  manifest 供人阅读/审计，但按既定决策**不参与**失败判定（不进 hash），
  以免调一次门槛就让历史 mandate 全部失效。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

# 包根：hermes_trader/validation/ -> hermes_trader/ -> 仓库/安装根
_PKG_ROOT = Path(__file__).resolve().parents[2]

MANIFEST_VERSION = 1
UNAVAILABLE = "unavailable"


def _pkg_version() -> str:
    """hermes_trader.__version__，导入失败时确定性标记 unavailable。"""
    try:
        from hermes_trader import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - 极端环境才会触发
        return UNAVAILABLE


def _text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_components() -> dict[str, str]:
    """捕获会塑造研究/交易判断的提示词模板（静态文本，剥离动态上下文）。

    ``system_prompt.build_system_prompt`` 含 mode/track_record/reflections 等
    动态段，因此用固定参数 (OFF, 无战绩, 无反思) 实例化，只指纹其静态骨架。
    辩论三 system prompt 是模块级常量，直接取文本。
    """
    out: dict[str, str] = {}
    try:
        from hermes_trader.agents.system_prompt import build_system_prompt

        out["system_prompt"] = _text_fingerprint(
            build_system_prompt("OFF", 0.0, 0, None)
        )
    except Exception:
        out["system_prompt"] = UNAVAILABLE
    try:
        from hermes_trader.agents import research

        out["debate_bull_prompt"] = _text_fingerprint(getattr(research, "_BULL_SYS", ""))
        out["debate_bear_prompt"] = _text_fingerprint(getattr(research, "_BEAR_SYS", ""))
        out["debate_synth_prompt"] = _text_fingerprint(getattr(research, "_SYNTH_SYS", ""))
    except Exception:
        out.setdefault("debate_bull_prompt", UNAVAILABLE)
        out.setdefault("debate_bear_prompt", UNAVAILABLE)
        out.setdefault("debate_synth_prompt", UNAVAILABLE)
    return out


def _skill_components() -> dict[str, str]:
    """仓库内每个 ``skills/*/SKILL.md`` 的指纹。

    开发树位于 ``<root>/skills/*/SKILL.md``；容器内未 COPY 该目录，此时
    返回 ``{"skills": "unavailable"}`` 而非崩溃。
    """
    skills_dir = _PKG_ROOT / "skills"
    if not skills_dir.is_dir():
        return {"skills": UNAVAILABLE}
    found = dict(
        sorted(
            (p.parent.name, _text_fingerprint(p.read_text(encoding="utf-8")))
            for p in skills_dir.glob("*/SKILL.md")
            if p.is_file()
        )
    )
    return found or {"skills": UNAVAILABLE}


def _dependency_components() -> dict[str, Any]:
    """依赖指纹：pyproject.toml 的 [project].dependencies 区间 + 包版本。

    解析为 TOML（3.11+ 标准库）。优先读声明区间（决定"装哪个版本"），
    uv.lock 存在时附带其锁文件指纹（决定"实际装了什么"）。
    """
    comp: dict[str, Any] = {"package_version": _pkg_version()}

    pyproject = _PKG_ROOT / "pyproject.toml"
    deps: Any = UNAVAILABLE
    if pyproject.is_file():
        try:
            import tomllib

            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
            deps = sorted(data.get("project", {}).get("dependencies", []))
        except Exception:
            deps = UNAVAILABLE
    comp["pyproject_dependencies"] = deps

    lock = _PKG_ROOT / "uv.lock"
    comp["uv_lock"] = (
        _text_fingerprint(lock.read_text(encoding="utf-8")) if lock.is_file() else UNAVAILABLE
    )
    return comp


def _statistical_methods_info() -> dict[str, Any]:
    """信息性字段（不参与 hash）：研究/准入所用统计方法与门槛常量。

    仅作可读审计记录——调门槛不应使历史 mandate 批量失效，故这些值刻意
    不进入 :func:`methodology_fingerprint` 的归并输入。
    """
    info: dict[str, Any] = {}
    try:
        from hermes_trader.validation.microstructure_edge import (
            HARVEY_MIN_ABS_T,
            REVERSED_ABS_T,
        )

        info["microstructure"] = {
            "harvey_min_abs_t": HARVEY_MIN_ABS_T,
            "reversed_abs_t": REVERSED_ABS_T,
        }
    except Exception:
        pass
    try:
        from hermes_trader.agents.canary_pipeline import DEFAULT_GATES

        info["canary_gates"] = DEFAULT_GATES
    except Exception:
        pass
    return info


def collect_components() -> dict[str, Any]:
    """收集全部方法论成分（含信息性字段），结构稳定、可 JSON 序列化。"""
    return {
        "manifest_version": MANIFEST_VERSION,
        "prompts": _prompt_components(),
        "skills": _skill_components(),
        "dependencies": _dependency_components(),
        # 信息性：记录但不进 hash
        "statistical_methods": _statistical_methods_info(),
    }


def _hashable_components(components: dict[str, Any]) -> dict[str, Any]:
    """剔除信息性字段，只保留参与指纹的硬方法论成分。"""
    return {
        "manifest_version": components["manifest_version"],
        "prompts": components["prompts"],
        "skills": components["skills"],
        "dependencies": components["dependencies"],
    }


def methodology_fingerprint(components: Optional[dict[str, Any]] = None) -> str:
    """对硬方法论成分计算 canonical SHA-256（不含统计门槛信息字段）。"""
    components = components if components is not None else collect_components()
    blob = json.dumps(
        _hashable_components(components),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_manifest() -> dict[str, Any]:
    """完整 manifest：成分明细 + 指纹。供 gate/CLI/诊断直接落盘或展示。"""
    components = collect_components()
    return {
        "manifest_version": MANIFEST_VERSION,
        "methodology_sha256": methodology_fingerprint(components),
        "components": components,
    }
