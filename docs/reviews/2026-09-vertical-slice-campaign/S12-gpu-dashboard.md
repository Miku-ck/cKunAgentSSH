# S12 · GPU 仪表盘与服务监控（⚪ 低产出切片）

锚点 `a8e588c`。核验图例：✅ = 我本人证实（含实跑测试）；◐ = 采信引用未二次打开。
本切片按计划在裁决密集区**只做确认 + 报新增**，结论是：这一面基本干净。

## 实跑证据

`npx vitest run src/components/ssh/NvGauge.test.ts` → **28 passed**，195ms，RC=0（纯前端、不触库，可安全单跑）。
`git diff a8e588c --stat` 对 `NvGauge.ts` / `NvGauge.test.ts` / `server-monitor-core.ts` → **空**（与锚点逐字节一致）。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 核验 |
|---|---|---|---|---|---|
| S12-01 | — | 正例 | **记忆条目过时**：`NvGauge` 的「66/90 红测试欠账（工作区已修未提交）」已结清并入库——阈值实现与用例现同为 60/80，28 条全绿 | `NvGauge.ts:5,36` vs `NvGauge.test.ts:111-116` | ✅ |
| S12-02 | b | Minor | 「监控目标会话不可用」在**主进程正在为该会话重连**时仍被标为不可重试，UI 于是隐去「连接恢复后自动重连」的预期 | `ssh-error-core.ts:54-66`（`SESSION_NOT_FOUND` 不在表内）→ `server-monitor.ts:121` → `DashboardView.vue:13,26` | ◐（同 S0 的 S0b-7，此处为唯一条目，勿重复计） |
| S12-03 | d | Minor | 编排层与展示层零测试：`GpuNvtopPanel.vue`(662) 与 `DashboardView.vue`(582) 无任何用例，纯核心（`server-monitor-core.ts` 382，31 例）覆盖了解析却覆盖不到轮询编排/取消/失败停机三件套 | `src/stores/server-monitor.ts`(247，31 例经 core 间接) | ✅ |

## 确认在位（不复报为发现）

- **10s GPU 轮询超时「临界」的旧担忧已不成立**：`server-monitor.ts:130-133` 是 `sshExec(..., 10)` 外套 `Promise.race(…, 12000, 'metrics poll timeout')`，且 `:165` 的 `finally` 必释放 `inflight`；`:128` 的注释正是为「inflight 永久为 true 导致后续轮询全跳过」写的。
- **`inflight` 不在 `stop()` 重置**：命中 91 清单第 1 条（在 stop 里 reset 是**错的**），本轮**未**复报。
- 跨会话脏写守卫在两处采样返回点都在位：`:134` 与 `:156` 的 `activeSshId !== session.id` 早退，`:192-193` 的「采集期间用户可能 stop()/切会话 ⇒ 不得复活轮询」。
- `describeMonitorError` 的兜底路径**先剥 Electron IPC 信封**再展示（`:145-147`）——这条与 S0-13 互为反证：团队知道信封形态，`client.ts:69` 那条腿没剥。
- 解析层对 `N/A` 的处置有纪律：`naNum/naStr`（`:205-221`）把「被动散热卡无风扇 / WSL 无功率」如实建模为 `null`，`GpuSample` 逐字段注释了 null 语义（`:38-74`），`gpuError` 与 `gpus=[]` 联合区分「无卡」与「采集失败」（`:105-106`）。这是本轮少见的把「未知」和「零」分开的正面样本。

## 未覆盖声明

`GpuNvtopPanel.vue`(662) 与 `DashboardView.vue`(582) 的模板/样式/本地状态未逐行审（⚪ 权重 + 全 `.vue` 零测试的既定现实 + 09-24 双轴轮已扫过一批组件面）；`utils/nvtop-scale.ts`(158，31 例) 与 `nvtop` 采集命令的远端 shell 兼容性未压测（需真机多卡）。
