# Hermes-Trader 新增功能前端 Web 接入与集成评估（2026-09-07）

> 背景：震荡市七波次改造（S0–S6）+ Pathia 吸收（夜间评级器、C11 硬底、shadow 巡检修复）上线后，对"系统优化升级新增功能"做前端接入梳理。本文件明确：①各新增功能业务需求与技术实现；②需接入功能的前端交互/UI/数据接口；③对现有前端架构影响；④优先级与实施计划；⑤验收标准。
>
> 关键结论先行：本轮新增能力**绝大多数是"后端风控埋点 + 夜间 CLI 评级器"，尚未通过任何 HTTP 端点暴露**。前端接入第一步不是写页面，而是先在 trader 侧补只读 API（M1），再经门户 BFF 代理（M2），最后做页面（M3）。

---

## 0. 系统现状（取证基线）

- **唯一在役前端**：`/home/ldy/hermes-portal`（Vue 3.5 + TS 5.5 + Vite 5.4 + Pinia 2.2 + Tailwind 3.4 + ECharts 6，15 个业务页面）。trader 仓内无任何前端代码（旧 hermes-web 已归档，nginx `/web/`→`/portal/` 301 收口）。
- **通信三层**：浏览器同源访问 nginx（8443/TLS 自签）→ BFF（hermes-portal:9000，认证/RBAC/菜单/推送/提醒/审计）→ 代理 hermes-trader:8000（注入 `X-Operator-Token`/`X-Internal-Token`）。
- **认证**：内存 access JWT（30min）+ httpOnly refresh Cookie（7d 轮换）+ SSE 60s ticket；axios 401 自动刷新重试；BFF `_PATH_RULES` 写操作 fail-closed。
- **实时**：全局 SSE 单例（`sseFeed.ts`，退避重连 + 1.5s 回放/新鲜度过滤）+ 风控卡 5s / AppShell 10s 轮询双通道。
- **trader API**：唯一 FastAPI app（`hermes_trader/server.py:166`），路由 = server.py 直接装饰器 + `dashboard_routes/{config,operator,shadow,public}.py`。鉴权依赖 `_require_operator`（读）/`require_operator_write`（写，per-IP 限速）/`require_operator_or_internal`（LAN/loopback 免 token）。
- **部署**：双 compose 栈经外部网络 `hermes-deploy_default` 互联；trader 双进程（server + trading_loop），mode=SHADOW（不下真单）。

> 术语区分：`/api/dashboard/shadow/*`（SHADOW 模式**纸账户资金账本**，ShadowBook 页已接）与本次的"**风控臂影子采数**"（12 个 `*_shadow.jsonl`）是两套完全不同的数据。后者目前门户完全看不到。

---

## 1. 新增功能盘点：业务需求 × 技术实现 × 前端可接入性

