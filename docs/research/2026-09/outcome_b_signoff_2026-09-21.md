# 结局 B 与四范式证伪 —— 人工接受（sign-off）记录

> 日期：2026-09-21（Asia/Shanghai）
> 性质：**结论接受记录**，非新证据。把分散在多个 SCOREBOARD/ADR 中的证伪结论落成
> 一处正式 sign-off，补上审计意义上的"人工确认手势"。
> 关联：[ADR-0003](../../adr/0003-evaluate-strategy-paradigm-shift.md)、
> [ADR-0002](../../adr/0002-evaluate-higher-timeframe-or-strategy.md)、
> [T-02 复核](t02_cpcv_dsr_confirm_outcome_b_2026-09-21.md)

## 一、接受的历史结论

| # | 项 | 结论 | 判定方法 | 状态 |
|---|---|---|---|---|
| 1 | 5m 原策略 | 净期望为负 | block-bootstrap + CPCV + DSR | **OUTCOME_B 接受** |
| 2 | C-7（1h 出场周期） | 四臂 CI 全负 | 留一月份法 + 全样本 | NO-GO |
| 3 | P-A（carry） | alpha 真但 beta 不可对冲 | beta 自相关 0.132 | Deprecated |
| 4 | P-C（结构化反转） | 事件级 0 增量 edge | 事件级 vs 小时级 | Deprecated |
| 5 | P-D（基差，60d） | 成本后 CI 负 | 预注册单持有期 | Deprecated |
| 6 | P-D-v2（高阈值 carry+vol 闸门） | 无同场现货 + CI 负 | 预注册 + 独立块 | Deprecated |

## 二、三方法交叉验证（作为结局 B 的独立复证）

| 臂 | block-bootstrap 95%CI | CPCV OOS 正占比 | DSR P(真实SR>0) |
|---|---:|---:|---:|
| filt | [−13.10, −7.16] | 0.00 | 0.0000 |
| filt_exch | [−16.24, −10.27] | 0.00 | 0.0000 |
| filt_ra | [−11.31, −3.71] | 0.00 | 0.0000 |
| baseline | [−16.68, −11.97] | 0.00 | 0.0000 |

三方法、不同假设，结论一致：**不存在正期望路径**。

## 三、本记录的效力与边界

- 本记录为**历史结论的人工接受**，它**不等于** LIVE 授权。
- LIVE 授权仍需满足 [live_gate](../../../hermes_trader/agents/live_gate.py) 的硬门槛：
  一个 CI 下界 >0 的验收记录（`outcome_a_go_live`）。截至目前**不存在**这样的记录，
  故系统**正确保持 SHADOW**。
- 方向决策（注资 P-B maker 取样 vs 暂停维持 SHADOW）不在本记录范围内，仍待操作者独立拍板。

## 四、配套工具

- 新增统一验证入口 `scripts/validate_outcome.py`：一次跑全三方法、输出二值 verdict
  （`NEGATIVE_EXPECTANCY` / `POSITIVE_EDGE_POSSIBLE`），消除"判断需手动调多处"的出错面。
  已在 b3 数据上复现与单脚本一致的 CI。