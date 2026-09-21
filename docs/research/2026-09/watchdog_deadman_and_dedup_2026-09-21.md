# 风控加固：block-bootstrap 去重 + 第4层独立 dead-man 看门狗 SCOREBOARD

> 日期：2026-09-21（Asia/Shanghai）
> 授权：用户「先优化当前发现的问题，再选择方案」
> 关联：[outcome_b_signoff](outcome_b_signoff_2026-09-21.md)、
> 系统设计合理性评估（2026-09-21）

## 一、解决的两个评估发现

### 1. block-bootstrap 重复实现 → 收敛为单一事实

- 此前 95% CI 在 `scripts/bps_block_bootstrap.py` 与 `scripts/validate_outcome.py`
  各实现一份，长期有口径漂移风险。
- 新增 `hermes_trader.validation.block_bootstrap_ci(series, boot, seed)` 作为
  **唯一实现**；两个脚本都改为调用它。
- 复现验证：b3 结果字节一致——filt **[−13.10, −7.16]**、point −9.65，四臂判定不变。

### 2. 补齐风控第 4 层：独立 dead-man 看门狗

- 现状盘点：容器内已有 healthcheck（HTTP+进程+心跳，300s）、Docker restart 策略、
  进程内 `_watchdog`。缺的是**不在同一进程/容器内**的外部看门狗（kill-switch 分层
  的第 4 层），否则整容器/主机假死时无人告警或平仓。
- 新增 `hermes_trader/watchdog.py`（模块 + `python -m hermes_trader.watchdog` CLI），
  **设计为外部调度器（另一主机 cron / 容器外 systemd timer）调用，进程内不接线**，
  以保证独立性。
- 能力：
  - 心跳来源三口径：session-log.jsonl 循环事件最新 ts / 纯毫秒时间戳文件 / 文件 mtime；
  - STALE 时非零退出 + 可选 `--alert`（复用 notify 层）；
  - 预留 `--emergency-close-cmd` 钩子（默认**不执行任何平仓**，须操作者显式配置）。
- atr_stop 的 DEPRECATED 经核实已在 config_store/config_schema/stop_model 三处固化，
  本轮无需改动；生产配置 `.agent-config.json` 不在本仓、未擅动。

## 二、测试与验证

| 项 | 值 |
|---|---|
| 新增离线测试 | **12**（test_watchdog_deadman 7 + test_block_bootstrap_shared 5） |
| CI floor | 4649 → **4661** |
| 全量离线回归 | **4661 passed / 0 failed / 14 deselected**（332.7s） |
| ruff / walltime | 全绿 |
| 交易语义 / 生产容器 | 零变更，未重启 |

## 三、要让看门狗真正生效（运维动作，待授权）

代码已就位，但独立看门狗需要**外部调度**才有意义，例如在另一台主机：

```sh
# 每 60 秒一次；心跳路径需该主机能读到（共享卷/挂载 session-log）
* * * * * /path/.venv/bin/python -m hermes_trader.watchdog \
    --heartbeat /shared/session-log.jsonl --max-age 300 --alert
```

是否部署该外部调度、以及是否配置 emergency-close，属于运维决策，不在本次代码改造内。