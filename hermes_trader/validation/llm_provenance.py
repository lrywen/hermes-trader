"""LLM 训练截止日（knowledge cutoff）污染标注——纯函数，标准库，无 I/O。

当 LLM 在环（in-the-loop）参与信号/研究决策时，回测或影子窗口若落在该模型的
**训练数据覆盖区间内**，模型可能"记得"那段行情（regime、事件、甚至具体走势），
于是所谓的样本外表现可能是记忆而非泛化。这与行情数据的前视偏差（PIT/no-lookahead）
是两类不同污染，必须分别标注。

本模块只回答一个可审计的问题：**这次运行使用的模型，其训练截止日是否覆盖了
评估窗口？** 并给出三态结论：

- ``clean``       —— 评估窗口严格晚于模型训练截止日（真正未见过的数据）；
- ``contaminated``——评估窗口与训练覆盖区间有重叠（含边界，按保守计）；
- ``unknown``     ——模型 cutoff 缺失/未知，**fail-closed**：绝不默认 clean。

设计原则（对齐 Vibe-Trading ``model_provenance``）：

1. cutoff 是"按模型记录的事实"，集中在 :data:`MODEL_TRAINING_CUTOFFS`；表里没有
   的模型不允许被当成干净，调用方应显式传入 ``training_cutoff`` 覆盖或接受 unknown。
2. 只比较日历日期（UTC），不依赖 wall clock，可对历史运行复算。
3. 返回结构化标注（:class:`LLMProvenance`），可直接并入研究卡片 / 影子结果 JSON，
   不做任何策略结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

# 模型训练截止日（UTC 日历日）。来源：各模型官方公布的 knowledge cutoff。
# 只登记已确认的值；不确定的模型不要放进来——缺失即走 unknown（fail-closed）。
# 如需新增/修正，连同来源在提交说明里记录。
MODEL_TRAINING_CUTOFFS: dict[str, date] = {
    # OpenRouter 上当前默认模型（research.py）。占位待官方确认；在确认前显式登记
    # 为 None 语义由"不在表中"承担，故此处不放入，避免给一个假的精确日期。
}

CLEAN = "clean"
CONTAMINATED = "contaminated"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class LLMProvenance:
    """一次 LLM 在环运行的训练截止污染标注。"""

    model_id: str
    training_cutoff: str | None  # ISO date 或 None（未知）
    window_start: str
    window_end: str
    contamination: str  # clean / contaminated / unknown
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "training_cutoff": self.training_cutoff,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "contamination": self.contamination,
            "reason": self.reason,
        }


def lookup_training_cutoff(model_id: str | None) -> date | None:
    """返回模型登记的训练截止日；未登记（或 model_id 为空）返回 None。"""
    if not model_id:
        return None
    return MODEL_TRAINING_CUTOFFS.get(str(model_id).strip())


def annotate_contamination(
    *,
    model_id: str | None,
    window_start: date,
    window_end: date,
    training_cutoff: date | None = None,
) -> LLMProvenance:
    """标注评估窗口与模型训练覆盖区间的关系。

    参数全部 keyword-only（防止把日期位置传反）。``training_cutoff`` 显式传入时
    优先于注册表，便于在同一次研究里用命令行/配置指定。

    - ``window_end <= cutoff``        → contaminated（整窗都在训练数据内）；
    - ``window_start <= cutoff < end``→ contaminated（部分重叠，保守计入）；
    - ``window_start > cutoff``       → clean（窗口起点已晚于截止日）；
    - cutoff 未知                     → unknown（**不**默认 clean）。
    """
    if window_end < window_start:
        raise ValueError("window_end must be >= window_start")

    cutoff = training_cutoff if training_cutoff is not None else lookup_training_cutoff(model_id)
    ws, we = window_start.isoformat(), window_end.isoformat()

    if cutoff is None:
        return LLMProvenance(
            model_id=model_id or "",
            training_cutoff=None,
            window_start=ws,
            window_end=we,
            contamination=UNKNOWN,
            reason=(
                "model training cutoff unknown; cannot certify the evaluation "
                "window is post-training (fail-closed: not labelled clean)"
            ),
        )

    cs = cutoff.isoformat()
    if window_start > cutoff:
        return LLMProvenance(
            model_id=model_id or "",
            training_cutoff=cs,
            window_start=ws,
            window_end=we,
            contamination=CLEAN,
            reason=f"window starts after training cutoff {cs}",
        )
    overlap = "fully within" if window_end <= cutoff else "partially overlaps"
    return LLMProvenance(
        model_id=model_id or "",
        training_cutoff=cs,
        window_start=ws,
        window_end=we,
        contamination=CONTAMINATED,
        reason=(
            f"evaluation window {overlap} training data (cutoff {cs}); "
            "LLM may recall this period — discount this evidence"
        ),
    )


def parse_utc_date(value: str | date | datetime) -> date:
    """宽松解析 ISO 8601 日期/时间戳为 UTC 日历日（研究脚本读 JSON 用）。"""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    # 截到前 10 位（YYYY-MM-DD）；兼容带 T/带 Z 的完整 ISO 时间。
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return date.fromisoformat(s[:10])
    # 兼容毫秒 epoch（13 位）/秒 epoch（10 位）。
    if s.isdigit() and len(s) in (10, 13):
        ts = int(s) / (1000.0 if len(s) == 13 else 1.0)
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    raise ValueError(f"unparseable date: {value!r}")
