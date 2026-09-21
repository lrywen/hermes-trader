# 思路4：funding 数据独立交叉核对（等价 ScalarField）SCOREBOARD

> 日期：2026-09-21（Asia/Shanghai）
> 授权：用户「按思路3起草预注册 ADR，再执行思路4的免费数据交叉核对，用 ScalarField 验证 funding」
> 关联：[ADR-0005](../../adr/0005-pd-v2-preregister-carry-threshold-vol-gate.md) §5
> 性质：**只读取数对账，未修改任何缓存/配置，未下单**。

## 裁定：PASS —— 本地 funding 缓存与独立取数在全部重叠时段逐值一致，分页/存储代码无 bug。

## 一、方法与一个关键事实

- ScalarField 的 `getHyperliquidFunding()` **底层即 HL 官方 `fundingHistory` 端点**
  （据其[数据来源说明](https://scalarfield.io/docs/market-data/hyperliquid-data)），与项目自采
  funding 是**同一个第一方数据源**，且该函数经由需平台账号的 `scalarlib` 提供。
- 因此用一条**与原分页采集器完全独立的最小取数代码**（httpx 直连 `https://api.hyperliquid.xyz/info`，
  独立翻页逻辑）取数对账，既等价于 ScalarField 核对，又不引入未审计依赖。脚本 `/tmp/sf_funding_xcheck.py`，
  明细 `/tmp/sf_funding_xcheck.json`。
- **同源的含义（局限）**：本核对只能验证「自采/分页/存储代码是否忠实复制了 HL 数据」，
  **不能独立验证 HL funding 数值本身的真伪**——那需要真正异源的第三方 funding（后续 ADR）。

## 二、核对设计

- 6 币：BTC / ETH / SOL（大币）、DASH / ZEC（短史/晚上线）、ZRO（本地回溯最老，2024-08-31）。
- 3 个 72h 历史窗：far(2024-09) / mid(2025-06) / recent(2026-08)，覆盖分页拼接的远/中/近边界。
- 对账：time 按整点对齐（忽略 ~31–77ms 偏移），fundingRate 转 float **逐值比较**；
  并区分「本地覆盖范围内的缺失」与「本地回溯未到的范围外时点」。

## 三、结果

| 币 | far | mid | recent |
|---|---|---|---|
| BTC | 范围外72 / 不符0 | 范围外72 / 不符0 | **72 全对** |
| ETH | 范围外72 / 不符0 | 范围外72 / 不符0 | **72 全对** |
| SOL | 范围外72 / 不符0 | 范围外72 / 不符0 | **72 全对** |
| DASH | 上线前0 | 上线前0 | **72 全对** |
| ZEC | 上线前0 | 上线前0 | **72 全对** |
| ZRO | **72 全对** | **72 全对** | **72 全对** |

**汇总：本地覆盖时段内 缺失=0、数值不符=0、请求错误=0 → PASS。**

## 四、覆盖范围事实（非缺陷）

- BTC/ETH/SOL 本地缓存各 8760h，窗口 **2025-09-20 → 2026-09-20（1 年）**；故 far/mid 窗
  属本地回溯范围之外（远端有、本地采集时本就只取 1 年），不计为错误。
- ZRO 本地 18000h 起自 2024-08-31，三窗全部在覆盖内、逐值一致。
- DASH/ZEC 在其上线时点之前，远端正确返回空，与本地一致。

## 五、对后续的意义

- **排除了「P-D/P-A 结论可能是 funding 采错/分页丢值所致」这一假设**：自采 funding 忠实于 HL。
- P-D-v2（ADR-0005）可在可信 funding 上继续；其信号仍需的**现货真实成交价/历史 L2 深度**
  不因此消除（ScalarField 同源、且本轮未取订单簿历史），留待实现阶段按 §4 门槛处理。
- 若未来需要对 funding 做**异源**校验，应接入真正独立的第三方（多所聚合，如 Loris 的跨所 funding），
  而非 ScalarField 这一同源通道。
