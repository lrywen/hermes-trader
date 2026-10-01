# 2026-09 研究材料归档索引

> 归档日期：2026-09-19（Asia/Shanghai）
> 性质：W0 冻结门（M0）的研究依据归档，纳入版本管理，供后续 W1–W5 回查。

## 现存基准文档（本目录）

| 文件 | 行数 | 说明 |
|---|---|---|
| `hermes-trader_统一技术改造文档_2026-09-19.md` | 1,055 | 41 项任务 A–G / G1–G3 任务书；附录 A 是 19 份报告的去重整合索引 |
| `Hermes_改造缺口清单_未做不做做错_2026-09-18.md` | 322 | 核对基线 `e9648f9` 的「未做/不做/做错」清单 |

## 19 份调研报告的状态说明（重要）

统一技术改造文档由 **19 份分散调研报告（合计 8,394 行）去重整合**而成。
2026-09-19 W0 归档时对整台主机（`/home/ldy` 全树、运行中容器、docker 卷、
全部 git 分支历史、`/tmp`、编辑器留痕）做了只读检索，**19 份原始报告实体
均已不可得**——它们在 2026-09-16 前后生成于临时目录（`/tmp/bt_out*`、容器
`/data/bt_out_*`），随后随临时目录清理而丢失。

其**结论并未丢失**：每份报告的一句话结论、有效性标注（✅ 有效 / ⚠️ 部分作废
/ ❌ 作废）、权威源归属均已整合进
`hermes-trader_统一技术改造文档_2026-09-19.md` 的**附录 A「19 份报告索引」**。
后续改造以该整合文档 + 本仓库 `docs/refit_plan_v2_2026-09-19.md`（方案 v2.0）
为准；引用 19 份报告时回查统一文档附录 A，不再依赖已不存在的原件。

| # | 报告（标题） | 整合后状态 | 权威用途 |
|---|---|---|---|
| 1 | Sprint0_可行性闸门与数据缺口 | 部分有效（成本口径被 P5-1 更正） | 样本量需求 |
| 2 | P1_数据源一致性与代理失真 | ✅ 有效 | 数据源/迁 1h |
| 3 | P2_信号edge归因 | ✅ 有效 | 1h/4h alpha |
| 4 | P2-3_空头侧修正 | ✅ 有效 | 做空无 edge |
| 5 | P3_edge窗口识别器 | ✅ 有效 | 三阶段失败 |
| 6 | P3-2_事件级复盘 | ✅ 有效 | 择时 alpha 季节集中 |
| 7 | P3-3_事前可观测判别条件 | ⚠️ 可实施性被 P3-4 推翻 | — |
| 8 | P3-4_gate完整绩效 | ✅ 有效 | gate 不上线 / maxc=2 |
| 9 | P4_exit侧止损分析 | ✅ 有效 | 放宽止损非解 / noise_band |
| 10 | P5_出场引擎代价分解 | ⚠️ 绝对值被 P5-1 更正 | — |
| 11 | P5-1_成本口径校正 | ✅ **成本口径权威源** | 9.26 bps / 18.84 损耗 |
| 12 | P5-1bd_成本常数落上游 | ✅ 有效（已 commit 于 P5-1bd） | flat 0 偏差自检 |
| 13 | P6_资金量vs盈利模式冲突 | ⚠️ §2 整章作废 | — |
| 14 | P6b_配置真相与实盘对齐 | ✅ **配置层权威源** | filt_ra maxc=2 −19.21% |
| 15 | P6b1_回测与实盘出场机制正交 | ❌ 头条因果作废（侦察数据有效） | — |
| 16 | P6b1c_交易所触发真实机制 | ✅ **机制层权威源** | exchange_trigger = floor 镜像 |
| 17 | 0.3_影子证据SCOREBOARD | ✅ 有效 | 9 条动作清单 |
| 18 | 0.4_ADR-001_配置冻结与三不变量 | ✅ 有效 | 配置冻结纪律 |
| 19 | 未改造项复核清单 | ✅ 有效 | 41 项任务索引 |

## 阶段 SCOREBOARD（改造过程产出）

