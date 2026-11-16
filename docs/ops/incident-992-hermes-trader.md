# 事故 #992 结构化归档 — hermes-trader

- 事故编号：#992（关联表象 #975）
- 事故日期：2026-11-15（容器时间）
- 影响时长：容器停机 3 小时
- 影响模式：远程会话中断，服务端无对应错误码日志
- 当前终态：running / healthy / restarts=0，Network Changed=0，#975 错误清零，心跳正常

---

## 1. 根因（两项叠加）

### (a) 镜像回滚 — TRAE agent 误将 `:latest` 回滚到修复前旧镜像

- 操作：`hermes-deploy-hermes-trader:latest` 被错误指向旧镜像 `5e0dc93876f1`（标签 `8e1824d6`，修复前版本）
- 后果：10 项历史配置/代码漂移全部复活，容器以已知有缺陷的版本启动
- 处置：已将该标签重命名为 `8e1824d6-BROKEN-pre-parity-patch`，镜像数据保留，消除其“上一个好版本”的误导性；`:latest` 现指向 `:6ec9ea01`（镜像 `e5646a53c7a2`）

### (b) 周度标定参数漂移 — `weekly_calibrate_regime.sh --apply`

- 时间：周日 16:30 自动标定
- 变更：修改了 `regime_classifier` 参数值（fast_ema 15→10、slow_ema 21→30、chop_adx_max 25→22）
- 门禁缺陷：原有门禁采用“值绑定登记”，新旧值不匹配，触发 12 项判红
- 连锁：容器触发 `on-failure:3` 重启策略，累计 3 次失败后停机 3 小时

---

## 2. #992 / #975 的本质

- 仅为**远程会话被打断的表象**，服务端**无对应错误码日志**
- 排查时**禁止在错误码本身浪费时间**
- 正确排查路径：直接查看 `Network Changed` 指标和容器 `RestartCount`

---

## 3. 已验证的处置方案

- 将 `regime_classifier` 相关登记从“值绑定”改为 **`_VALUE_UNBOUND_LEAVES`** 模式（仅校验叶名）
- 效果：
  - 周度标定自动更新参数**不再触发阻断**
  - 新增未登记叶**保持判红**
  - 资金保护类参数变更**保持拦截**
- 门禁边界验证：当前配置无告警、周度参数调整不阻断、新增未登记叶正常判红、资金保护类参数变更正常拦截

---

## 4. 镜像与快照收尾

| 操作 | 结果 |
|---|---|
| 危险标签重命名 | `8e1824d6` → `8e1824d6-BROKEN-pre-parity-patch` |
| `:latest` 指向 | `:6ec9ea01`（`e5646a53c7a2`），宿主机与镜像内 md5 一致（`b6446361…`） |
| 配置快照同步 | `scripts/apply_prod_override.py --capture` 已重新捕获 live 配置，更新 `deploy/prod_override.json` 和 `.agent-config.json` |
| 测试修复 | `test_config_parity_guard.py` 两个与快照时代不一致的既有失败已修复（era 匹配），22/22 通过 |
