# 0012：多空方向盲根因修复 —— 方向感知评分 + 空头镜像触发器 + 方向化新鲜度（从零实现，先 shadow）

- 状态：**Accepted for shadow implementation（生产零行为变更；晋升另走 ADR）**
- 日期：2026-11-02（Asia/Shanghai）
- 决策者：用户（AskUserQuestion：按融合方案从零实现）
- 关联：[ADR-0011](./0011-symmetric-long-short-entry-shadow-dual-track.md)（本 ADR 是其根因修复的细化与修正）、
  [ADR-0008](./0008-allow-shorts-single-source-runner-gate.md)（做空总开关，本批不翻转）、
  上传复核《Hermes_多空与晚追复核_2026-11-02.md》、
  [triggers.py](../../hermes_trader/indicators/triggers.py)（composite_score L1002、build_hits L875、
  higher_lows_1h L778、uptrend L563 / downtrend L590）、
  [executor.py](../../hermes_trader/agents/executor.py)（B2 早退 L5330、short 门槛 L5644）

## 0. 双向复核结论（用户复核文档 × 助手独立复算，2026-11-02）

对上传文档逐条用生产数据独立复算（`/data/decision_shadow_outcomes.jsonl`，2319 行，1033 有 1h 前向覆盖）。

### 0.1 经实测确认成立（采纳）

- **B1 评分函数方向盲**：`composite_score`（triggers.py L1014）仅按 trigger name 查权重，**无 side 维度**；
  空头候选触发看涨触发器照样被加分。这是比 ADR-0011「downtrend 权重 0」更深的根因。
- **B2** allow_shorts=false 最前置早退（executor L5330），runner gate 9 天 1478 次空头意图 100% 拦截、78% 死于此。
- **B3** min_short_confidence=0.72 / min_short_composite=45 位于 B2 之后，**从未执行**（代码顺序确认）。
- **B4** downtrend_momentum 权重 0、bearish_reversal_candle 无权重键。
- **B5** 全仓无 `lower_highs_1h`，多头镜像 `higher_lows_1h` 存在于 triggers.py L778。
- 5 个晚追 veto（overextension / deep_slow_burn / breakout_exemption / pullback_long / breakout_score_floor）
  live 配置全部 shadow_mode=true（只记不拦）。
- **多头追新鲜脉冲显著亏损**：side=long & fresh_impulse=True 的 1h、9.26bp 成本方向化收益
  trade 等权 **−138.2bp**、day-block **−73.8bp**（助手复算与文档逐位一致）。
- **同一特征空头方向相反**：short & fresh=True day-block 助手复算 **+34.1bp**（文档 +15.6，符号一致、数值因日权重/成本口径差异）；
  short & fresh=False 助手 −10.0（文档 −28.6，同号）。
- B_fresh_score day −467bp（文档 −476）、C_daily_mover −50.6bp CI 不含 0（文档 −54.1）：系统最看好路径最差。
- 被拦截组 Z_blocked 方向化后约 +2.6bp —— 被 gate 拦掉的空头反而不亏，佐证单一新鲜度规则错杀空头。

### 0.2 对复核文档的纠正（双向）

1. **文档称 `side_scoring.py`「已实现/已交付 293 行、legacy 等价 5/5」——当前代码树该文件不存在**，
   git 工作区无此产物。S1/S4「已交付」状态不成立，本批**从零实现**，不继承未落地声明。
2. 文档表格空头收益为**方向化 PnL（short 取负）**但未在表头标注；用 raw 前向收益复现符号相反，
   方向化后才吻合。最终方案所有空头收益一律明确标注「方向化」。
3. short 侧具体 bp 两版有差异（聚合细节），但两组 CI 均跨 0、独立信号日 d≤8（文档亦自认 d≤8 是观测非结论），
   故只采信**方向性/排序**结论，不采信精确数值，不据此设固定阈值。

## 1. 统一问题定性（修正 ADR-0011）

根因是**评分与新鲜度逻辑方向盲（B1）**，两个表现：

1. 空头侧：被看涨证据加分 + 看跌权重 0 + 总开关关闭 → 无法成交（ADR-0011 已覆盖）。
2. 多头侧：同一条「新鲜强势」规则使系统追在行情末期，long/fresh 显著亏、B/C 高信心路径最差（ADR-0011 漏覆盖，本批补入）。

