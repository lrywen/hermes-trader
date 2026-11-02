# 0011：多空对称入场重构 —— 确定性信号 / LLM 双轨影子对跑，赢者晋升（只设计，本批不改生产行为）

- 状态：**Proposed（设计待用户批准；批准后按阶段落地，先 shadow 不切实盘）**
- 日期：2026-11-02（Asia/Shanghai）
- 决策者：用户（AskUserQuestion 四项拍板）
- 关联：
  [ADR-0008](./0008-allow-shorts-single-source-runner-gate.md)（做空总开关单一真源，本方案不翻转它）、
  [executor.py](../../hermes_trader/agents/executor.py)（`_dispatch_entry_shadow_probes` L2464、
  `_runner_entry_block_reason_impl` L5284、short 分支 L5644-5664）、
  [triggers.py](../../hermes_trader/indicators/triggers.py)（build_hits L875、uptrend L563 权重 0.20 /
  downtrend L590 权重 0）、[ta_filter.py](../../hermes_trader/agents/ta_filter.py)（analyze_perception L845、
  long_only_should_skip L825）、[regime_overlay.py](../../hermes_trader/agents/regime_overlay.py)（allow_shorts AND-only L316）、
  [risk_gates.py](../../hermes_trader/agents/risk_gates.py)（short_liquidity_floor L510、eval_all_gates L2466）、
  [shadow_book.py](../../hermes_trader/agents/shadow_book.py)、
  [backtest_short_5m.py](../../scripts/backtest_short_5m.py)、
  [significance.py](../../hermes_trader/validation/significance.py)（DSR L194 / PBO）、
  [canary_pipeline.py](../../scripts/canary_pipeline.py)（min_dsr_p 0.95 L58）

## 1. 背景与生产取证（2026-11-02，只读审计）

用户目标：显著改善多空双向盈利，专项解决空单入场；要求全量复核、统计显著验收、回测+模拟盘后才上实盘。

### 1.1 真实实盘成交（/data/events.jsonl*，2026-09 起，54248 事件）

| 口径 | 多 long | 空 short |
|---|---|---|
| 真实下单 order 事件 | 19 | **1** |
| 真实平仓 close | 21（胜率 52.4%，sumPnL +0.03） | **1（亏损 −0.11）** |

研究层 execute 事件含 short 11180 次，但**无一穿透到下单**。

### 1.2 空头意图漏斗（/data/runner_gate_shadow.jsonl*，2026-10-24→11-02，9 天）

- runner gate 评估：short **1478** 次 / long 947 次；short **admitted=0（100% 拦截）**。
- 拦截原因：**1154 次（78%）= `runner_gate_blocked (shorts disabled)`**（总开关 allow_shorts=false）；
  其余为 confidence<0.60、extension 超延伸、RSI<25 超卖等次级门槛（这些本应在开关打开后才评估）。

### 1.3 唯一有统计意义的影子基线（/data/.shadow-book.json，round-trip 配对）

| 臂 | long n / 胜率 / PF / sumPnL | short n / 胜率 / PF / sumPnL |
|---|---|---|
| taker | 139 / 46.8% / 1.05 / +1.49 | 19 / 42.1% / **0.75** / −1.25 |
| maker | 65 / 36.9% / 0.79 / −3.17 | 12 / 50.0% / 1.02 / +0.05 |

空头样本仅 **31 笔、两臂方向矛盾** → **不构成「空头信号本身差」的统计证据**。

### 1.4 13 处多空不对称（Explore 全量测绘）

downtrend 权重 0（uptrend 0.20）；scan_long_only 研究前砍空头；allow_shorts=false 最前置短路；
空专属 min_short_confidence 0.72 / min_short_composite 45；$50M short_liquidity_floor；chop overlay 强制关空；
structural override 只升 LONG；momentum_reentry / low_position_relax / pullback_long 均 long-only；
correlation_cap 只限多；breakout flow 只替代 upside 确认；GEX veto 硬编码 long；
candidate_gate_shadow / xs_reversal 纯 long；`side` 缺省 "long"；counter-trend 0.82 > aligned 0.78。

### 1.5 核心定性

