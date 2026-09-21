# T-02：CPCV + DSR 独立复核 —— 三方法一致确认 OUTCOME_B SCOREBOARD

> 日期：2026-09-21（Asia/Shanghai）
> 授权：用户「按建议立即执行改造」（执行方案 T-02 / GAP-VALID）
> 关联：[ADR-0003](../../adr/0003-evaluate-strategy-paradigm-shift.md)、
> [b3 SCOREBOARD](b3_81coin_scoreboard_2026-09-19.md)、
> 外部方案《Hermes-Trader 技术选型与改造方案 v1.0》T-02

## 裁定：OUTCOME_B（可信但净期望为负）被**两种独立新方法再次确认**。原 block-bootstrap、CPCV、DSR 三方法结论一致，二值判定——**不存在正期望路径**。

---

## 一、做了什么（GAP-VALID 收口）

新增纯标准库包 `hermes_trader/validation/`（不替换原 block-bootstrap）：

- `day_bps_series`：与 [bps_block_bootstrap.py](../../scripts/bps_block_bootstrap.py) **同一数据契约**
  （trade JSONL → 逐笔 `pnl_net/notional*1e4` → 按天聚合）；
- **CPCV**：6 组、选 2 组为测试集（C(6,2)=**15 条 OOS 路径**），测试段两侧 purge + 1 天 embargo；
- **DSR**：Bailey & López de Prado 闭式解，对多试验最优夏普做选择膨胀校正（含自实现标准正态 CDF/PPF）；
- **PBO**：OOS 路径落到零基准以下的占比。
- 零第三方框架依赖（满足方案 C-05）；17 个单元测试。

## 二、真实数据结果（b3_81coin_filt_ra.jsonl，81 币、185 个交易日）

| 臂 | 日bps均值 | 非年化夏普 | **CPCV OOS 路径 SR>0 占比** | 中位 OOS bps |
|---|---:|---:|---:|---:|
| filt | −10.14 | −0.487 | **0.00（0/15）** | −10.09 |
| filt_exch | −13.26 | −0.633 | **0.00** | −13.34 |
| filt_ra | −7.52 | −0.283 | **0.00** | −8.06 |
| baseline | −14.32 | −0.853 | **0.00** | −14.24 |

- **CPCV**：四个臂、15 条 OOS 路径，**没有一条**样本外夏普为正（win_frac 全 0）。
- **DSR**：四臂中最优夏普本就是负（filt_ra −0.283）；按保守 16 次试验校正后
  **P(真实夏普>0) = 0.0000**。
- 对照原 block-bootstrap：四臂 95% CI 全在 0 以下。

→ 三种方法、不同假设，**结论完全一致**。

## 三、方法学意义

- 排除了"OUTCOME_B 只是单一 block-bootstrap / 单 seed 的产物"这一质疑：CPCV 用多路 OOS、
  DSR 用多重检验校正独立得到同一结论。
- DSR/PBO 已就位：未来任何新策略即便跑出正夏普，也须经试验次数校正（不以单次名义 95% 为据）。
- 这是把"证伪能力"补齐到与方案目标一致；它确认负期望，但不产生 edge。

## 四、验证与影响

- 新增 17 个离线测试；全量回归通过（见提交）；ruff 无问题。
- 纯增量模块，未改任何交易/回测数值行为，未触碰生产容器，零资金敞口。
- 数据契约共享，便于后续接入新策略复用同一三方法判定。

## 五、剩余

- GAP-ARCH（executor 解构）已由 [ADR-0004](../../adr/0004-defer-engineering-decomposition-items.md) Deferred；
  GAP-EXEC（T-04 执行真实性建模）blocked-on 成交/影子数据。
- 唯一方向决策仍是 ADR-0003 末尾：**注资小资金实盘取样（P-B maker）或暂停维持 SHADOW**，待操作者拍板。
