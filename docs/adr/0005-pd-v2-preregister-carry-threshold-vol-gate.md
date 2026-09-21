# 0005：P-D-v2 预注册 —— 高阈值 carry 价差 + 已实现波动率闸门（市场中性基差）

- 状态：**Deprecated（2026-09-21 否证，见 SCOREBOARD）**
- 日期：2026-09-21（Asia/Shanghai）
- 关联：[0003](0003-evaluate-strategy-paradigm-shift.md)（P-D 初版）、
  [pd_basis_preregistered_no_go_2026-09-21.md](../research/2026-09/pd_basis_preregistered_no_go_2026-09-21.md)（60d 预注册证伪）、
  外部生产案例 [TierZero: Generating Alpha from Funding-Rate Basis on Hyperliquid](https://tierzero.dev/blog/signal-generation-funding-rate-basis-hyperliquid)

## 1. 背景与假设

P-D 初版（60 天预注册、单一持有期）已被证伪：计入成本均值 −43bps、零成本均值仍 −19.8bps，
独立块 CI 下界不 >0。诊断其信号**过于粗糙**：「7d funding 均值>0 就进场」，既不要求 carry 厚到
盖得过成本，也不区分市场状态——在 funding 因高 conviction/极端持仓而飙升、价格即将剧烈波动时
仍硬收 carry，结果持有期内 funding 转深度负 + delta 腿亏损形成肥尾。

**本 ADR 检验一个新假设（P-D-v2）**：市场中性「买现货 + 空 perp」的基差 edge，只有在
**(a) 预期 carry 显著厚于交易成本**，且 **(b) 不处于高波动/极端持仓 regime** 时才存在。
规则与全部阈值**冻结如下，取自外部生产案例 TierZero，本轮不做任何扫描或调参**。

## 2. 冻结的信号与执行规则（不得在看结果后修改）

### 2.1 进场：carry 价差阈值（单一阈值，不扫）

- 在每个决策小时 t，计算
  - **年化预期 funding**：`AF = fundingRate(t) * 24 * 365`（HL fundingRate 为每小时纯小数）；
  - **年化实际基差**：`AB = (perp_mid(t) − spot_mid(t)) / spot_mid(t) * 365 / H_years`，
    其中以「过去 8h 实际 perp−spot 基差」为短窗实现（见 §5 数据口径）；
  - **carry 信号**：`C = AF − AB`。
- **进场阈值（冻结）**：仅当 `C ≥ 4.0%`（年化）时允许进场；等价的短窗成本门槛为
  **每个 8h 窗口毛 carry ≥ 12.0 bps**（取自 TierZero「entries below ~4% after costs are noise」
  与「clear at least 12–15 bps per 8-hour window」）。
- 方向：`C` 达标为正 → 买现货 + 空 perp；本轮**只做这一方向**（不做反向 flip，避免翻倍试验次数）。

### 2.2 已实现波动率闸门（regime filter，冻结）

- 每小时用**现货 5 分钟收益率（48 个观测 = 4h）**计算已实现波动率并年化：
  `RV = sqrt(sum(r5m^2)) * sqrt(365*24*12)`。
- 闸门：将当前 RV 与 **trailing 30 天的第 75 百分位**比较。
  - **RV ≤ 阈值**：允许按 §2.1 进场、持有到结算；
  - **RV > 阈值**：**不开新仓**；已持仓则在下一个决策小时平仓（「don't harvest into a storm」）。

### 2.3 流动性/深度检查（冻结）

- 进场前要求 **perp 订单簿在 mid 的 0.5% 内深度 ≥ $500,000**；不达标则本小时不进场。
- 该检查用于排除「进场自身打歪基差」的薄书币；历史回测中以可得的 L2/代理口径处理（见 §5）。

### 2.4 成本与再平衡（冻结）

- 固定成本：4 个 taker 边，沿用仓库财务口径：
  **单边 perp taker = 2.5 bps、现货腿 = 逐币半价差（per_coin_half_spread_bps.json）**，
  `cost4 = 2*perp_taker + 2*spot_leg`（现货买卖 + perp 开平）。
- delta 漂移再平衡：当毛 delta 不平衡 **> 0.3% 名义**时才再平衡（计入再平衡成本）。
- 仓位：单币名义敞口沿用账户既有上限；本轮回测**固定等权、不优化 sizing**。

## 3. 持有与出场（冻结）

- **单一持有期 = 30 天**（在初版 30/90 天正区间与 60d 证伪之间，取规则明确、样本更足的 30d；
  本数在本 ADR 接受时即冻结，**不再扫 7/30/60/90/180**）。
- 提前出场仅两种：① §2.2 波动率闸门转超阈值；② carry 信号连续 24h 低于进场阈值。
- 到期按当时现货/perp 中间价平仓，计真实成本。

## 4. 判定门槛（funnel，全部满足才判 P-D-v2 成立）

1. 数据可行：信号所需 funding、现货价、perp 价、RV 在样本内可得（PIT 对齐）。
2. 毛 carry 非 beta：收益来自 funding 价差，且不被 perps 池方向 beta 吞没（沿用 P-A 的 beta 检验）。
3. 成本后 CI：**30 天独立块（不重叠）block-bootstrap 95% CI 下界严格 > 0**，5000 次、固定 seed。
4. 留一稳健：**剔除任一单月后下界仍 >0、不翻号**（重点剔 2026-06 与收益最高月）。
5. 账户可承接：深度/名义/再平衡约束下账户能实际执行。

任一条不满足 → P-D-v2 Deprecated；**只有 1–5 全过**才允许据结果生成 B-13 验收记录。

## 5. 数据口径与已注册的妥协（防止事后美化）

- funding：本地 `logs/funding_cache`（HL fundingHistory，分页自采），以决策小时整点对齐。
- 现货价：回测先用 Binance 5m 收盘价代理（与 P-D 初版一致）；**现货成交真实性单列风险**，
  本轮**先用 ScalarField 免费 funding（同一 HL fundingHistory 端点）做独立管线交叉核对**，
  确认自采 funding 无分页/存储错误；真实历史 L2 深度需第三方数据源（后续 ADR）。
- **已知局限（预注册即承认）**：ScalarField 与自采同源，交叉核对只能验证「取数/存储代码」，
  不能独立验证 HL funding 数值本身；现货代理与历史深度缺口在通过 §4-3/4 前不视为已消除。
- 多重检验：本轮为继 P-D 后的**第二次**基差试验，未来任何正 CI 需按试验次数做 Deflated Sharpe/
  分位校正（思路 5），不以单次名义 95% 为最终证据。

## 6. 执行顺序

1. 本 ADR Accepted（规则冻结）。
2. 思路 4：ScalarField funding 交叉核对，落 SCOREBOARD。
3. 实现 P-D-v2 信号（§2 全规则），在生产口径下回放 81 币、产逐笔 pnl_net。
4. 跑 §4 门槛 3/4 的块自助与留一；写 SCOREBOARD、更新本 ADR 状态、全量回归、提交。
