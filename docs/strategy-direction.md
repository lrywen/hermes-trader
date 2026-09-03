# 策略方向：入场信号重构 + 双周期 PF 门槛

- 审计标注：Audit 2026-09-03 P3-16
- 性质：**策略研发方向文档**（非代码变更）。本文件只记录结论与路线图，
  不触碰行情/策略核心逻辑；任何代码实现需另行评审。

## 1. 当前策略结论（2026-09 复盘后确认）

1. **暂停加仓 / 暂停继续调参**。现有闸门（冷却、regime、trend surface、
   debate、runner gate）数量已经很多，FARTCOIN 2026-08-26 事件
   （见 docs/RCA-FARTCOIN-quick-stop-2026-08-26.md）表明：信号在 surge
   顶部追入后 88 秒被 ROE 闸门止损——问题在**入场质量**，不在仓位/闸门参数。
2. 研发重心从"参数微调 / 加闸门"转向**入场信号本身的重构**：
   减少在情绪顶部、低质量冲动点的入场，提高每笔入场的边际期望。
3. 信号质量必须用**样本外、双周期利润因子（PF）**量化把关，而不是凭
   单笔盈亏或单周期回测。

代码现状（实证）：
- 全仓搜索 `profit_factor` / `pf_gate` / `profit_factor_hours`：仅
  shadow_book.py L708 在影子账本统计里输出 profit_factor；
  **不存在任何双周期 PF 门槛代码或配置**——本方向尚未落地。
- 回测脚本 scripts/ 下无 PF 门槛实现。

## 2. 入场信号重构目标

| 维度 | 现状痛点（证据） | 重构目标 |
|------|------------------|----------|
| 追高 | 评分跳变（14.7→69.2）可绕 re-research 节流，surge 顶部入场（FARTCOIN RCA 时间线） | 入场需多周期方向一致性确认，单边脉冲不构成入场理由 |
| 信号来源混杂 | TA/whale/news/composite 多源加权，权重未经 PF 验证 | 每个信号源/子信号按双周期 PF 排序，低于门槛的降权或剔除 |
| 研究质量 | debate synth 超时后单 LLM fallback 仍可给出 conf=0.78 并被 gate 放行（730 次 single fallback） | fallback verdict 与 debate consensus 在 gate 中区别对待（RCA 观察项②）：低质量来源不得独立支撑入场 |
| 无回测门禁 | 信号改动无量化验收 | 新信号/改权重必须过双周期 PF 门槛才能进 SHADOW |

非目标：不改动 DSL 止损/仓位/熔断等 fail-closed 闸门；不追求预测方向，
只过滤低边际入场。

## 3. 双周期 PF 门槛定义

对每个候选信号（或信号组合），在两个独立窗口计算利润因子：

- **4h 周期 · 近 365 天**：PF₄ = 总盈利 / 总亏损（按 4h bar 信号撮合，
  含手续费与滑点模型），门槛 **PF₄ ≥ 1.05**
- **1h 周期 · 近 180 天**：PF₁ ≥ **1.05**

规则：
1. 两个周期**同时** ≥1.05 才允许该信号进入 SHADOW 评估；任一不达标即拒绝。
2. PF 按"信号方向 × 后续 N bar 收益"统计，多空分别计算，样本数 <30 的
   信号不予评估（避免小样本噪声）。
3. 成本模型必须含 taker 手续费（execution.taker_fee_pct）与实测滑点
   （memory.avg_exit_slip_bps，当前 30 天中位数），用毛收益算 PF 视为无效。
4. 门槛 1.05 是**最低准入**而非目标；目标档 PF ≥ 1.2，1.05-1.2 之间
   仅允许小仓位灰度。
5. 已上线信号每季度复算 PF，连续两个季度 <1.0 的信号下线。

PF 计算可复用：shadow_book.py L708 的 profit_factor 聚合逻辑
（gross_win/|gross_loss|）、executor.get_atr_hist_mean_pct 的 K 线取数、
backtest_ab_compare 的 regime 评分字节对齐实现。

## 4. 实施路线图

| 阶段 | 内容 | 产出 | 验收 |
|------|------|------|------|
| S1 | 离线回测器增加 PF 双周期报表（不动线上） | scripts/ 下 PF 报表，对现有全部信号源出基线 PF | 能输出每个信号源 PF₄/PF₁ + 样本数 |
| S2 | 信号源 PF 普查：TA/whale/news/composite/动量各子信号 | 信号质量排行榜 | 识别 PF<1.05 的信号源清单 |
| S3 | 入场重构候选：多周期方向一致性 + fallback verdict 降级（gate 侧标记，不改策略核心） | 候选方案 A/B | 候选方案双周期 PF 均 ≥1.05 且优于基线 |
| S4 | 优胜方案进 SHADOW 灰度（复用 sizing-v2 灰度模式：env/配置开关，默认关） | 影子成交对比 | 影子 PF ≥ 回测 PF 的 80%，无新增熔断 |
| S5 | 季度 PF 复算机制（报表 + 告警） | 定期报表 | 长期运行 |

S3 中"fallback verdict 降级"对应 RCA 观察项②：gate_results.debate 增加
`via="single_fallback"` 标记并在统计中与 `debate_consensus` 区分——
这是可观测性/风控标记，不改变 verdict 计算本身。

## 5. 回测验证标准

- 双周期 PF 同时 ≥1.05（准入）/ ≥1.2（目标）。
- 样本数：4h/365d 与 1h/180d 各自信号样本 ≥30。
- 含成本：手续费 + 滑点后净值；同时报告毛 PF 供对照。
- 稳健性：PF 在两个不重叠半年窗口均 ≥1.0（参数鲁棒性，见
  advanced-optimization-roadmap.md 第 4 项）。
- 与基线 A/B：新信号组合相对现网信号组合，PF 提升且最大回撤不恶化。

## 6. 验证方式

- 本文档为方向文档；S1 完成前无代码可验。
- S1 验收命令（回测报表脚本落地后）：
  `python3 scripts/<pf_report>.py --windows 4h:365d,1h:180d --with-costs`