| # | 新增功能 | 业务需求 | 技术实现（现状） | HTTP 暴露 | 前端接入动作 |
|---|---|---|---|---|---|
| F1 | **9+ 个 shadow 风控臂**（regime_overlay / xs_reversal / trend_filter / reentry_cap / daily_extension_cap / confidence_decay / signal_age_decay / atr_regime_calibration / sizing_v2） | 震荡市新闸门先 shadow 采数不拦截，积够样本再人工升 enforce | 各臂 `shadow_log.append_jsonl` 落盘 12 个 JSONL（`/data/*_shadow.jsonl`），mode=off/shadow/enforce | ❌ 触发量/命中率/would_block 无端点（仅 `/metrics` 有 block 计数） | **需新 API + 新页面** |
| F2 | **夜间评级器** `scripts/shadow_grade.py` | 吸收 Pathia：每晚 24/72/168h 给各臂六档评级（PROMOTE/COLLECTING/INSUFFICIENT/DATA_GAP/REVIEW/OFF）+ 飞书建议 | CLI，cron 08:45 跑 `--push`；结果只发飞书 + stdout，**不落历史** | ❌ 无端点、无历史存储 | **需新 API（含结果持久化）+ 新页面** |
| F3 | **采数进度盘点** `scripts/shadow_progress.py` | 发现"门变盲"（配了 shadow 却 0 条） | CLI `--json` | ❌ | 并入 F1/F2 页面 |
| F4 | **C1 熔断门 fail-open + 飞书告警** | memory 门故障 fail-open 但必须告警 | `_alert_memory_gate_blind` 直发飞书；熔断**状态**经 `risk_status_snapshot` | ✅ 状态已暴露 `/api/dashboard/risk-status`；⚠️ **blind 告警只走飞书，不进 SSE** | 门户弹窗/横幅收不到 blind，**建议补 SSE 事件** |
| F5 | **events.jsonl 哈希链台账** | 真实成交 order/close 全链路、SHA-256 防篡改审计 | `event_log.query_events`/`verify_chain` | ❌ 仅 close 汇入 `/closed-trades`；order 事件、链校验无端点 | 需新 API + 审计页卡片 |
| F6 | **reconcile 对账结果**（fills / pullback / ta_late_entry） | 孤儿单、影子回填、反事实 outcome | CLI 写日志/cron/回填 | ❌ | 需新 API（状态文件）+ 状态卡片 |
| F7 | **各臂 mode/参数配置** | 人工拍板升 enforce、调参 | agent config 各 gate 块 | ✅ `/api/dashboard/config`（schema/backup/rollback 全套） | Config 页已能改，缺"臂 mode 一览 + 评级联动" |
| F8 | **飞书投递熔断状态** | 知道告警是否发得出去 | notify webhook 熔断/降级链 | ❌ 仅 `/metrics` 计数器 | 低优先，并入运维页 |

---

## 2. 各需接入功能的前端交互 / UI / 数据接口

### P0 — F2/F1/F3：风控影子臂与评级中心（新页面 `/risk-arms`）

**交互逻辑**
- 顶部真实成交基线条：closes 数、胜率；closes=0 时显式提示"SHADOW 无真钱对照，评级仅基于影子采数"。
- 主表 12 臂，每行：臂名、kind（拦截/调仓/信号）、**mode 徽标**（off 灰 / shadow 黄 / enforce 绿）、**verdict 徽标**（六档配色）、24/72/168h 三窗触发量与命中率、成熟度进度条（n/60）。
- 排序：DATA_GAP(红) > REVIEW(琥珀) > PROMOTE(绿) > 其余。
- 行点击 → 抽屉详情：三窗统计、最近命中样本、（回填后）反事实胜率/pnl。
- **DATA_GAP 行整行红 + 页顶横幅**"该采没采：门变盲"；**PROMOTE 行**按钮"去配置页升级"→ 跳 `/config` 定位该臂（**仅跳转，此页不提供任何改闸门入口**，守 inert 红线）。
- 数据为夜间预算；operator 可手动"立即刷新"（触发重算），viewer 只读。

**UI 规范**：复用 `.card/.badge(-ok/-warn/-danger/-muted/-purple)`、深色金融终端风、中文；verdict 六档映射现有 badge 色系 + 新增 neutral 灰给 OFF；ECharts 可选画命中率趋势（F2 历史）。

**数据接口（M1 新建，trader 侧）**
- `GET /api/dashboard/shadow-arms/grades?windows=24,72,168` → 包 `shadow_grade.collect_grades()`，返回 `arms[]{arm,mode,kind,verdict,verdict_cn,reason,windows[]{total,hits,decisions,hit_rate,mature_outcomes,pnl_usd_sum}} + real_baseline`。**TTL 缓存 60s**，避免每请求解析 ~2.5MB JSONL。
- `POST /api/dashboard/shadow-arms/refresh`（operator 写）→ 强制重算返回。
- `GET /api/dashboard/shadow-arms/grade-history?days=30` → 读 `/data/shadow_grade_history.jsonl`（**需 shadow_grade 每晚追加落盘**，现状只发飞书）画趋势。
- 鉴权：operator 读；BFF `_PATH_RULES`（M2）登记，读权限复用 `config:read` 或新增 `risk:read`。

