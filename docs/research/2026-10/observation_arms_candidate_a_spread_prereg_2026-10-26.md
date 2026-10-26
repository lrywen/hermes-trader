# 观测臂启用预注册：candidate_a_shadow 与 spread_observe（2026-10-26）

> 日期：2026-10-26（00:35Z 复核后冻结，启用前）
> 触发：`剩余未修复项_2026-10-26_0835CST.md` §3.4——两观测臂代码已落地（commit 4a1816b）但生产配置 `launch_capture.spread_observe.enabled` / `candidate_a_shadow.enabled` 均为缺省 false，零数据在收。操作员决定两个都开（§建议顺序 2）。按铁律「新增观测臂先预注册再开」，本文件在改配置前固定观测口径、样本、判定与禁止事项。
> 治理地位：依据 ADR-0001，本预注册属 SHADOW/观测研究，不触发任何真实资金出口；`allow_shorts` 保持 false，两臂均 record-only、不路由、不下单。

---

## 1. 启用范围（精确到配置键）

通过 `update_agent_config`（flock RMW，自动备份 + 写审计）只改两个叶子，其余配置逐字节不动：

```jsonc
"launch_capture": {
  "spread_observe": { "enabled": true },
  "candidate_a_shadow": {
    "enabled": true,
    "require_regime_up": true,   // 冻结：候选 A 必须在币自身 1h regime=="up"
    "log_path": ""               // 空 = <HERMES_DATA_DIR>/candidate_a_shadow.jsonl
  }
}
```

启用后配置 md5 由 `792e8829…` 变为 `a051d07a…`（预期，唯一改动来源），原始文件备份于容器 `/data/.agent-config.json.bak`。配置每轮 scan 热读（`read_agent_config()`），无需重启即生效。

## 2. 观测臂 A：candidate_a_shadow（候选 A 前向样本）

- **假设（已冻结，FWER p=0.205 ⇒ 维持影子，不进 live）**：`higherLows1h ∧ pctMoveSpike ∧ volumeSpike` 在币自身 **1h regime=="up"** 下，固定持有 4h（不挂止损/止盈，裸漂移），扣 **9.47 bp** 往返成本后有正的可执行收益。
- **冻结口径（代码即规格，`agents/candidate_a_shadow.py`）**：combo 三腿缺一不可；regime 用 scan 已抓的 `candles_1h` 经 `market_regime.classify_candles` 判定（零新增网络）；入场参考价取当根 `mid`；每行记录 `bar_close_ms / entry_mid / composite_score / regime / fired_triggers / hold_bars_1h=4 / round_trip_cost_bps=9.47`。
- **前向收益在离线分析时计算**：用 entry_mid join 4h 后的收盘价（不在臂内取未来价，杜绝在线前视）。
- **去重**：同一 `(coin, bar_close_ms)` 每进程仅记一行。
- **样本量与判定（先固定）**：累积 **≥ 250** 个合格 setup 后首检；判定须同时满足
  1. 扣 9.47bp 后每笔平均净收益 **≥ 3 bps**；
  2. 按时间块 bootstrap（块长 ≥ 2× 持有窗，即 ≥8h，B=10000，seed 固定）的净均值 **95% CI 下界严格 > 0**；
  3. 分 regime / 留一币最差不翻号（任一主要子集显著为负即不过）。
  不足 250 判「样本不足」，延长采集，不降门槛、不改 combo/regime/horizon。
- **禁止**：不得据本臂改任何生产下单参数；不得因为 `higherLows1h` 单腿有信号就放宽（其离线为 −5.76 bp）；转正必须另走 LIVE 小额实测，不在本预注册内授权。

## 3. 观测臂 B：spread_observe（top-of-book 价差观测）

- **目的**：为「流动性 / 成本」类候选因子（含已被 Stage A 翻案的 `spread_bps`）累积**生产实测**价差分布，供后续 Stage 0 在真实而非假设的成本下检验。
- **口径**：`volume_profile` payload 新增 `spread_bps`，由 `client/exchange.py::get_cached_orderbook_spread` 从 L2 正结果缓存**只读**得到——**永不发起网络请求**；冷缓存/过期/空盘口记 `null`。
- **覆盖率口径（接受窄覆盖，§3.5）**：L2 缓存仅由「接近下单」路径（`get_orderbook_spread` @ executor 两处、`_ioc_cross_price`）暖，因此零成交期绝大多数扫描币为冷缓存、`spread_bps=null`。**这是预期行为，不是缺陷**。分析时必须报告 `覆盖率 = 非null行数 / volume_profile 行数`，并显式区分「接近执行的币」（选择后人群，存在选择偏差）与「全体浮出币」；不得把窄覆盖样本当作全市场无偏价差。
- **唯一消费方**：`signal_rank._liquidity_component`（shadow `signal_ranking`，mode=shadow 不影响路由/下单）。缺失/null 映射为中性 50。
- **判定**：本臂只采集、不设通过门槛；是否升级为「全浮出币 WS 直取」覆盖率，取决于本臂显示的选择偏差大小，另案预注册（需新建 WS 订阅缓存链、碰热路径，不在本轮）。

## 4. 数据可比性记账（对应复核 §4 / §3.3）

- `signal_ranking.jsonl` 自本轮起每行带 `rank_caliber`（`rank_score.v2 … liq=.05(spread_bps,neutral50) …`）。**v1（liquidity 常数 55）与 v2（spread 驱动，缺失中性 50）记录不得混比**；每个 VP-bearing 币总量在 spread=null 时较 v1 约 −0.25 分。
- Stage 0 波动桶判据自本轮改为样本量自适应门槛 `max(min_abs_ic=0.10, tanh(1.96·1.0637/√(n−3)))`，CLI 冻结串 `caliber` 记录该公式，且每个 `vol_group` gate 输出逐桶 `thresholds`。Stage A 旧报告里在「纯符号 / 固定 0.10」下「通过」的 `spread_bps`（三桶 IC −0.0090/−0.0225/−0.0109）在新判据下**不再通过**——这是判据收紧导致的**结论翻转**，旧结论不可再被引用为「spread_bps 通过」。

## 5. 回滚

- 关闭任一臂：将对应 `enabled` 改回 false（同样走 `update_agent_config`），即恢复严格 no-op；已落盘的 JSONL 保留（append-only 研究数据），不删除。
