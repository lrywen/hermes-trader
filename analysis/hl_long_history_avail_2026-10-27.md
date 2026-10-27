# HL 长历史可得性核查（2026-10-27，只读实测）

目的：判断能否用**历史数据**把 panic-gated xs 臂在 Hyperliquid 上的门内独立信号日
从当前 14（< 自设确认线 20）补齐，而不必只靠部署后实时影子采样。

## 实测结论：当前环境无法补齐，硬约束是数据源 + 付费 egress，不是方法

| 来源 | 可回溯 | 粒度 | 成本/门槛 | 实测 |
|---|---|---|---|---|
| HL 官方 info API `candleSnapshot` | **仅约 2026-04 起** | 1h/1m 等 | 免费、可直连 | 显式请求 2024-01 / 2025-01 / 2025-09 窗口均 `n=0`，不能翻页取更早 |
| HL 官方 S3 `hyperliquid-archive` | 月更、不保证完整 | 主体 L2/fills，蜡烛稀少 | **requester-pays**，需 AWS 账号付 egress | 本机无 AWS 凭证 |
| Hydromancer Reservoir（第三方最全） | HL crypto perp **2025-08 起** | candles 仅 **1s**（可聚合成 1h） | **requester-pays** 周 parquet；无 1h 成品 | 即使付费也只到 2025-08，非跨年；体积大，本机无凭证/parquet 工具链 |

网络本身可达（api.hyperliquid.xyz TLS/POST 正常），瓶颈纯粹是**历史深度**与
**requester-pays 授权**。Binance 3.4 年 1h 仍可经 `data-api.binance.vision` 免费取得，
但那不是生产成交场所，只能作跨源旁证，不能替代 HL 统计确认。

## 对 S2 promote 判定的影响（2026-10-27 更新：独立日不是瓶颈，独立恐慌事件才是）

把本机 HL 缓存从 BTC/ETH/SOL 三币扩到**全部 81 币**后，门内独立信号日从 14 增到
**80**（pctile 门，449 episode）——所以"独立日只能靠部署后实时累积到 20"的旧说法
**作废：用历史就能补过日数线**。详见
`analysis/xs_hl_81coin_effective_sample_2026-10-27.md` 与
`scripts/xs_hl_panic_event_cluster.py`。

但去相关（同日多币=1 事件、相邻 ≤3 个活跃日合并为同一波 72h 恐慌）后：

- naive 按笔 449 在 HL 上 **≈0/略负（t−0.03）**，"有量"是横截面相关的方差假象；
- 208 天里只有 **3 个真正独立的全市场恐慌事件**（gap3d 口径），事件级 t=1.17、
  CI 极宽；**没有任何口径 t≥2**，2026-06 还为 −151bp。

因此真正的硬约束是**独立恐慌事件数 / 时间跨度（不同 regime）**，不是独立日数或
币种数：补小币、攒同日信号都无法解决，只能靠更长时间。当前处置不变：

1. `mode=shadow`、`enforce` no-op、panic gate 默认关，不接下单；
2. promote 的跨源判据改为"**≥10 个去相关独立恐慌事件（gap3d）、事件级 t≥2、
   CI 下界>0**"，当前 HL=3；
3. 累积方式二选一：(a) 部署后 record-only 影子随真实时间累积不同恐慌 regime；
   (b) 提供 AWS/egress 凭证，从 Reservoir 拉 2025-08 起 1s parquet 聚合成 1h
   （只加约 8 个月、仍非跨年，能增加但未必补足独立事件数）。

Binance 3.4 年（多轮恐慌 regime、事件级显著）仍是 edge 主证据；HL 仅方向一致的
旁证，不构成生产场所统计确认。