**「空单胜率低」在生产层面的真因是空单被多层开关结构性禁止成交（78% 死于总开关），
而非空头信号被证伪。** 因 allow_shorts=false 从未放开，系统从未采集到足够的真实空头结果，
无法对任何空头入场逻辑做统计推断。

## 2. 关键约束（决定架构）

1. **LLM verdict / min_short_confidence 无法历史回放**（backtest_short_5m.py L44 明确记载）。
   若空头主干依赖 LLM，则只能 forward shadow 积样本、无法做历史回测与 DSR。
2. ADR-0008：放开做空必须显式翻转 runner gate 并先确认 regime profile 分层意图。本方案 shadow 期不翻转。
3. 现成基础设施齐备：5m 精确信号回放（backtest_short_5m A–E 变体）、DSR/PBO/block-bootstrap、
   fire-and-forget shadow 探针扩展点（executor L2464）、shadow JSONL + outcome 回填范式、
   canary 晋升链（min DSR p=0.95）、shadow_book 多空对称滑点。

## 3. 决策（用户四项拍板）

1. 改造边界：**全量重构**多空入场架构，但以「新建独立模块 + shadow 对跑」落地，旧生产路径保留不动。
2. 样本策略：历史空头样本不足 → **以重构后 shadow / 模拟盘积累的新样本为准**，不拿 31 笔做硬门槛。
3. 验收口径：**用统计显著性而非固定阈值**（bootstrap 置信区间 + DSR/PBO），不设拍脑袋胜率 pp 数。
4. 推进方式：**先 shadow 影子对跑**，回测 + 模拟双验证、统计显著后，经用户二次确认才切实盘。
5. 信号主干岔路：**确定性信号与 LLM verdict 双轨并行 shadow，统计对比后再决定进实盘的轨道**。
6. 落地形态：**新建对称多空入场模块（可回放信号为 SSOT），旧路径保留，新旧对跑赢者晋升**。

## 4. 架构设计

### 4.1 新模块：`hermes_trader/agents/symmetric_entry/`（新建，生产 hot path 零改动）

- `signal.py`：**确定性、可回放**的多空对称信号 SSOT。
  - 输入：与 perception 相同的 5m/1h/4h closed bars；复用 triggers.build_hits，使 downtrend 与 uptrend
    **对称参与 composite**（不再 weight=0），方向由对称动量 + 反转 K 线 + breakout 共同决定，输出
    `{side: long|short|flat, composite, confidence_like, fired, regime, extension_atr, rsi, macro_regime}`。
  - 多空共用同一阈值函数（镜像 SSOT，参照 ta_filter.late_entry_check 的对称写法），消除 13 处不对称中
    属于「信号层」的部分；信号层不内置任何 long-only 豁免。
  - **纯函数、无网络、无 LLM、无当前时间副作用**（时间与 bar 显式传入）→ 可被 backtest 逐 bar 精确回放。
- `tracks.py`：双轨裁决。
  - **Track-D（deterministic）**：仅用 signal.py 输出。
  - **Track-L（llm）**：signal.py 输出 + 生产 research verdict（side/confidence），LLM 作为可选叠加/否决；
    该轨在历史回测中只评估其「确定性部分」，LLM 部分仅 forward shadow 可评。
  - 两轨对同一候选各自给出 would-side / would-action / reason，互不影响，全部落 shadow 日志。
- `probe.py`：fire-and-forget 影子记录器，挂到 executor `_dispatch_entry_shadow_probes`
  （新增第三个 dispatch，self-gated，daemon 线程，永不 raise、永不触达下单）。
  - 对每个进入 maybe_execute 的候选，额外计算「对称模块在多空双轨下会怎么做」，追加到
    `/data/symmetric_entry_shadow.jsonl`（经 shadow_log.append_jsonl 轮转），字段含
    ts/trace_id/coin/bar_ts/track/side/composite/regime/各过滤值/live_side/live_action/
    entry_px/outcome=None/exit_px=None/pnl_usd=None。
- `reconcile.py`：outcome 回填。决策在 bar i 收盘、fill 在 i+1 开盘（防前视），用与
  backtest/stop_model 一致的 DSL 3 参数梯子模拟平仓，回填 exit_px/pnl_usd/win/hold_minutes，
  复用 shadow_book 的多空对称滑点与费率。

