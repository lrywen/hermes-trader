# 0008：做空开关单一真源 = runner_entry_gate.allow_shorts；chop 对齐回 False（只决策，本批不改值）

- 状态：**Accepted（决策成立；对齐改值另走变更）**
- 日期：2026-10-22（Asia/Shanghai）
- 决策者：用户拍板（选项「runner gate 为真源，chop 对齐回 False」），会话评审取证
- 关联：问题清单复核 2026-10-22 条目 A5；分支 `feat/optimization-v3`（HEAD `9b9708d`）；
  [config_defaults.py](../../hermes_trader/agents/config_defaults.py)
  （`scan_long_only` L386、`runner_entry_gate.allow_shorts` L467、
  `regime_risk_overlay.chop.allow_shorts` L1356）、
  [regime_overlay.py](../../hermes_trader/agents/regime_overlay.py) L316-318、
  [executor.py](../../hermes_trader/agents/executor.py) L5746-5766、
  [config_parity_guard.py](../../hermes_trader/agents/config_parity_guard.py) L186

## 背景与触发

复核发现三个做空相关开关在 live 配置中取值不一，疑似「三态矛盾」：

| 键 | canonical 默认 | live `.agent-config.json` | 所在层 |
|---|---|---|---|
| `scan_long_only` | `False` | **`True`** | 研究准入节流 |
| `runner_entry_gate.allow_shorts` | `False` | **`False`** | 末端执行门（权威） |
| `regime_risk_overlay.chop.allow_shorts` | `False` | **`True`** | 震荡市风险叠加层 |
| `regime_risk_overlay.trend.allow_shorts` | `True` | `True` | 趋势市风险叠加层 |

代码取证表明运行时并无矛盾：

1. **runner gate 是权威执行开关**。`executor._runner_entry_block_reason`
   （L5742 起）先取 `gate.allow_shorts`，再过风险叠加层；做空候选最终以
   `_ov_allow_shorts` 裁决（L6061-6064）。
2. **叠加层只能收紧、不能放开（AND 语义）**。`resolve_applied_knobs`
   （regime_overlay.py L316-318）：
   `out["allow_shorts"] = base and profile.allow_shorts`。
   故 base（runner gate）= False 时，chop=True 被短路，实际效果仍为禁空；
   SHADOW 模式下叠加层 `applied=False`，生效旋钮等于 base。
3. **`scan_long_only` 不是执行门**。它在付费 LLM 研究调用之前跳过纯空头候选，
   纯为省 token / 降噪；持仓币豁免（平仓检查照常）。canonical 注释
   （config_defaults.py L379-386）已明确 runner gate 才是权威空头开关。

真正的遗留分歧只有一处语义：live `chop.allow_shorts=True` 与 canonical 默认
`False` 不一致，含义是「**将来重新放开做空时，震荡市是否额外禁空**」。当前因
base=False 不可达，但一旦 runner gate 置 true，chop=True 会让震荡市也允许做空，
与该叠加层「去风险」的设计意图相反。`config_parity_guard.py` L186 已把这对
`(False, True)` 差异登记为允许漂移对，不阻断启动。

## 决策

1. **`runner_entry_gate.allow_shorts` 是做空方向的唯一权威执行开关（单一真源）。**
2. **`scan_long_only` 定性为从属的研究层节流**：只能减少研究开支，不能放宽任何
   资金保护；其取值不参与、不覆盖执行门裁决。
3. **`regime_risk_overlay.chop.allow_shorts` 的目标值对齐回 `False`**（震荡市
   额外禁空，与叠加层只收紧的语义一致）。
4. **本批不改任何配置值（零行为变更）**。把 live `chop.allow_shorts: true → false`
   登记为后续独立配置变更（经 patch gate / parity 检查后执行）；在 runner gate
   保持 false 的现状下，该改值本身也是零运行时行为变更，仅消除未来放开做空时的
   语义陷阱与 parity 漂移。

## 考虑过的备选

1. **承认真源为 runner gate，但保留 chop=True**（视为刻意策略：将来放开做空后
   震荡市也不禁空）—— 否决。与 chop profile「去风险、尺寸减半、禁 pullback」的
   整体定位冲突，且该 live=true 无 ADR/评审记录支持，属漂移而非决策。
2. **以 `scan_long_only` 为做空总开关** —— 否决。它在研究链路、可被持仓豁免、
   且明确不触达执行路径；让它承担资金门语义会制造「研究跳过 = 资金保护」的假象。
3. **维持现状仅记录分歧** —— 部分采纳：本批即「只决策不改值」，但进一步明确
   了 chop 必须对齐回 False 的动作，而非无限期挂起。

## 影响 / 后果

- 对资金安全、生产交易路径：本批零影响（无代码、无配置值变更）；当前实际行为
  仍为「全方向禁空」，由 runner gate=False 经 AND 叠加层保证。
- 后续动作（独立变更，不在本批）：live `regime_risk_overlay.chop.allow_shorts`
  改为 `false`；届时 parity guard 漂移对 `(False, True)` 不再被 live 触发。
- 长期：未来任何「重新允许做空」的变更必须且只需显式翻转 runner gate，并先确认
  各 regime profile 的 allow_shorts 表达了预期的分层意图。
- 回滚方式：本批不适用；后续对齐改值如需回滚，恢复 live `chop.allow_shorts=true`
  即可（在 runner gate=false 期间无行为差异）。

## 验收

- 只读取证（2026-10-22）：live `runner_entry_gate.allow_shorts=False`、
  `scan_long_only=True`、`chop.allow_shorts=True`、`trend.allow_shorts=True`。
- 代码判据：`resolve_applied_knobs` 保持 AND-only；`_runner_entry_block_reason`
  以 overlay 解析后的 `_ov_allow_shorts` 为唯一做空裁决。
- 后续对齐完成判据：live `chop.allow_shorts=false`，且 parity 检查无该键漂移；
  全量做空相关测试（runner gate / regime overlay）通过。
