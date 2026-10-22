# 0007：HIP-3（DEX 永续）维持关停，登记注资重启前置条件（不改代码/配置）

- 状态：**Accepted**
- 日期：2026-10-22（Asia/Shanghai）
- 决策者：用户拍板（选项「维持关停并登记重启条件」），会话评审取证
- 关联：问题清单复核 2026-10-22 条目 A1；分支 `feat/optimization-v3`（HEAD `9b9708d`）；
  [executor.py](../../hermes_trader/agents/executor.py)
  HIP-3 资产门 `is_hip3 = ":" in coin`（约 L1856）、
  `_hip3_dex_preflight`（约 L2246-2287）、runner 门 `min_hip3_composite`（默认 50，约 L5794/L6375）

## 背景与触发

2026-10-22 只读取证（钱包 `0x78337F1426E62EE0771491841DCe273b50f9536E`）：

- perp 账户 `accountValue = 30.584959` USDC，现货 USDC 余额 20.8；
- HIP-3 目标 DEX（xyz / flx / vntl / hyna / km / abcd / cash / para / mkts / io，共 10 个）
  余额**全部为 $0.00**；
- live 配置（`.agent-config.json`）与 canonical 默认均为 `enable_hip3 = False`。

即便把开关置 true，HIP-3 路径仍有两道独立资金/质量门：
`_hip3_dex_preflight` 对目标 DEX 余额 `< 1.0` 美元以 `hip3_dex_underfunded` 拒单
（查询失败两次才放行交由 Hyperliquid 裁决）；runner 门要求 HIP-3 composite ≥ 50。
即：**当前事实状态是「双重关停」——配置门关 + 资金门关。**

## 决策

**HIP-3 维持关停（`enable_hip3` 保持 false），本次不改任何代码或配置值。**
将「注资后重启」登记为有前置条件的未来动作，条件全部满足后才允许改开关并灰度。

## 重启前置条件（全部满足才可重开）

1. 至少一个目标 DEX 余额 **≥ 1.0 美元**（过 `_hip3_dex_preflight`），
   建议留足 gas/滑点缓冲，首笔小额灰度而非足额；
2. 链上到账经独立只读核验（不依赖下单失败日志）；
3. 该目标 HIP-3 市场 runner composite ≥ `min_hip3_composite`（默认 50），
   且现货成交量过 `min_hip3_volume_usd`（默认 5,000,000）；
4. 先在 SHADOW 观测到 HIP-3 候选通过全部门控的记录，再转灰度实盘；
5. 改 `enable_hip3=true` 走正常配置变更与 ADR 引用流程。

## 考虑过的备选

1. **向 xyz 充值后启用** —— 暂缓。无明确预算与 DEX 选择，当前总资金（约 $51）
   优先保障 Hyperliquid 主路径；充值属用户链上操作，系统侧无法代行。
2. **彻底移除 HIP-3 代码路径与配置键** —— 否决。改动大、不可逆，且配置门关
   （`enable_hip3=false`）已使其零资金风险；保留 dormant 路径的维护成本低于删除后重建。

## 影响 / 后果

- 对资金安全、生产交易路径：零影响（配置与代码均未变）；HIP-3 候选继续在资产门被
  `hip3_disabled` 拒绝。
- 残余风险（记录已知）：未来有人仅置 `enable_hip3=true` 而忘记注资时，preflight 会
  拒单（安全侧），但会产生持续 `hip3_dex_underfunded` 噪声；故重启必须按上述顺序。
- 回滚方式：不适用（无变更）。

## 验收

- 只读取证：`.agent-config.json` 与 `CANONICAL_DEFAULTS` 的 `enable_hip3` 均为 `False`；
- 10 个目标 DEX 余额持续为 0 的情况下，不出现任何 HIP-3 实盘/影子下单；
- 未来重开时，以「前置条件 1-5 全部有证据」作为本 ADR 的关闭/续接判据。
