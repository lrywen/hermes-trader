# 0006：看门狗 scheduler 活性判据维持 mtime-only（recheck-4 P1 关闭，不改代码）

- 状态：**Accepted**
- 日期：2026-10-21（Asia/Shanghai；取证窗口为 UTC 2026-10-19 至 10-21）
- 决策者：会话评审，用户授权「关闭 P1 并更新文档」
- 关联：[watchdog SCOREBOARD（2026-09-21）](../research/2026-09/watchdog_deadman_and_dedup_2026-09-21.md)；
  recheck-4（2026-10-18）看门狗 P1 条目；
  提交 `978a88b`（selection_attribution 1h 桶去重根治）、
  `abe5c81`（P0-1/P0-7 scheduler 单 job 硬超时 + 校验门）、
  `aa5a5f3`（recheck-4 P0-A..E parity gate 与 scheduler 耦合整改）

## 背景与触发

recheck-4（2026-10-18）提出一条 P1：外部看门狗的
`check_scheduler_freshness()`（[hermes_trader/watchdog.py](../../hermes_trader/watchdog.py)）
**只用 `scheduler_state.json` 的 mtime 判定 scheduler 活性，不把 `last_ok:*` 并入 ok 判据**。
担忧场景：scheduler 主循环活着、每 60s 重写状态文件（mtime 新鲜 → 看门狗绿），
但 daily job 静默连续失败时没有红色通道。

调查确认这是**刻意设计而非缺陷**：函数 docstring 明确——主循环每 60s 无条件重写状态，
mtime 是可靠的活性信号；`last_ok:*` 仅拼进 reason 文本，job 级成败交给 panel 消费，
避免把冷启动/重建后「尚无 job 成功」误报为故障。

历史上确有 daily 漏跑（journal 显示 Oct 13–18 每晚仅 1–3 个 daily 片段执行），
但根因不是看门狗判据，而是：

1. **积压卡死单线程循环**：10-18 00:15 `reconcile_selection_attribution` 撞上约 4.5 万条
   selection_attribution 脏积压，长时间占用串行循环，致 :20–:55 job 全灭，01:03 容器重启；
2. **夜间高频容器重建 + 2 分钟触发窗口无补跑**（如 Oct 16 单日十几次 restart-manager）。

两者均已被现有修复覆盖：①由 `978a88b` 根治（cycle 归一化到 1h 逻辑桶
`CYCLE_BUCKET_MS`，同小时多 tick 不再重复登记），②由 `abe5c81` 的单 job 硬超时
覆盖（`reconcile_selection_attribution` 240s、其余默认 900s，超时返回 124，
不再吞掉窗口内相邻 job）。

## 决策

**关闭该 P1，不改代码。** 看门狗继续以 mtime-only 作为 scheduler ok 判据，
`last_ok:*` 维持现状（仅展示 + 供 panel 消费），不并入健康判定。

## 关闭证据（UTC，只读取证）

容器自 2026-10-20 15:54 起 RestartCount=0 跨过 UTC 午夜，之后：

- **连续两夜（10-20、10-21）9 个 daily job + `macro_regime_watch` 全部按点 exit=0**，
  无 TIMEOUT，串行顺序正确。10-21 各 job 落盘时刻约为
  00:00:56 / 00:10:34 / 00:15:36 / 00:20:37 / 00:25:39 / 00:30:45 /
  00:40:47 / 00:50:31 / 00:55:44；`scheduler_state.json` 中 10 个 job 的 slot 与
  `last_ok:*` 均为当日窗口。
- **曾卡死整批的 00:15 reconcile 恢复正常**：240s 预算内 exit=0。
- **积压受控、不变量保持**：`/data/selection_attribution.json` total 8046
  （pending 3375 / settled 4671；coin_selection 2827/2752、signal_ranking 547/1920），
  off-grid（`cycle` 不在 1h 桶）= **0**，唯一键 8046/8046，dupes = **0**；
  折叠后从 5435 的增长为正常有界累积，未触 4000/source 的 settled 上限。
- **看门狗自身健康**：宿主机 `hermes-watchdog.timer` 触发的 oneshot 在
  2026-10-21 02:47:31 status=0 / SUCCESS。

## 考虑过的备选

1. **把 `last_ok:*` 纳入 ok 判据（如要求当日 daily slot 全部 last_ok）** —— 否决。
   scheduler 状态文件在容器冷启动/重建后从零开始，daily 窗口一天仅一次：
   重建后到下一个 UTC 午夜前，朴素 last_ok 判据必然报「当日无成功」，把正常运维
   误报成故障；且 2 分钟窗口本就无补跑语义，"当日未成功"不等于"进程死了"。
2. **现在就实现「容忍重建的 last-slot 漏跑检测」**（按 slot_id 比对
   `state[job]` 与 `state[last_ok:job]`，并对容器重启时间戳设豁免）—— 否决/延后。
   属为假设需求增加复杂度：漏跑根因已被 `978a88b` + `abe5c81` 覆盖并经两夜实证，
   SHADOW 模式下漏跑只损失观测数据、无资金危害，此刻没有需要红色通道的真实损失。

## 影响 / 后果

- 对资金安全、生产交易路径：零影响，纯观测/告警判据，未改任何代码与配置。
- 残余风险（记录已知）：主循环存活但全部 daily 静默失败时，看门狗不报红；
  job 级成败目前只在 panel 的 `last_ok:*` 视图可见，无主动告警。
- 回滚方式：不适用（无变更）。

## 再评估触发条件

出现以下任一条件时，重开本项并实施备选 2（slot 级漏跑告警，须容忍重建/冷启动）：

1. 系统由 SHADOW 转 **LIVE**，或某个 scheduler job 的漏跑开始产生资金安全后果
   （而非仅损失观测样本）；
2. 现有修复落地后，再次观测到 daily slot 实际漏跑（slot 已写但 `last_ok` 缺失，
   且非容器重建窗口所致）连续出现。
