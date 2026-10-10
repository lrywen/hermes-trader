# 出场优化已到哪一步、还能到哪一步

- 日期：2026-09-30
- 基线：宿主 HEAD `f0f4ca4`；容器镜像 `sha256:e699c4e7…`（StartedAt 2026-09-28T10:48:06Z，Restarts=0）
- 触发：用户「刚刚出场做了一定优化了，那接下来能优化到什么程度？」
- 约束：结论须有原始输出支撑；未测项显式标注

---

## §0 结论摘要

1. **刚刚那次改动（2026-09-30 03:30 UTC）= 3 个参数，全在出场侧**。其中**实质性的是 trend regime 硬止损 4.0% → 1.5%**；顶层 `max_loss_pct` 1 → 1.5 在**出场路径不可达**（`executor.py:976-978` 明确注释），但**会被 v1 sizing 读取**。
2. **⚠️ 两处风险**：① trend 止损收紧**直接违反代码内记载的设计理由**（`executor.py:966-969`：4.0% 是为了「不被 1h 噪声甩下车」）；② 顶层 `max_loss_pct=1.5` 恰好等于已被否证的影子候选 `candidate_max_loss_pct=1.5`（W4/E-1：**P4 −18.23bps，方向错误，不可转正**）。
3. **该写入无审计记录**：session-log 覆盖 09-28 12:52 → 09-30 03:52，其中 `config_write` = **0**；备份 `.agent-config.json.bak-20260928` 为 **root 属主**。
4. **出场优化的天花板（核心答案）**：14 臂参数族实测跨度仅 **16.1 bps**（+1.33 ~ −14.81），**现实天花板 ≈ +10 bps/笔（乐观），实测已达成 +1.33 bps**。理论极限（完美出场于 MFE）是 **+78.06 bps**，不可达。
5. **缺口的本质是成本，不是出场逻辑**：`filt` 盈亏平衡缺口 +5.2pp，而往返成本等价 **6.58pp**。把手续费归零（8.64bps）就使 `filt` 从 −9.65 → **−1.01**；连滑点一起归零 → **+2.59**。**仅手续费一项 = 出场参数族全部跨度的 54%。**

---

## §1 核实：刚刚到底改了什么

**方法**：对比 `/data/.agent-config.json`（mtime 2026-09-30 03:30）与同批生成的 `/data/.agent-config.json.bak-20260928`

```
/dsl_exit/max_loss_pct                                  OLD=1     NOW=1.5
/dsl_exit/regime_aware/max_loss/trend/max_loss_pct      OLD=4.0   NOW=1.5
/dsl_exit/regime_aware/max_loss/trend/max_loss_roe_pct  OLD=20.0  NOW=15
```

**完整出场配置对比**

| 项 | 改动前 | 改动后 |
|---|---|---|
| `max_loss_pct`（顶层） | 1 | **1.5** |
| `max_loss_roe_pct`（顶层） | 15 | 15 |
| `protect_pct` / `retrace_threshold` | 1.5 / 0.15 | 不变 |
| `breakeven_trigger_pct` | 2.5 | 不变 |
| `regime_aware.max_loss[non_trend]` | 1.5 / ROE 15 | 不变 |
| **`regime_aware.max_loss[trend]`** | **4.0 / ROE 20** | **1.5 / ROE 15** |
| `stop_tuning_shadow` | `{shadow_mode:true, candidate_max_loss_pct:1.5, candidate_breakeven_trigger_pct:1.0}` | 不变 |

### 1.1 哪一项真正生效

`executor.py:962-1002` `select_exit_params` 的原文注释：

```
# 生产 regime_aware.enabled=true 时 trend 块显式给 4.0、non_trend 块给 1.5，
# 故顶层 dsl_exit.max_loss_pct（=1.0）在本出场路径不可达，仅在 non_trend
# 块缺省时作为回落默认。它仍被 v1 sizing 读取（见 _v1_stop_width）。
```

⇒ **顶层 `max_loss_pct` 改 1→1.5 不改变出场止损**（两个 regime 都被 regime_aware 覆盖），但**会改变 v1 sizing**（`_v1_stop_width`）。
⇒ **真正改变出场行为的是 trend 档 4.0% → 1.5%**（收紧 62.5%）。

### 1.2 ⚠️ 与设计理由冲突

`executor.py:966-969` 原文：

> LOOSEN to trend-ride protect/retrace so we RIDE the rippers, **AND widen the hard stop (live: 4.0% spot / ROE 20% in up/down vs 1.5% / ROE 15 in chop/neutral) so trending positions aren't shaken out by 1h noise before the trailing protect kicks in.**

⇒ 4.0% 是**有意**设计的：趋势单要用更宽的硬止损，避免在 tracking 止盈接管前被 1h 噪声甩出。**改成 1.5% 与该设计理由直接矛盾。**