| 文件 | 阶段 | 内容 |
|---|---|---|
| `w1_scoreboard_2026-09-19.md` | W1 | D-1/D-3′/D-6/F-1 只读取证收口（维持不转正，被 D-7 阻塞） |
| `b3_81coin_scoreboard_2026-09-19.md` | W2/B-3 | 81 币 318,195 笔出场占比；filt/filt_exch 对齐实盘，filt_ra 被否证 |
| `w3_blockers_scoreboard_2026-09-20.md` | W3 | A-1~A-6 阻塞性澄清结论（max_loss_pct=1 / maxc=2 / [atr] 公式 / held 语义 / DOT cloid / TP 分批缺口） |
| `g2_loss_ledger_2026-09-20.md` | W3/G2 | 五段损耗瀑布（峰值毛利→出场回吐→滑点→手续费→净，加性残差 0）；L1 回吐占 88%，指向结局 B |
| `w4_robustness_scoreboard_2026-09-20.md` | W4 | C/E/F 裁定 + bps 按天块自助；四臂 95%CI 全负不含 0，机械落定**结局 B** |
| `c7_higher_timeframe_no_go_2026-09-20.md` | C-7 | 1h 出场独立评估，全样本/剔6月/剔9月四臂 CI 全负，**NO-GO**；瓶颈是 L1 出场回吐非成本 |
| `pa_carry_funnel_closed_2026-09-20.md` | P-A | funding carry alpha 真实显著（5.83bps/天 CI不含0、中位年化8%、81币无负），但 perps beta 不可对冲（β自相关0.13、加权后−37%），**关闭转P-C** |
| `pc_structured_reversal_no_edge_2026-09-20.md` | P-C | funding极值反转事件级无增量 edge（毛中位0、净CI含0/为负），小时级+16bps 是连续run中后期前视错觉，**否证关闭** |
| `pd_basis_preregistered_no_go_2026-09-21.md` | P-D | 现货+空perp基差经预注册单一60天确认证伪：260笔中位+39.5但均值−43、CI[−74.8,196.4]下界不>0，**零成本下均值仍−19.8**（funding反转肥尾主导，非执行问题），**否证关闭** |
| `w6_leftovers_closure_2026-09-21.md` | W6 | v2.0 遗留项收口：B-6 币池一致性脚本、B-7/B-10 source-gated 启动守卫、B-8 选档日志、B-11 atr_stop deprecated（B-9 经查已实现），**D-7 外部平仓聚合回填修复 A-6 分批 TP 漏记**；+13 测试，全量 4610 passed |
| `w7_guard_leverage_costtable_2026-09-21.md` | W7 | 回测内核 guard 补**杠杆一致性(=10)+逐币成本表齐全度**两守，接入 bt_ra_exch 调度 chokepoint（fail-closed）；B-12/13 仓库早完成待重建镜像、A 类高风险解构明确不做；+19 测试，全量 **4629 passed** |
| `funding_independent_xcheck_pass_2026-09-21.md` | 思路4 | funding 独立交叉核对（直取 HL 官方 fundingHistory，等价 ScalarField 同源通道）：6币×远中近三窗，**覆盖时段内 0 缺失/0 数值不符 → PASS**，排除"结论由采数错误导致"；同源局限与异源校验路径已记录 |
| `pdv2_no_same_venue_spot_no_edge_2026-09-21.md` | P-D-v2 | 高阈值 carry(≥4%)+波动率闸门 81币回放：**HL 81 perp 币无同场现货对、跨所basis噪声主导(−4.3bps年化放大)、99.9%提前出场**；30d独立块 CI 显著负[−5.23,−4.05]、剔6月仍负 → **Deprecated**。HL perp 1h 数据资产81币208d |
| `t02_cpcv_dsr_confirm_outcome_b_2026-09-21.md` | T-02 | 新增 validation 包（CPCV+DSR+PBO，纯标准库）：b3 四臂 **15条OOS路径SR>0占比全0、中位OOS负**，最优夏普本为负、DSR P=0.0000 → 三方法一致确认 OUTCOME_B；+17 测试，floor 4646 |
| `outcome_b_signoff_2026-09-21.md` | sign-off | 结局B+四范式(C-7/P-A/P-C/P-D/P-D-v2)证伪的**人工接受记录**；并新增统一验证 CLI `scripts/validate_outcome.py`（一次跑全三方法、二值判定），在 b3 复现与单脚本一致 CI |
| `watchdog_deadman_and_dedup_2026-09-21.md` | 风控加固 | block-bootstrap 收敛为共享 `block_bootstrap_ci`（两脚本复用、CI字节不变）；补齐风控第4层**独立 dead-man 看门狗** `hermes_trader/watchdog.py`（供外部调度，STALE告警+紧急平仓钩子，默认不平仓）；+12测试 floor 4661，全量4661 passed |
| `m1_l2_book_capture_prereg_2026-09-28.md` | M-1 | K线+funding 四轮失败后转向**扩信息集**：L2 order book 采集已部署（Top20/1s/10档/集成容器，book-raw 保留14天）；并在看数前**预注册** OFI 主假设（W=60s,h=5m）、按时间块 bootstrap、CI下界>0 且净均值≥3bps/笔，首次检验不早于 2026-10-28、60天确认不早于 2026-11-27；明确当前无 tape、CVD 仅作 exploratory |
| `m2_binance_tape_cvd_prereg_2026-09-28.md` | M-2 | 换 venue 走历史：`/mnt/tick` 的 **Binance U本位永续逐笔成交**补齐为 BTC/ETH 各 **270 天连续（2026-01-01→09-27）零缺口**，含 is_buyer_maker 可算真 CVD；看数前**预注册**极端净流 H1（W=60s,h=5m,前/后50%天切分,固定成本11bp round-trip）、按天5天块 bootstrap、CI下界>0 且净均值≥3bps/笔，H2 CVD-价格背离作次要；明确不跑扫参、迁回 HL 需另开假设 |
| `m2_binance_tape_cvd_result_2026-09-28.md` | M-2 结果 | test 135天 n=15,756：净 **−11.04bp/笔 ≈ 纯成本**（毛利≈−0.04bp，方向预测力≈随机），9/15bp、单币、1/10天块全部 CI 为负不含0 → **H1 CLOSED**；确定性清洗 44 天微秒时间戳；第五类公开信号（tape/CVD）结案，edge 仍待不可回溯数据（M-1 OFI / 扩 venue） |
| `m3_metalabel_filter_prereg_2026-09-30.md` | M-3 | "模拟人工盯盘"的可复制部分=入场**选择性**：不预测方向，用 ≤5 个入场时刻特征（score/触发数/ADX/RSI/extension）训练浅 go/no-go meta-label 过滤层；切分点 2026-06-17，模型由训练段时序CV选定，τ 训练段定，按天块 bootstrap；目标仅减损，不预设转正 |
| `m3_metalabel_filter_result_2026-09-30.md` | M-3 结果 | test 7,216笔/96天：训练CV AUC 仅 **0.5225≈随机**，τ≈0.49、保留99.9%（等价不过滤），Δ=+0.00 CI[−0.05,+0.05]，GO净−7.32bp CI全负 → **H1 CLOSED**。累计第六轮：方向/出场/入场选择三层在公开信息集上均证伪；"盘口感"属微结构信息，归 M-1（2026-10-28） |
| `m4_bookdepth_imbalance_prereg_2026-09-30.md` | M-4 | 不等 M-1 30天，走 Binance 免费历史：bookDepth **30s帧**、12档（±0.2/1/2%，非真逐档L2），检验近mid ±0.2% 名义额失衡对5m收益的预测力；BTC+ETH、179天、切分03-31、阈值仅训练段估、扣11bp往返、按天块bootstrap |
| `m4_bookdepth_imbalance_result_2026-09-30.md` | M-4 结果 | test 89天/49,556桶：gross方向收益仅 **+0.07bp≈随机**，go 8,965笔净 **−10.93bp** CI[−11.25,−10.60]，middle带无差异 → **H1 CLOSED**。第七类公开信号（深度总量）结案；不代表真OFI(M-1)失败，但强化"公开可回溯数据edge=0" |
| `m5_limit_vs_taker_ab_prereg_2026-09-30.md` | M-5 | 信号触发后不立刻市价追、改 post-only 限价回调 r=10bps（W=15m超时不追、往返6.5bp）能否减损；用 Binance 逐笔精确判成交，同信号同持有 H=15m，切分03-31，看数前冻结四门槛（B′≥A且ΔCI≥0、成交率≥50%、踏空机会成本≤1bp、A≥300） |
| `m5_limit_vs_taker_ab_result_2026-09-30.md` | M-5 结果 | test 171天/2153笔：成交率63.2%，B′−4.38 vs A−9.87（Δ+5.49显著）但**踏空单机会成本+7.57bp**（系统性错过不回调的最强趋势单）→ R3 FAIL，**KEEP TAKER**。表面减损是选择偏差；执行层不改，唯一待验 M-1 OFI（2026-10-28） |
| `m6_hold_horizon_decay_prereg_2026-09-30.md` | M-6 | 零预测自由度出场诊断：同一冻结信号/成本/切分，隔离纯 time-based exit，H∈{5,15,30,60,120,240}m 看数前列死，test唯一报告净bps衰减曲线，只判形状不在样本内选最优H |
| `m6_hold_horizon_decay_result_2026-09-30.md` | M-6 结果 | test 945笔：净bps全horizon≈−9bp平坦（相邻|Δ|≤0.85、CI全重叠），**无短H集中/长H回吐** → time-based exit 不做，缩短持有期救不了。过程中修复b1前视bug（曾造假衰减曲线），H15与M-5交叉对账一致。累计：改平仓时刻也无效，毛利≈0亏的全是成本 |
| `m7_signal_significance_prereg_2026-10-01.md` | M-7 | 零自由度决策闸门：5m/15m/1h 各自检验动量方向在下一bar去趋势回报上的stationary bootstrap p值+毛利/净bps，分多空，决定周期该不该上移 |
| `m7_signal_significance_result_2026-10-01.md` | M-7 结果 | 9格中**唯一净正=1h多头**（毛利+11.59bp、净+2.59、p=0.0375）；5m多空≈0、做空全周期为负（1h空−10.4/p0.98）→ **主周期上移1h、只做多、静态关空**。薄edge需波动率目标+出场结构，新数据再确认 |
| `m8_h1_long_exit_curve_prereg_2026-10-01.md` | M-8 | 1h多头信号出场诊断：H∈{1,2,4,8,16,24,48}h 看数前列死，净bps+stationary bootstrap CI，只判形状不选最优 |
| `m8_h1_long_exit_curve_result_2026-10-01.md` | M-8 结果 | n=182：净bps随持有累积（H1+2.6→H4+14.9→H16/24+38峰→H48回吐+28），**真趋势跟随** → 让利润奔跑+trailing在1-2天保护，不做固定短持有；峰值CI宽不选16h |
| `m9_vol_target_sizing_prereg_2026-10-01.md` | M-9 | 零方向自由度风控：1h多头固定H=8h，对比 U/V20/V40 三档定尺（w=target/sigma，trailing30d，cap2），看数前列死不扫参 |
| `m9_vol_target_sizing_result_2026-10-01.md` | M-9 结果 | test134笔：**V20 Pareto最优**——回撤−30%→−15%减半、ES−3→−1.24，同时收益2.4%→4.8%、Sharpe0.22→0.42；V40≈加杠杆U未降回撤。**采纳20%目标波动**，新数据再确认 |
| `m10_integrated_candidate_prereg_2026-10-01.md` | M-10 | 整合 1h多头 + 硬止损−10/门槛+2/Chandelier回撤5%/最长120h，三臂 BASELINE(固定H8,w1)/TRAIL(trailing,w1)/FULL(trailing+V20)，前向单仓状态机严格非重叠 |
| `m10_integrated_candidate_result_2026-10-01.md` | M-10 结果 | **否证**：TRAIL−27.5%/FULL−13.4% 远劣于 BASELINE+0.8%。M-8收盘+38bp无法用盘中trailing实现（5%盘中回撤太常见、被扫在局部低点）→ 固定trailing不切换；1h多头edge薄、仅作M-1方向载体 |



## 找回原件的可选途径（备查，非必须）

- 09-16～09-19 的宿主级备份/快照（归档时主机未见此类快照）；
- Trae 云端会话记录导出。
