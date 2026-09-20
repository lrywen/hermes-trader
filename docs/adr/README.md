# 架构决策记录（ADR）

本目录是 Hermes Trader **持久化**的架构/配置决策记录。任何改变以下内容的变更，
**必须**在此新增一份 ADR 并在提交中引用（F-3 流程，2026-09-20 确立）：

- 资金安全语义：`mode`（SHADOW/LIVE）、杠杆、止损、仓位/并发上限、notional 上限；
- 回测/影子臂的转正（shadow → enforce）、被否证方向的处置；
- 影响生产交易路径或资金冻结纪律（G3）的任何配置/代码决策。

## 规则

1. **一变更一 ADR**，文件名 `NNN-kebab-title.md`，编号递增不复用。
2. ADR 状态：`Proposed` → `Accepted` →（`Superseded` / `Deprecated`，须链接替代者）。
3. ADR 与代码同提交；没有 ADR 的资金安全变更，评审/CI 应要求补记。
4. ADR 是**决策**记录（为什么这么定、否决了什么），不是实现文档；实现细节回链代码与 SCOREBOARD。
5. 回测数字引用必须满足 F-2 标注规范：**配置源 + 币池 + maxc**（见
   `docs/research/2026-09/hermes-trader_统一技术改造文档_2026-09-19.md` 附录 D）。

## 索引

| ADR | 标题 | 状态 |
|---|---|---|
| [0001](0001-config-freeze-three-invariants.md) | 配置冻结与三条不变量（据丢失原件重建） | Accepted |
| [0002](0002-evaluate-higher-timeframe-or-strategy.md) | 启动「换周期(1h/4h)/换策略」独立评估（C-7，结局 B 触发） | 执行中（参数已确认） |