### P1 — F4：C1 blind-gate 告警进门户

- **交互**：blind 事件接现有 `AlertPopup`（danger 手动关闭）+ Overview 风控卡状态点。
- **接口**：trader 在 `_alert_memory_gate_blind` 发飞书同时向 feed 推 `risk_gate_blind` SSE 事件；前端 `useTraderEventBridge.mapTraderEvent` 增映射 → `risk_alert`（现有 10 类提醒已含 risk_alert/circuit_breaker，**无需新权限/新组件**）。

### P2 — F5：台账哈希链完整性（审计页 `/audit` 卡片）

- **交互**：链校验状态（OK / 断裂位置）、事件总数、order/close 计数；可按类型查最近事件。
- **接口**：`GET /api/dashboard/ledger/verify` → `event_log.verify_chain()`；`GET /api/dashboard/ledger/events?type=&limit=` → `query_events()`。鉴权 `admin:audit`。

### P2 — F6：对账状态（运维页 `/operator` 卡片）

- **交互**：最近一次 fills reconcile 时间、orphan-open/orphan-close/phantom-local 计数、回填状态；pullback/ta_late_entry 反事实回填进度。
- **接口**：reconcile cron 写 `/data/reconcile_status.json`，新增 `GET /api/dashboard/reconcile/status` 读它。

### P3 — F7/F8：配置页增强 + 通知状态

- Config 页各 gate 块增"臂 mode 分组视图"，旁挂评级 verdict（读 F1 接口）；升级仍走现有 force-confirm/backup/rollback。
- 通知熔断状态读 `/metrics`（BFF 内部 token 代取）做状态点，低优先。

---

## 3. 对现有前端架构的影响

- **兼容性**：全增量——新端点、新路由、新菜单项（BFF `/menu` 按权限下发，无权限不可见）；不动现有 15 页、不改 SSE/axios 契约；旧 SPA 已归档无包袱。
- **性能**：唯一风险是 grades 接口实时解析 12 个 JSONL（ta_late_entry 已 1MB+ 且增长）。对策：夜间 cron 算好落历史快照，端点默认读快照（O(小文件)）；手动刷新才重算 + 60s 缓存；列表页不高频轮询（进入加载 + 60s 静默刷新，区别于风控卡 5s）。ECharts 已按需注册，趋势图不增依赖。
- **安全/权限**：新端点含风控姿态，默认 operator 读；BFF 写规则 fail-closed（refresh/升级动作）；**评级页本身无任何改闸门入口**，升级走 Config 页两步确认，守"新闸门默认 inert、不自动改风控姿态"红线。
- **UX**：verdict 配色与现有 badge 一致；DATA_GAP 红与全站冻结横幅（kill_armed/global_halt）视觉语言统一。

---

## 4. 优先级与实施计划（按依赖与安全权重，非日历日期）

| 阶段 | 内容 | 进入条件 / 出口标准 |
|---|---|---|
| **M1 后端 API 铺路** ✅ **已落地 2026-09-07** | trader 侧 grades/refresh/history 3 端点 + shadow_grade 落历史快照；blind 事件补发 SSE；py_compile + 全量 pytest 只增不减 | 出口：curl 取到分级 JSON；blind 时 feed 有事件 — **已达成**（容器 6b029550aced 冒烟全通，2997 passed，详见文末附录 A） |
| **M2 BFF 接线** ✅ **已落地 2026-09-07** | proxy.py `_PATH_RULES` 登记新路径（读/写权限）；nginx 无需改（走 `/api/dashboard/` 已通） | 出口：门户带 token 经 BFF 取数；无 token 写被拒 — **已达成**（容器 5b8beae9d8b9 冒烟全通，门户 pytest 13 passed，详见附录 B） |
| **M3 影子臂评级中心页** ✅ **已落地 2026-09-07** | 新建 `/risk-arms` 页 + 路由/菜单/Store；blind 事件接 AlertPopup | 出口：12 臂评级、三窗统计、DATA_GAP 红横幅、PROMOTE 跳 Config 全可用 — **已达成**（容器 382797cfa3e3 冒烟全通，门户 pytest 14 passed，type-check/build 0 错，详见附录 C） |
| **M4 审计/对账（可并行）** | 台账哈希链卡片、reconcile 状态卡片 | 出口：链校验状态、对账差异可见 |
| **M5 数据积累后复盘** | grade-history 趋势图、Config 页臂 mode 联动；待 shadow 臂积够样本（PROMOTE/REVIEW）供人工拍板 | 依赖真实影子数据积累，非开发量 |