### 4.2 回测：复用并扩展 backtest_short_5m.py

- 新增对称模块回放入口，对 long/short 两侧在同一 5m bar 流上回放 Track-D（及变体：
  对称基线 / +自身 4h 趋势 / +BTC macro down/up / +composite 扫阈 / +regime 分层）。
- 多空分别 + 组合三口径输出：n、胜率、平均盈亏、**盈亏比 PF、最大回撤 MDD、夏普**，
  block-bootstrap 置信区间（block_bootstrap）、独立日 WFE/DSR（significance.deflated_sharpe_prob）、
  PBO（CPCV）。防前视、成本/滑点/DSL 与生产一致。
- Track-L 的 LLM 增量只在 forward shadow 用配对比较（同一候选 D vs L 的结果差）评估，不进历史回测。

### 4.3 晋升（canary，沿用现有链）

- 判据为统计显著而非固定 pp：相对「保留的旧生产路径」对照组，新对称模块在组合口径上
  DSR p ≥ 0.95、PF 置信区间下界 > 1、MDD 不恶化（区间）、且**空头子集非劣**（不要求空头单独显著为正，
  要求不拖累组合且样本量达标）。具体最小样本/独立日数在阶段 5 用 power 分析预先注册（pre-register），
  避免事后挑阈值。
- 晋升仍需：用户二次确认 → ADR 记录 → 显式翻转 runner gate（ADR-0008）并确认 regime profile →
  小仓位 canary → 全量。任何一步不达标停在 shadow，旧路径继续运行。

### 4.4 多空不对称点处置原则

- 信号层不对称（downtrend=0、breakout 只替 upside、side 缺省 long、long-only 豁免/override）：
  在**新模块内**对称化；旧路径不动。
- 资金保护层不对称（allow_shorts、$50M 空头流动性地板、chop 关空、counter-trend 0.82）：
  **保留为独立风险层**，不在信号模块里复刻；晋升时逐项 ADR 决定保留/对称/调参。
  这些是「去风险」而非「信号偏差」，默认保留，靠 shadow 数据证明能否安全放松。

## 5. 分阶段交付与可追溯记录

| 阶段 | 产物 | 资金风险 | 是否需重建 |
|---|---|---|---|
| 4 实现 | symmetric_entry/ 新模块 + probe + shadow 开关（默认 off） | 零（不挂单） | 是（trader） |
| 5 回测 | 对称模块 5m 回放 + DSR/PBO/bootstrap 报告 | 零（离线） | 否 |
| 6 前向 shadow | 开 probe 对跑，reconcile 周期回填，持续积样本 | 零（影子） | 开关热读 |
| 晋升 | 达预注册判据 → ADR → 翻 gate → canary → 实盘 | canary 起有风险 | 逐次确认 |

每次参数/规则变更写入本 ADR 的「变更日志」或新 ADR；shadow JSONL + 回测报告 + 晋升记录构成可追溯链。
回滚：probe/模块均为开关热读、生产路径零改动，关闭开关即完全回退；晋升前不动任何生产下单逻辑。

## 6. 验收（统计显著，非固定阈值；阶段 5 预注册具体数）

- 回测：组合口径 DSR p ≥ 0.95、PF 的 bootstrap 95% 下界 > 1、MDD 区间不劣于对照、夏普为正且区间跨零收窄；
  空头子集达到预注册最小样本且不拖累组合。
- 前向 shadow：在足够独立日（power 分析定）上重复上述方向，D vs L 双轨配对比较给出选轨证据。
- 多空组合的盈亏比 / 最大回撤 / 夏普相对旧路径有**统计可辨的正向改善**，方进入 canary。

## 7. 明确不做（本批边界）

- 不翻转 allow_shorts、不改任何生产下单/风控/仓位逻辑、不改旧信号路径。
- 不依据 31 笔历史影子样本或唯一 1 笔真实空单下「空头信号差」的结论。
- 不把资金保护层的多空差异在信号模块里偷偷对称化（晋升时逐项独立决策）。
