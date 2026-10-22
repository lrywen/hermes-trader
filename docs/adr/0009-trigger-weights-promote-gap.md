# ADR-0009：trigger_weights canonical→生产 promote 遗漏（trend_strength 0.55 vs 0.30）

- 状态：Accepted（已执行：`2026-10-22T19:03:54Z` config_write `adr-0009-promote-trigger-weights`，验收 4 条全部满足）
- 日期：2026-10-23（Asia/Shanghai）
- 决策者：用户指令（「现在就把漂移写进 ADR 或 promote 队列」），会话评审取证
- 关联：终版裁决 `.review/final_ruling_2026-10-23.md` §0.2 / §3-1；
  canonical 变更 commit `43d8d28`（2026-09-27，「refactor(trigger): 重平衡 trigger 权重」）；
  配置基线 commit `6bdf81a`（B12 注释偏差）；分支 `feat/optimization-v3`

## 背景与触发

config freeze 纪律（ADR-0001）下，canonical defaults 先行、生产配置等 promote。
2026-09-27 commit `43d8d28` 将 canonical `trigger_weights` 调整：

| 键 | 旧值 | canonical 新值 | 理由（commit message） |
|---|---|---|---|
| `trend_strength` | 0.55 | **0.30** | 近1年事件研究：触发率 43% 且负期望，权重过高 |
| `uptrend_momentum` | 0 | **0.20** | 唯一稳定正 EV trigger，由 surfacing 提升为评分项 |

但生产配置（`/home/ldy/hermes-deploy/.agent-config.json`，容器挂载同一份）**从未 promote 此变更**：

| 证据 | 时间 | trend_strength | uptrend_momentum |
|---|---|---|---|
| `.bak.20260912`（最早备份） | ≤ 2026-09-12 | 0.55 | 0 |
| `.pre-p1-20260917` | 2026-09-17 | 0.55 | 0 |
| `.bak-20261016` | 2026-10-16 | 0.55 | 0 |
| 当前配置（mtime 2026-10-21 11:10:09Z） | 2026-10-21 | 0.55 | 0 |

`config_write` 审计（`session-log.jsonl`，仅覆盖 2026-10-19 起）共 6 条事件，
无一触碰 `trigger_weights` 键。

**直接后果**：`6bdf81a` 的 B12 注释（`config.py` L57-62）按 canonical 权重（Σ=1.80, trend .30）
计算 `minCompositeScore=54` 的选择性，得出「单触发器上限 ~22，两重 ~39，三重 ~55.6」。
生产实际权重（Σ=1.85, trend .55）对应数字为 **29.73 / 51.35 / 67.57**，偏差 **+34% / +32% / +22%**。
结论「54 需 ≥3 触发器」两套权重下均成立，但注释的中间数字全部偏离生产现实。

## 决策

1. **确认漂移性质为「promote 遗漏」，非「故意 pin」**。
   若故意保留，B12 注释应使用生产权重计算；使用 canonical 权重恰好证明作者未察觉生产仍在旧值。

2. **本批不热修复配置值**。
   当前 `mode=SHADOW`，改值不影响真实资金，但按纪律仍需用户确认或预注册背书。
   修复动作登记为独立配置变更：生产 `trend_strength: 0.55→0.30`，`uptrend_momentum: 0→0.20`。

3. **修复时必须同步修正 B12 注释**。
   用 promote 后的生产权重重算选择性数字，消除「注释 ≠ 现实」的二次漂移。

4. **建立 promote-queue 登记机制**（轻量，不新增文件）：
   凡 canonical defaults 发生行为性变更（非注释/非新增惰性键），必须在 commit message 或
   关联 ADR 中显式标注 `PROMOTE-PENDING: <key-path>`，否则 parity 检查无法区分「故意冻结」与「漏 promote」。

## 考虑过的备选

1. **立即热修复生产配置** —— 否决。虽在 SHADOW 下无资金风险，但绕过用户确认违反
   「配置写入需明确指令」的会话铁律；且修复后需同步改 B12 注释，应一次性完成。
2. **仅记录漂移，不建立登记机制** —— 否决。本次漂移持续近 1 个月未被任何自动化检查发现，
   说明现有 parity guard 只检「键是否存在」，不检「canonical 与生产值是否一致」；
   不补登记机制，同类遗漏必然复发。
3. **回滚 canonical 到 0.55/0** —— 否决。canonical 变更基于事件研究（负期望证据），
   回退等于放弃研究结论；正确方向是 promote，不是回退。

## 影响 / 后果

- 对资金安全、生产交易路径：当前 `mode=SHADOW`，零真实资金影响；但 `minCompositeScore=54`
  的实际选择性比文档描述的松约 34%，在 LIVE 下会导致更多低分候选通过。
- 需要的门禁 / CI：无新增（parity guard 不检值漂移是已知局限，本 ADR 通过登记机制弥补）。
- 回滚方式：修复后如需回滚，恢复生产 `trend_strength=0.55, uptrend_momentum=0` 即可；
  但需同步回滚 B12 注释。

## 验收

- 漂移确认（已完成）：生产配置所有备份（Sep 12 → Oct 21）`trigger_weights` 恒为旧值；
  `config_write` 审计无相关事件。
- 修复完成判据：
  1. 生产配置 `trigger_weights.trend_strength == 0.30` 且 `uptrend_momentum == 0.20`；
  2. `config.py` B12 注释数字与生产权重重算结果一致；
  3. `config_write` 审计出现一条 `via` 含 `adr-0009` 或 `promote-trigger-weights` 的事件；
  4. 相关单测（composite score 选择性、`trigger_weights` schema 校验）通过。