顺序强依赖 M1→M2→M3；M4 可与 M3 并行；M5 等数据。**M1/M2 是关键路径**。

---

## 5. 验收标准（可测）

**F2/F1 评级中心**
1. 页面加载后 12 臂全部显示 mode + verdict，与容器内 `shadow_grade.py` 文本输出逐条一致。
2. 某 shadow/enforce 臂 0 条记录时，该行红底 + 页顶横幅，且对应 SSE/飞书告警触发。
3. PROMOTE 行"去升级"跳转 `/config` 定位该臂；**评级页内不存在任何可直接改 mode/下单的控件**（inert 红线）。
4. viewer 角色看不到该页/接口 403；operator 可读、可点"立即刷新"。
5. grades 接口在 1MB+ 日志下 P95 < 500ms（读快照路径），连续刷新命中缓存。
6. closes=0 时顶部显示"无真钱对照"提示。

**F4 blind 告警**
7. 模拟 memory 门故障，门户 AlertPopup 在 10s 内弹 danger 且需手动关闭；重连不重复弹旧事件（复用 1.5s 回放/新鲜度过滤）。

**F5 哈希链**
8. 正常台账显示"链完整 + 事件数"；篡改一条 events.jsonl 后 verify 报断裂位置，页面红色告警。

**F6 对账**
9. reconcile cron 跑完后页面显示最近时间戳与 orphan/phantom 计数；未跑过显示"尚无对账"而非报错。

**全局**
10. 新端点全部只读默认、写操作 fail-closed；全量 pytest 基线只增不减；新菜单对无权限用户隐藏；移动端 768px 表格可横滚、触控目标 ≥40px。

---

## 附录 A：M1 落地记录（2026-09-07 完成）

> M1 阶段（trader 侧后端铺路）已全部交付并上线容器 **6b029550aced**（healthy）。本节回写实际实现与第 2/4/5 节规格的偏差，供 M2/M3 对接。