参照 ADR-002 的噪声口径：实盘 18 标的 4h ATR 中位 **4.029%**、最小 BTC **1.257%**；noise band = 0.8×ATR。1.5% 对趋势单约等于 **0.37×ATR**，**落在噪声带内部**。

### 1.3 ⚠️ 与已否证候选重合

`dsl_exit.py:141-160` `_record_stop_tuning_shadow` 原文注释：

> E-1/E-2 否证标注（2026-09-20，W4）：生产候选 `candidate_max_loss_pct=1.5` / `candidate_breakeven_trigger_pct=1.0` 均已被证据否证，**不得转正**——**E-1：放宽到 1.5 在 P4 为 −18.23 bps（方向错误）**；E-2：breakeven 触发降到 1.0 会把追踪止盈价值从 +218 压到 +73 bps。

`docs/research/2026-09/w4_robustness_scoreboard_2026-09-20.md:61`：

> | **E-1** | `candidate_max_loss_pct=1.5` 方向错误（P4 −18.23bps）→ 标注不可转正 | `dsl_exit.py` `_record_stop_tuning_shadow` 加否证注释，shadow 留痕禁 promote（ab7aa1f） |

⇒ 顶层 `max_loss_pct` 现为 1.5，**与 E-1 被否证的候选数值相同**。该参数虽不改变出场止损，但被 v1 sizing 读取 ⇒ **sizing 层把被否证的宽度引入了**。

**副作用**：`would_survive = (abs(loss_pct) < wider_cap and abs(loss_pct) >= effective_max_loss)`。当 live 与 candidate 同为 1.5 时该式恒假 ⇒ **shadow 的 `would_survive` 分支被静默废掉**（`be_would_arm` 分支仍有效）。

### 1.4 审计缺失

| 检查 | 结果 |
|---|---|
| `session-log.jsonl` ts 范围 | 2026-09-28 12:52:45 → 2026-09-30 03:52:06 UTC（**覆盖** 03:30） |
| 其中 `config_write` 事件数 | **0** |
| 备份文件 | `.agent-config.json.bak-20260928`，**属主 root**（生产文件属主 hermes） |

⇒ **第 3 起无审计配置写入**（前两起：09-27 00:00:38、09-27 02:12~12:56）。

---

## §2 核心答案：出场能优化到什么程度

**数据**：`logs/b3_81coin_filt_ra.jsonl`（81 币 × 14 臂 × 180d，318,195 笔，`peak_pct` = MFE，`pnl_net` 已含 fee 8.64 + 双边滑点）

### 2.1 天花板阶梯

| 口径 | net bps/笔 | 说明 |
|---|---|---|
| 完美出场（oracle，恰好出在 MFE） | **+78.06** | 事后不可实现，仅为**上界** |
| 去掉手续费 + 滑点（fee=0, slip=0） | **+2.59**（filt）/ +13.57（breakout7） | 纯信号+出场结构的毛表现 |
| 去掉手续费（fee=0） | **−1.01**（filt）/ +9.97（breakout7） | |
| **14 臂参数族最好**（breakout7, n=299） | **+1.33** | 实测已达成的天花板 |
| 14 臂参数族中位 | ≈ −10 | |
| 14 臂参数族最差（pullback） | **−14.81** | |
| **实测参数族跨度** | **16.14 bps** | 这就是"调参"能给的全部 |
| 当前 `filt`（对齐实盘） | −9.65 | |

### 2.2 为什么天花板这么低：收益结构

`filt` 臂按出场原因分解（n=14,288）：

| exit_reason | 占比 | net 均值 | MFE 均值 | MFE 中位 |
|---|---:|---:|---:|---:|
| `floor_breach`（追踪止盈） | 52.5% | **+77.74** | 142.37 | 112.40 |
| `max_loss`（硬止损） | 45.4% | **−110.66** | 27.67 | **19.30** |
| `stale_flat_timeout` | 2.1% | −16.07 | 51.54 | 54.54 |

- **赢家侧已相当高效**：捕获 112.40 中的 77.74 ≈ **69%（含成本前约 78%）**。剩下的提升空间很小。
- **输家侧是全部问题**：45.4% 的交易 MFE 中位仅 **19.30 bps**、`hold_bars` 中位 **3 根（15 分钟）**就被打到 −110.66 bps。这些交易**从一开始就没走对**——是**入场时点问题**，不是出场回吐问题。
- **因此「出场回吐吃掉 99% 峰值」是构成性假象**：赢家回吐 46%（142→78），输家回吐 138bps 但峰值只有 28bps；两者混合才得出"99%"。**不能据此认为出场逻辑坏了。**（这是对上一份报告 §1 的更正。）

### 2.3 盈亏平衡缺口 ≈ 交易成本

