# R7 工单：agent-config 配置校验 Pydantic 化（F27 续）

- 审计标注：Audit 2026-09-03 P2-8
- 前身：F27（2026-08 综合问题根因清单）
- 当前状态：**核心已落地，剩余为嵌套块深校验收尾**

## 1. 背景与目标

`.agent-config.json` 的配置键类型、默认值、取值范围早期分散在多处：
`config_store.py` 的手写类型表、`config_schema.py` 的手写范围表、各消费模块里的
`.get(key, 字面量默认)`。F27 要求收敛为"单一事实源"：类型看注解、默认值看
`Field(default=...)`、范围看 `ge=/le=/gt=`。

## 2. 已完成部分（代码实证）

### 2.1 标量键已全部 Pydantic 化

`hermes_trader/agents/config_schema.py`：

- `_ConfigPatch(BaseModel)`（L35），`model_config = ConfigDict(extra="allow")`
  —— 未知键原样透传，白名单检查交给 `validate_config_updates`，不破坏旧合并端点。
- 标量键按类分区声明，默认值全部从 `CANONICAL_DEFAULTS` 派生（不再写死字面量）：
  - 字符串/布尔：L48-70（mode、enable_crypto、trend_surface_enabled、
    auto_flatten_on_global_halt、roe_halt_enabled 等 20 个）
  - 整数：L73-87（leverage `ge=1,le=50`、cooldown_min、news_cache_ttl_s 等 16 个）
  - 浮点：L90-135（equity_fraction_per_trade `gt=0,le=1`、sl/buffer/阈值类 30+ 个）
  - 列表：L138-143（coin_allowlist/blocklist、conviction_tiers 等）
- 范围边界从 Pydantic 字段元数据反射，范围表不再手工维护（模块 docstring L9-13）。
- `validate_config_updates` **刻意保留历史严格 isinstance 接受矩阵**
  （bool 不被接受为 int/float、字符串不强制转换），范围消息表由字段元数据反射；
  历史文案的 4 个键（leverage/max_concurrent/min_ai_confidence/
  equity_fraction_per_trade）在 `_SPECIAL_RANGE_KEYS`（L268）中保留原文案。

### 2.2 嵌套块：默认值已收敛，校验分两级

- 35+ 个嵌套块在 `_ConfigPatch` 中以 `dict[str, Any]` 声明
  （L146-263：dsl_exit、atr_risk_sizing、regime_classifier、research_llm、
  research_fetch、scan_budget、http_cache、hl_client_io、ta_late_entry 等），
  默认值经 `_dict_default()` 从 `CANONICAL_DEFAULTS` 深拷贝。
- 其中**已有 4 个块**在 `_NESTED_BLOCK_SPECS`（L420 起）提供**叶级深校验**：
  - `atr_risk_sizing`（_ATR_RISK_SIZING_SPEC L394）：enabled/bool、
    risk_per_trade_pct 0-1、sizing_basis 枚举（primary_stop/dsl_stop/atr_stop）、
    sizing_v2_enabled/bool、sizing_v2_cap_pct 0-1、coin_overrides.sl_floor_pct 0-50。
  - `dsl_exit`（_DSL_EXIT_SPEC）。
  - `signal_enforcement`（_SIGNAL_ENFORCEMENT_SPEC L409）：veto/boost 开关 +
    whale 金额上限 0-10^9。
  - `ta_late_entry`（L424）：mode 枚举（off/shadow/enforce）+ RSI/ext 阈值 +
    趋势例外放宽阈值。

### 2.3 防漂移机制

- 研究链路新增旋钮有 drift sentinel 测试锁死：
  `tests/test_r13_b10_research_llm_fetch_registration.py` 逐叶比对
  research.py 字面量 ↔ `CANONICAL_DEFAULTS["research_llm"]` ↔ 测试期望
  （叶数、逐叶值、类型、SPEC env 映射、kind、guard）。
- R13-B 系列（B1-B13）已把 scan/research/HTTP/HL 客户端/内存质量等运行期旋钮
  全部登记进 canonical，dashboard 配置转储与 validate 端点均可见。

## 3. 剩余范围

| # | 内容 | 说明 |
|---|------|------|
| R7-1 | 其余 ~30 个嵌套块的叶级 SPEC | 目前它们以 `dict[str,Any]` 接受、消费端用稀疏 `.get`；错误类型/越界叶值要到运行时才暴露。参照 `_ATR_RISK_SIZING_SPEC` 模式逐块补 `_NUM_leaf/enum/bool/dict_of` 即可，模式已固定。 |
| R7-2 | 嵌套块漂移 sentinel 推广 | r13_b10 的逐叶镜像测试目前只覆盖 research_llm/research_fetch；可推广为"每个有 SPEC 的块都断言 canonical 叶键集合 == SPEC 叶键集合"，防止新增旋钮漏登记。 |
| R7-3 | 配置写入前 dry-run 校验端点 | 现 validate 端点已存在；可补一个"仅校验不落盘"的 CI 钩子，部署前对目标 .agent-config.json 跑一次全量 SPEC。 |

不做的部分：嵌套块内的**业务联动校验**（例如 dsl_exit.max_loss_pct 与
max_loss_roe_pct/leverage 的联动、sizing_basis=dsl_stop 时 dsl_exit 必填）属于
策略语义，超出配置类型校验范畴，留在策略文档中约束。

## 4. 工作量与优先级

- R7-1：按块独立、可并行；每块约 10-30 行 SPEC + 对应测试。优先级中——
  当前消费端 `.get` 默认值已安全（错误配置不会崩，只是不生效），属健壮性而非正确性。
- R7-2：小工作量高价值（一次防一类漏登记），建议优先。
- R7-3：部署流程增强，优先级低。

## 5. 依赖项

- 无外部依赖；纯 `pydantic`（已在依赖中）。
- R7-1 每块 SPEC 的默认值必须以 `CANONICAL_DEFAULTS` 为准（单一事实源）。

## 6. 验证方式

```bash
python3 -m pytest tests/ -q                       # 全量 1892 passed
python3 -m pytest tests/test_r13_b10_research_llm_fetch_registration.py -q
# 手动：dashboard 配置转储端点可见全部 canonical 键；
#       向 validate 端点提交越界值（如 leverage=999）应返回范围错误。
```