**A1. 实际交付端点**（代码：[shadow_arms.py](file:///home/ldy/hermes-trader/hermes_trader/dashboard_routes/shadow_arms.py)，注册于 [dashboard.py](file:///home/ldy/hermes-trader/hermes_trader/dashboard.py) `register_routes`，位于 shadow 路由之后、public SPA catch-all 之前）

| 端点 | 鉴权（实测） | 说明 |
|---|---|---|
| `GET /api/dashboard/shadow-arms/grades?windows=24,72,168` | **匿名可读**（与现有 shadow 读端点一致；RBAC 在 BFF/nginx 层收口） | `collect_grades()` 经 `asyncio.to_thread` + `_ttl_cached` **60s TTL**（singleflight）；windows 逗号分隔、1..2160h，非法 422；返回体附 `windows_req`/`cache_ttl_s` |
| `POST /api/dashboard/shadow-arms/refresh` | operator **write**（Bearer/X-Operator-Token + per-IP 限速），无 token 401 | 重算后暖写 TTL 缓存；审计事件 `shadow_arms_refresh`（含 `via:"web"`、verdict counts）；返回 `{"ok":true, ...report}` |
| `GET /api/dashboard/shadow-arms/grade-history?days=30&limit=400` | 匿名可读 | 读 `/data/shadow_grade_history.jsonl`，返回 `{snapshots, count, days}`；缺文件返回 `count:0` 不报错 |

- grades 鉴权与第 2 节"operator 读"的规格偏差：trader 侧读端点按现有 `dashboard_routes/shadow.py` 范式保持匿名（数据无密、只含风控姿态），**访问控制由 M2 BFF `_PATH_RULES` 收口**（viewer/operator 可见性在门户层判定）。refresh 写端点在 trader 侧即 fail-closed。
- grader 模块（scripts/shadow_grade.py，非包）经 `importlib` 懒加载，候选路径 `$HERMES_SCRIPTS_DIR` → `/app/scripts` → 相对 `../../scripts`；加载失败三端点统一 503。

**A2. 夜间历史快照**（[shadow_grade.py](file:///home/ldy/hermes-trader/scripts/shadow_grade.py)）

- 新增 `append_history`/`read_history`/`_slim_snapshot`/`_trim_history`：每晚 cron（08:45）main 路径在飞书推送后追加一行精简快照（只取最长窗 168h 统计），上限 400 行（~13 个月），超限保留最新；全部 best-effort 不抛。
- **API refresh 刻意不写历史**——防止人工点"立即刷新"伪造夜间趋势；历史只由 cron/main 路径产生。

**A3. blind SSE 事件**（[risk_gates.py](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py) `_alert_memory_gate_blind`）

- 事件 `risk_gate_blind`（字段 `gate/coin/posture:"fail-open"/error`）经 `session_log.append` 进入 feed（feed 即 session_log tail），5 个熔断门调用点全部覆盖。
- **operator-only**：不加入 `_PUBLIC_FEED_EVENTS` 白名单（匿名 feed 不泄露风控姿态，冒烟已验证）；不进 fork/notify 白名单（不重复发飞书、不写 events.jsonl）。
- 飞书卡片与 SSE 镜像各占独立 try 块：飞书故障不压制 Web 告警（测试锁定）。
- M3 前端对接：`mapTraderEvent` 增 `risk_gate_blind → risk_alert` 映射即可，无需新组件/新权限。

**A4. 闸门证据**

- 新增 tests/test_shadow_arms_api.py **15 例**（快照 6 + 端点 7 + blind SSE 2）；全量 pytest **2997 passed / 14 deselected**（基线 2982 净增 15，只增不减）。
- 容器内冒烟（真实 /data）：grades 返回 12 臂真实评级（ta_late_entry enforce/COLLECTING、sizing_v2 与 atr_regime_calib shadow/PROMOTE_CANDIDATE、pullback OFF）；refresh 401→200；windows 非法 422；cron 路径跑后 grade-history count=1；blind 事件 operator feed 可见、匿名 feed 不泄露。
- M1 已提交推送（trader 仓 commit **baf9b7a**，分支 feat/optimization-v3）。

---

## 附录 B：M2 落地记录（2026-09-07 完成）

> M2 阶段（hermes-portal BFF 接线）已交付并上线门户容器 **5b8beae9d8b9**（healthy，替换 e04389f2a2ae）。trader 侧零改动。

**B1. 路径规则**（portal 仓 `app/routers/proxy.py` `_PATH_RULES`，置于 risk-status 之后、影子账本 `/api/dashboard/shadow/` 之前）

```python
("/api/dashboard/shadow-arms", "operator:mode", "operator:mode"),
```

- **读/写统一 `operator:mode`**（仅 operator/admin 角色持有）：评级含各闸门 mode 姿态（off/shadow/enforce）与 DATA_GAP 盲信号，属敏感风控姿态，与 §3"含风控姿态默认 operator 读"、验收 #4（viewer 403）、blind SSE operator-only 一致。**未新增权限码**（评估第 2 节"可新增 risk:read"为可选，复用现有 operator:mode 更小改动、无需动 RBAC 种子/菜单）。
- 前缀用连字符 `shadow-arms`，与影子账本 `shadow/`（斜杠）`startswith` 互不误匹配——MATRIX 用 `/api/dashboard/shadow/book` 读回归锁定。
- BFF 转发时自动剥离客户端伪造凭据并注入 `X-Operator-Token`/`X-Internal-Token`/`X-Portal-User`（既有机制，未改）；trader 侧 refresh 的 operator write 校验因此通过。
- nginx 无需改动（`/api/portal/` 路由已通），与评估预判一致。

**B2. 前端对接契约（M3 用）**

- 经 BFF 的实际请求路径（浏览器同源）：
  - 读：`GET /api/portal/trader/api/dashboard/shadow-arms/grades?windows=24,72,168`
  - 读：`GET /api/portal/trader/api/dashboard/shadow-arms/grade-history?days=30&limit=400`
  - 写：`POST /api/portal/trader/api/dashboard/shadow-arms/refresh`，**body 为 JSON 且 `windows` 为逗号分隔字符串**（如 `{"windows":"24,72,168"}`，与 grades query 同形；传数组会被 trader 端 422）；空 body/`{}` 用默认窗口。
- viewer/trader 角色调任一影子臂接口 → BFF 直接 403（不触达 trader）；operator/admin 全通。菜单 `/menu` 本期未加项（M3 加 `/risk-arms` 页时用 `can("operator:mode")` 条件下发，仿"操作员控制台"范式）。

**B3. 闸门证据**

- portal 仓 `tests/test_proxy_rbac.py`：MATRIX 增 5 行（grades/grade-history/refresh 三端点 viewer/trader 403、operator/admin 200；影子账本 book 读四角色 200、reset 写 viewer/trader 403 回归）+ 单元断言 8 条（锁死 shadow-arms 读/写 = operator:mode，且不误伤 shadow/ 账本规则）。
- 门户全量 pytest（.venv）**13 passed** 全绿（假上游 ASGI transport，无真实网络）。
- 经 BFF（https 8443）生产冒烟：admin 登录后 GET grades 200 返回 12 臂真实评级、grade-history 200 count=1、POST refresh（正确 body）200 ok/12 臂、坏 JSON body 422；匿名 GET/POST 均 401。生产库仅 admin 用户，viewer/trader 的 403 由 RBAC MATRIX 锁定（未在生产造临时账号）。
- M2 已提交推送（portal 仓 commit **c319698**，main，2 files/26 insertions：proxy.py `_PATH_RULES` + test_proxy_rbac.py MATRIX；提交前已配置仓库级 git 身份）。M3 落地记录见附录 C。

---

## 附录 C：M3 落地记录（2026-09-07 完成）

> M3 阶段（影子臂评级中心页 + 菜单 + 盲信号弹窗）已交付并上线门户容器 **382797cfa3e3**（healthy，镜像 hermes-portal:latest 重建）。trader 侧零改动。

**C1. 改动文件清单**（hermes-portal 仓）

- 新建 `src/modules/operations/RiskArms.vue`：评级中心页（12 臂主表 + 三窗统计 + 成熟度 n/60 进度条 + DATA_GAP 红横幅/整行红底 + 真实成交基线条 + 每晚评级历史表 + 页尾 INERT 红线声明）。
- `src/router/index.ts`：operator 路由后新增 `risk-arms` 懒加载路由，`meta.perm = operator:mode`，beforeEach 守卫无权限回退 /overview。
- `app/routers/proxy.py` `menu()`：运维组 children 新增条件菜单项 `{"id":"risk-arms","label":"影子臂评级","path":"/risk-arms","icon":"ShieldAlert"} if can("operator:mode") else None`（图标复用 Sidebar 已有 ShieldAlert 🛡️，未动 Sidebar）。
- `src/shared/composables/useTraderEventBridge.ts`：`mapTraderEvent` 新增 `risk_gate_blind → risk_alert`（level=danger，手动关闭）映射，复用 alerts 种子已有 `risk_alert` code，**未改 seed.py**；trader 端该事件 operator-only（不在 `_PUBLIC_FEED_EVENTS`），无权限用户收不到。
- `tests/test_proxy_rbac.py`：新增菜单可见性测试 `test_menu_risk_arms_visibility`（viewer/trader 菜单不含 /risk-arms，operator/admin 含，/config 对照组不受影响）——补齐 /api/portal/menu 端点此前零测试。

**C2. 对接契约落实**

- 读：`GET /api/portal/trader/api/dashboard/shadow-arms/grades?windows=24,72,168`；历史：`.../grade-history?days=30&limit=400`；重评：`POST .../refresh`，body `{"windows":"24,72,168"}`（逗号分隔字符串，数组会 422），响应解构 `{ok,...payload}` 后直接渲染。
- 字段严格按 shadow_grade 契约：`arms[].windows[]`（total/hits/decisions/hit_rate/mature_outcomes/outcome_wins/losses/pnl_usd_sum）、verdict 六值（DATA_GAP/REVIEW/PROMOTE_CANDIDATE/INSUFFICIENT_DATA/COLLECTING/OFF）、`real_baseline.real_closes/real_win_rate/note`、slim 历史快照。成熟度 = longest-window total/60（MIN_SAMPLES_PROMOTE）。
- 页面轮询 30s + SSE 订阅（`risk_gate_blind`、`shadow_arms_refresh`，500ms 防抖刷新）+ onMounted 立即加载；onUnmounted 清理 timer/timeout/unsub。

**C3. INERT 红线四重落实**（评级器只评级 + 建议，绝不自动改配置/闸门/下单）

1. 页内无任何改 mode/闸门/下单控件；
2. PROMOTE_CANDIDATE 行"去升级 →"、REVIEW 行"去复核"仅 `router.push('/config')`，升级动作在 Config 页人工走 config_store 权威写路径；
3. operator 可见的"⚡ 立即重评"按钮仅调只读 refresh（重算建议，不写历史快照）；
4. 页尾固定红线声明卡。

**C4. 闸门证据**

- 前端：`npm run type-check`（vue-tsc --noEmit）**0 错**；`npm run build` 成功（dist/assets/RiskArms-*.js ≈11.9 kB）。
- 后端：门户全量 pytest **14 passed**（M2 的 13 + 菜单测试 1，只增不减）。
- 经 BFF（https 8443，admin/token）生产冒烟全通：
  - 菜单 /api/portal/menu 运维组 children = `['/operator','/risk-arms','/config']`，/risk-arms 对 admin 可见；
  - grades 200 返回 12 臂真实评级（Counter：OFF 5 / PROMOTE_CANDIDATE 3 / INSUFFICIENT_DATA 3 / COLLECTING 1，generated 2026-09-07 16:09 UTC；本轮无 DATA_GAP/REVIEW）；
  - **real_baseline.real_closes = 15**（M2 冒烟时为 0，现有真钱对照，基线卡显"有真钱对照"）；
  - refresh 200 ok=true / 12 臂；grade-history 200 count=1；匿名访问 401；
  - 静态资源新版本 hash（index-*.js 更新）证明新包已上线。
- viewer/trader 的 403 与菜单不可见由 RBAC MATRIX（M2）+ 菜单可见性测试（C1）锁定（生产库仅 admin，未造临时账号）。

**C5. 验收对照**（第 5 节 #1-7）

- #1 12 臂 mode+verdict 与 shadow_grade 输出一致（冒烟逐条核对）；#2 DATA_GAP 红横幅+整行红底（本轮无 DATA_GAP 臂，渲染逻辑按 verdict=DATA_GAP 触发，blind SSE danger 弹窗链路已通）；#3 PROMOTE 跳 /config 且页内无改闸门控件（C3）；#4 viewer 403/菜单不可见、operator 可读可重评（MATRIX+菜单测试）；#5 grades 走 60s 快照缓存读路径；#6 closes=0 时显"无真钱对照"warn（当前 closes=15 显对照正常）；#7 blind → danger 弹窗需手动关闭（复用 alerts 既有去重/新鲜度机制）。
- 历史趋势图（grade-history 折线）按计划留待 **M5**（数据积累后），页面历史区已注明"趋势图在 M5 补齐"。