「不能做空」与「晚追亏钱」是同一缺陷。修复必须**方向对称**，不能只放开空头。

## 2. 决策：从零实现，分三层、默认零行为变更、全程 shadow

不修改生产 `composite_score` / `build_hits` / executor 下单路径的现有行为；新增纯函数模块，
以「legacy 等价」模式起步，shadow 记录对照，统计显著后才谈接线与放开（晋升仍遵 ADR-0008/0011）。

### 2.1 新模块 `hermes_trader/indicators/side_scoring.py`（新建，纯函数、无网络/时钟/配置 IO）

- `side_aligned_score(side, hits, weights) -> float`
  - 方向化评分：只累加**与 side 同向**的触发器贡献；反向证据不加分（B1 修复）。
  - 触发器方向归类用显式映射（LONG_ONLY / SHORT_ONLY / NEUTRAL），NEUTRAL 双向计分。
  - **legacy 模式**（默认）：返回与生产 composite_score 完全一致的值（方向中性），用于等价性回归；
    `mode="side_aware"` 时才按方向计分。两种模式同函数、可 A/B。
- `directional_weights_view(side, weights) -> dict`：暴露某 side 下的有效权重，供归因/回测。
- 纯函数 + 完备输入校验，空/短序列安全。

### 2.2 空头镜像触发器 `lower_highs_1h`（triggers.py，新增；不接线、不改权重表语义）

- 严格镜像 `higher_lows_1h`（L778）：最近 6 个 1h 高点中递降个数 ≥ required → fired，hit 名 `lowerHighs1h`。
- 本批只新增函数 + 单测；**不加入 build_hits 的生产 hit 列表、不进生产评分**（先在 shadow/回测中调用取证）。
  接线（S2）与权重键（初始 0）在后续独立变更，经评审后进行。

### 2.3 方向化新鲜度裁决 `freshness_verdict(side, row, mode="shadow")`（side_scoring.py）

- long + fresh_impulse → BLOCK（追突破=追末期，生产数据 −73.8bp）
- short + fresh_impulse → ALLOW（卖在拉升=逆势 fade，方向化 +34bp）
- 非新鲜 → 中性 ALLOW
- `mode="shadow"`（默认）恒返回 SHADOW/只记录，不拦截；`mode="enforce"` 才给 BLOCK，且 enforce 本批不接线。
- 输入用与 `decision_shadow_outcomes.jsonl` 同构的 dict（side / fresh_impulse），便于对历史 2319 行回放对照
  current vs side_aware 两列。

### 2.4 不做（本批边界）

- 不翻转 allow_shorts（B2）、不动 B3 门槛、不改 downtrend/bearish 生产权重（B4 接线后续）、
  不把 lower_highs 接入生产 build_hits、不改任何下单/风控/仓位逻辑。
- 5 个影子 veto 是否转 enforce，由各自 shadow 数据独立 ADR 裁决，本批不代决。
- 不设固定胜率阈值；验收用统计显著性（ADR-0011 §6）。

## 3. 验收（本批 = 实现 + 离线验证，非实盘）

1. legacy 等价：对随机/真实 hits，`side_aligned_score(mode=legacy)` 与 `composite_score` 数值一致（含 NaN fail-closed）。
2. 方向正确：构造「空头候选 + 看涨触发」用例，legacy 被加分、side_aware 不加分；多头镜像同理。
3. lower_highs_1h 与 higher_lows_1h 在镜像序列上行为对称。
4. freshness_verdict 四象限（long/short × fresh/stale）正确；shadow 模式恒不 BLOCK。
5. 对历史 decision_shadow_outcomes 回放，产出 current vs side_aware 对照（离线、只读），不据此下统计结论（d≤8）。
6. 全量既有触发器/回测回归通过，生产行为零变更（新函数无生产调用即零影响）。

## 4. 后续阶段（另批、需评审/批准）

S2 接线空头触发器（权重 0 归因）→ S3 lock_side 替换全局开关 → S4 方向化新鲜度 shadow 累计 ≥30 信号日 →
S5 多空双账本对跑 → S6 双侧回测 n≥300/days≥30 + DSR/Bonferroni → S7 实盘（单独批准）。
