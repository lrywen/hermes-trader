# W1 批次 1 只读取证收口 SCOREBOARD

> 收口日期：2026-09-19（Asia/Shanghai）
> 依据：统一技术改造文档 §3 批次 1（D-3′ / D-1 / D-6 / F-1）+ 容器 `/data` 只读复核
> 性质：W1「零新数据可执行」批次的证据链闭合记录。只读取证，未改任何生产语义；
> 唯一代码改动是评分器 D-6（commit `bc2bacc`），不触碰交易/下单路径。

## 结论一览

| 项 | 类型 | 收口结论 | 证据 |
|---|---|---|---|
| **D-6** | 评分器缺陷 | **已修**（commit `bc2bacc`） | 新增 INERT 档；88 tests passed |
| **D-1** | 评分器缺陷 | **已被 D-6 修复覆盖** | signal 臂零成熟样本同判 INERT；M8 manual-note 为有意设计保留 |
| **D-3′** | 受控实验 | **维持「不要转正」**；被 D-7 阻塞 | risk_tuning_shadow 292 行，sigma_burst 282 行全在边界带、全流 0 个非空 outcome |
| **F-1** | 文档一致性 | **以整合文档附录 A 为权威更正横幅** | 19 份原件随 09-16 临时目录清理丢失（见 `docs/research/2026-09/README.md`） |

---

## D-6：评分器无成熟样本/零触发误判晋升档（已修）

旧逻辑缺陷（`scripts/shadow_grade.py`）：

1. `decisions>0` 且命中率低于门槛、但**零回填 outcome** 时直接 `return PROMOTE`，
   理由文案却写「建议跑 reconcile 后再定」——档位与文案自相矛盾。
2. 168h 内**一次都没触发**的闸门（`hits=0`，实测 reentry_cap 83 次决策/0 命中）
   被判 `COLLECTING`（继续采数），对不产生任何正反证据的空转臂无意义。

实测规模（容器 `/data/shadow_grade_history.jsonl`，32 个夜间快照 09-07→09-19，
384 条臂记录）：旧逻辑下 **11 条 `PROMOTE_CANDIDATE` 的 `mature_outcomes=0`**
（confidence_decay 4 / atr_regime_calib 3 / sizing_v2 3 / trend_filter_200ma 1）。
其中 confidence_decay 正是命中集有害率 **45.1%**（全记录口径 8.8%，分母误用稀释
约 5 倍）那条，本不该被判可晋升。

修复：新增 `INERT`（无成熟样本·不评价）档；零触发闸门改判 INERT，`hits>0` 的
低命中仍保留 COLLECTING；有 ≥20 条成熟样本且健康的臂仍可达 PROMOTE（回归护栏）。
测试新增 5 例，`tests/test_shadow_grade_arms.py` 88 passed。

## D-1：signal 臂晋升缺陷（已被 D-6 覆盖）

D-1 原文（`0.3_影子证据SCOREBOARD §6 缺陷1`）随报告原件丢失，依据统一文档 §4.4
影子流映射表，D-1 关联 `xs_reversal_shadow`（signal 类臂）。复核：

- xs_reversal 在 09-15～09-17 连续被判 PROMOTE_CANDIDATE，而 hits=0~1、成熟胜率
  仅 15–19%——与 D-6 同类「无正向证据却给晋升档」病灶。
- D-6 修复已把 **signal 类臂零成熟样本一并改判 INERT**
  （`test_signal_arm_grades_on_candidate_field` 断言），D-1 的晋升缺陷随之消除。
- 另一处 signal 臂特性「有害率只出 manual note、不自动 REVIEW」
  （M8，`test_signal_arm_harmful_outcomes_get_manual_note_only`）经核为**有意设计**
  （signal 臂是建议性而非拦截性闸门），保留不改。

## D-3′：sigma_burst_gate 受控实验（维持不转正，被 D-7 阻塞）

容器 `/data/risk_tuning_shadow.jsonl` 只读复核（统一文档编写时 151 行，现 292 行，
流仍在活跃写入；样本增多不改性质）：

| 指标 | 实测 |
|---|---|
| 总行数 | 292（2026-09-19T00:15 → 21:14Z） |
| sigma_burst_gate / would=surface | **282** |
| sigma score 范围 / 中位 | **[45.02, 53.92] / 49.19**——100% 落在 eff_gate(45)≤score<gate(54) 边界带 |
| enforced=true | **0**（全为 shadow 只观测，未对真实下单生效） |
| **全流非空 outcome** | **0**（另有 8 条 outcome 字段全为 `null`，属 breakout/leverage 等其他规则） |

→ 与统一文档结论一致：实验设计干净（边界带采样），但**无 outcome 字段无法计算
反事实期望**，D-3′ 与已作废的 D-3 一样无法独立产出结论；信号为追涨型
（pctMoveSpike+volumeSpike+trendStrength+higherLows1h），与 P3-2 防御性 alpha、
P3-4「多笔日差于单笔日」、P4「应收紧而非放松」方向相反。**维持不转正。**
前置：**D-7（修 reconcile 回填链路补 outcome）需先做并升为高优先级**，否则该流
永远停在「只观测、不可判效」状态。

## F-1：报告更正横幅

19 份调研报告原件已不可得（见归档 README 的检索说明），无法在原件顶部补横幅。
事实上的权威更正是统一技术改造文档**附录 A**：每份报告标注 ✅有效 / ⚠️部分作废 /
❌作废 及权威源归属（如成本口径以 P5-1 为准、机制层以 P6b1c 为准、配置层以 P6b
为准）。后续引用一律回查附录 A，不依赖已丢失原件。

---

## 遗留到后续批次的项

- **D-7**（高优先级）：修 reconcile 回填链路，为 risk_tuning 等 shadow 流补
  outcome 字段；完成前 D-3′ 类受控实验均不可判效。列入后续批次跟踪。
- signal 臂 M8 通道：当前 manual-note-only 是有意设计；是否需要自动 REVIEW，
  待 D-7 补 outcome 后有足够样本再议，不在 W1 改。
