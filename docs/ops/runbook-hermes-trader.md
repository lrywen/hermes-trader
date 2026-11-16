# Hermes Trader 运维规范（runbook）

适用对象：Docker 容器 `hermes-trader`（Compose 目录 `/home/ldy/hermes-deploy`）。

---

## 1. 容器与状态文件位置

| 项目 | 值 |
|---|---|
| 容器名 | `hermes-trader` |
| 镜像 | `hermes-deploy-hermes-trader` |
| 状态文件（容器内） | `/data/.agent-config.json` |
| 配置版本化快照 | `deploy/prod_override.json` |
| 仓库本地配置副本 | `.agent-config.json`（gitignored） |
| 健康检查 | `docker inspect -f '{{.State.Status}} {{.State.Health.Status}} Restarts={{.RestartCount}}' hermes-trader` |
| 重启策略 | `on-failure:3`（最多重启 3 次，避免无限重启导致网络风暴） |

---

## 2. 周度标定流程 — 配套门禁规则

`weekly_calibrate_regime.sh --apply` 每周日自动调整信号生成类参数。为防止重演 #992：

- **禁止对自动调整的信号生成类参数设置硬值绑定**：EMA 周期（fast_ema / slow_ema）、ADX 阈值（chop_adx_max）、slope 参数（slope_threshold）等由周度标定自动更新的参数，门禁仅校验**叶名**，不校验叶值（`_VALUE_UNBOUND_LEAVES` 模式）。
- **资金保护类参数保持值绑定并判红**：如 `max_total_notional_pct`、`max_daily_loss_usd`、`min_available_margin_pct` 等，任何值变更必须显式登记并复核。
- **新增未登记叶**：任何不在 `LEAF_DANGER_KIND` / `_SANCTIONED_LOOSER_LEAVES` / `_REVIEWED_SAFE_DIVERGENCES` 中的新叶，一旦与 canonical 漂移，立即判红。

验证命令：
```bash
cd /home/ldy/hermes-trader
python3 -m pytest tests/test_config_parity_guard.py -q
# 期望：22/22 通过，含 test_tracked_snapshot_era_matches_live、test_repo_local_config_era_matches_live
```

---

## 3. 镜像标签命名规范

- **故障版本必须添加明确的 `BROKEN` 标记**：如 `8e1824d6-BROKEN-pre-parity-patch`，镜像数据保留，仅通过标签消除“上一个好版本”的误导性，避免后续误回滚。
- **禁止使用易被误判为正常版本的 commit hash 标签**：不得将故障镜像打上裸 commit hash（如 `8e1824d6`、`5e0dc93`）作为可回滚的“上一版本”标签。
- `:latest` 始终指向当前经过验证的生产镜像（现为 `:6ec9ea01`）。

重命名危险标签示例：
```bash
docker tag hermes-deploy-hermes-trader:8e1824d6 hermes-deploy-hermes-trader:8e1824d6-BROKEN-pre-parity-patch
docker rmi hermes-deploy-hermes-trader:8e1824d6   # 仅移除误导标签，镜像数据保留
```

---

## 4. 配置快照同步（生产调优回流）

生产调优在持久化卷 `/data/.agent-config.json` 上演进，版本化快照 `deploy/prod_override.json` 必须从 live 回流，否则 `--apply` 会静默回退生产 tuning：

```bash
# 捕获当前 live 配置到版本化快照（去重 mode，保留 _comment）
python3 scripts/apply_prod_override.py --capture \
    --config /home/ldy/hermes-deploy/.agent-config.json

# 用捕获结果覆盖仓库陈旧的 .agent-config.json（保留 reentry_cap / signal_age_decay 等仓库只读研究臂）
```

断言门禁：`test_tracked_snapshot_era_matches_live`、`test_repo_local_config_era_matches_live` 要求快照与 live 的 era 指纹一致。

---

## 5. 故障排查原则（#992 / #975 教训）

- 远程会话中断（#992 / #975）是**表象**，服务端无对应错误码日志，**禁止在错误码本身浪费时间**。
- 直接查看 `Network Changed` 指标和容器 `RestartCount`。
- 容器 `restarts=0` 且 `healthy` 即表示 `on-failure:3` 策略已按设计生效，未发生无限重启。