| arm | 赢均值 | 输均值 | 需要胜率 | 实际胜率 | 缺口 | 成本等价* |
|---|---:|---:|---:|---:|---:|---:|
| breakout7 | +153.17 | −107.74 | 41.3% | 41.8% | **−0.5pp** | 4.69pp |
| filt_s60 | +97.75 | −102.08 | 51.1% | 51.1% | **−0.1pp** | 6.13pp |
| filt_ra | +188.16 | −74.88 | 28.5% | 25.9% | +2.5pp | 4.65pp |
| **filt** | +81.14 | −104.86 | 56.4% | 51.2% | **+5.2pp** | **6.58pp** |
| baseline | +143.99 | −108.52 | 43.0% | 38.0% | +5.0pp | 4.85pp |
| pullback | +78.25 | −98.32 | 55.7% | 47.3% | +8.4pp | 6.93pp |

\* 成本等价 = 往返成本 12.24bps ÷ (赢均值 − 输均值) × 100

⇒ **缺口与成本等价同量级**。也就是说：**这套信号的毛（税前）期望大致在零附近，是交易成本把它推到负值。** 出场调参改变的是"赢/输两端的分配"，而**这个分配对总期望近似零和**——收紧止损（`filt_ra`：止损 0.65%，赢 188 @ 25.9%）与放宽止损（`filt`：止损 1.0%，赢 81 @ 51.2%）净效果几乎抵消（−6.66 vs −9.65）。

---

## §3 结论与建议

### 3.1 直接回答

> **出场优化已经基本到顶。** 参数族的全部跨度只有 ~16 bps，实测最好 +1.33 bps，而理论极限 +78 bps 不可达。原因是：**赢家侧已捕获 ~78% 的 MFE（没多少可榨），而剩下的 78bps 差距在输家侧——那里是 45% 的"死单"（MFE 中位 19bps、15 分钟内被止损），属于入场时点问题，出场改不了。**

### 3.2 建议动作（按优先级）

1. **先复核 09-30 的改动**（未审计 + 与设计理由冲突 + 与被否证候选重合）
   - 用 `scripts/bt_ra_exch.py`（读生产配置）跑一次 A/B：`--config` 指向改动前快照 vs 当前，比较 `filt` 臂 net bps
   - 该脚本已支持；`f0f4ca4` 还给回测引擎补了 ignition 透传，可一并回放新的多周期点火出场
2. **补审计**：把 09-30 03:30 的写入补记到 config 台账（或明确记录"由 root 外部进程写入"）
3. **换杠杆**：天花板不在出场参数，而在
   - **成本**：手续费 8.64bps 一项 ≈ 出场参数族全跨度的 54%；maker 路径的逆向选择问题（`post_fill_mid_drift_bps` mean +205bps）需先解决
   - **入场质量**：45% 死单，需在**入场侧**做过滤（meta-labeling / 条件化），而不是出场
   - **信息集**：系统自身元结论（M-1 预注册 §背景）——「问题在信息集而非指标选择」，K线+funding 是公开即时零门槛数据。M-2（Binance tape CVD）已于 09-28 证伪（n=15,756、净 −11.04bps、CI 全负、**gross ≈ −0.04bp ≈ 随机**）。M-1（HL L2 OFI）首次检验不早于 **2026-10-28**。

---

## §4 待核实

1. `bt_ra_exch.py` 各 arm 对应的具体止损宽度（用于把 `filt` / `filt_ra` 的止损差归因到单一变量）
2. 09-30 03:30 写入的执行者（root 属主备份指向容器外进程；nginx 日志未查）
3. trend 档收紧到 1.5% 后，趋势单被噪声甩出的比例（需专门回测，本次数据无法分离）
4. `_v1_stop_width` 读取顶层 `max_loss_pct` 的实际影响面（sizing 变化会改变单笔风险敞口）

---

## §5 复现命令

```bash
# 配置差异（改动前 vs 改动后）
ssh ldy@192.168.124.65 "docker exec -i hermes-trader python3 -" < diffcfg.py

# 天花板 / 成本反事实 / 盈亏平衡
ssh ldy@192.168.124.65 "cd /home/ldy/hermes-trader && python3 /tmp/mfe_ceiling.py"
ssh ldy@192.168.124.65 "cd /home/ldy/hermes-trader && python3 /tmp/exit_deep.py"
ssh ldy@192.168.124.65 "cd /home/ldy/hermes-trader && python3 /tmp/all_arms.py"
ssh ldy@192.168.124.65 "cd /home/ldy/hermes-trader && python3 /tmp/cost_cf.py"
```

---

## §6 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-30 | 初版：核实 09-30 出场改动 + 天花板量化 + 更正上一份报告「回吐 99% 是主因」的构成性假象 |
