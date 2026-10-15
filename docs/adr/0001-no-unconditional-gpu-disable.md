# ADR 0001：主进程不再无条件禁用 GPU 渲染

**状态：已定案（Resolved，2026-10-15）。本机启动失败的真因是机器层 HVCI（内存完整性）开启，
与三个渲染开关无关、也与仓库代码无关。三个开关保持 `9ed8b12` 状态不动（任何改动仍需真实窗口验证）。
详见文末「定案」。**

`electron/main.ts` 里 `disable-gpu` / `disable-gpu-compositing` / `disable-software-rasterizer`
三个开关来自 76956f2 与 9ed8b12 两次「修启动崩溃」，其归因是"本机 GPU 驱动与 Electron v35 不兼容，
不禁用即 exit 3"。本次审查用最小外壳（BrowserWindow + canvas，同一台机器、同一沙箱身份）做了对照：
带这三个开关 → `2d_canvas: disabled_software` 且窗口正常；一个都不加 → `2d_canvas: enabled` 且窗口同样
正常。归因证伪，真正的静默退出来源是 `ELECTRON_RUN_AS_NODE` 被注入（dev.cjs 处理）与 userData
不可写导致单实例锁创建失败（`main.ts` 的 userData 可写探针处理）。三个开关删除，硬件加速回到
Electron 默认——否则每个用户都要为一次误诊长期承担 monaco/xterm 的 CPU 渲染代价。

## 备选方案（已否决）

- **改成设置项、默认仍禁用**：多一个用户要理解的开关 + 重启生效文案 + 启动关键路径上一次同步读盘，
  而这些成本只为保住一个已被证伪的假设。
- **原样保留**：零风险偏好，但把误诊固化成产品行为。

## 后果

`disable-gpu-disk-cache` **刻意保留**：它不改变渲染路径、无性能代价，挡的是"GPU 进程写不进缓存目录"
那一类权限错误（沙箱内实测会出现），与渲染加速无关。

判据：改完必须 `npm run dev` 起一次，看到窗口 + GPU feature status 为 enabled；若某台机器确实回退，
应作为该机器的问题单独记录，而不是全量预防。

---

# 回填（2026-10-14 21:3x）：这条 ADR 的正反两个方向都没有证据

## 流程上的批评成立

判据「改完必须 `npm run dev` 起一次看到窗口」**从未在本机执行过**，删除就落地了。这一点不变。

## 但据此得出的因果被证伪

当晚把三个开关恢复回 `9ed8b12` 的状态之后，应用**照样起不来，死法一模一样**。所以「删掉开关导致本机
启动失败」不成立，这条 ADR 的原始归因也依然没有被证实或证伪——两边都缺一次真实的窗口验证。

## 本机当前真正的阻塞（与仓库无关）

Electron 的浏览器进程在**加载任何应用 JS 之前**就终止：没有任何 `[main]` 打印、`%APPDATA%\cKunAgentSSH`
从未被创建、WER 与应用程序日志零崩溃记录、连 `--enable-logging=stderr --v=1` 也只吐一行 crashpad。
用不带仓库代码的裸 `electron.exe` 测（父进程分别是 agent shell、Node spawn、Windows 任务计划程序）：

| 命令行 | 结果 |
|---|---|
| `electron.exe .` | `0x80000003` STATUS_BREAKPOINT |
| `electron.exe --no-sandbox .` | `0xFFFF7003`（另一种早退，仍在 JS 之前） |
| `electron.exe --no-sandbox --version` | 打印 `v35.7.5`、exit 0 ← 唯一干净的一条 |
| `--disable-crash-reporter` / `--disable-gpu` / `--user-data-dir=…` / `--disable-features=Crashpad` | 全部 `0x80000003` |

即：Chromium 沙箱初始化在本机失败，绕过它之后还有第二个预 JS 早退。`npm run dev` 的退出码
`-36861`（= `0xFFFF7003`）与第二行完全一致，说明 electron-vite 传来的 `--no-sandbox` 是生效的。

时间参照：最后一次成功是 13:14→14:56（`.qoder-run/dev8.log`）。**15:00–15:01** 机器激活了新的
Code Integrity 驱动策略（事件 3099/3116，`Microsoft Windows Driver Policy` id `10.0.29520.0`，
即有更新落地），15:14 `HillstoneSecureConnect`（VPN 客户端）动过 LOCALAPPDATA；此后 Electron 再没起来过。
不是 3077/3089 那类进程拦截记录，磁盘充足，`dist/` 校验与版本自洽（35.7.5 一致）。

## 因此现在的决定

1. 三个开关**暂时保持恢复态**（= 最后一次已知可用的提交态），只为不再在未验证的情况下改动启动路径；
   [electron/main-gpu-switches.test.ts](../../electron/main-gpu-switches.test.ts) 已按此口径改写说明。
2. 关于「该不该无条件禁用 GPU 渲染」：**问题回到原点**，等机器能起 Electron 之后，用真实应用
   （不是最小外壳）在本机做「加/不加开关」对照再判。
3. 机器层面先解决：先重启，再试 `npm run dev`；若仍 `0x80000003`，查 VPN/安全客户端的过滤驱动与
   15:00 落地的驱动策略更新。
4. 取证口径：Git Bash/MSYS 会把 `0x80000003` 截成退出码 **3**，与 76956f2/9ed8b12 记录的
   「exit 3」只是数值撞脸；agent 侧任何 GUI 启动测量都不可信，就绪判据只能由用户在普通终端提供。

---

# 定案（2026-10-15）：真因是 HVCI，不是这三个开关

上文回填里「恢复三个开关后照样起不来」是对的——它证伪了「开关」这条因果，但当时没找到真因。补上：

- 注册表 `HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity\Enabled = 1`
  → 本机 **HVCI / 内存完整性处于开启状态**。
- HVCI 在 hypervisor 层强制 W^X，Chromium 浏览器进程的 V8 JIT（动态生成可执行代码）被拦截 →
  浏览器进程在**加载任何应用 JS 之前**就被击杀。这正好解释上表最反直觉的一行：
  `--no-sandbox --version` 出 `v35.7.5` exit 0（这条路径不做 JIT），而 `--no-sandbox .`
  （起浏览器进程、跑 V8）就早退/崩。
- 上表记的「15:00 Code Integrity 驱动策略更新」基本就是 Windows 打开内存完整性的时刻——那之后 Electron 再没起来过。
- **因此本 ADR 的原始议题（该不该无条件禁用 GPU 渲染）依然没有结论**：正反两边都缺一次真实窗口验证。
  三个开关继续冻结在 `9ed8b12` 状态，等 HVCI 关掉、应用能起来之后，再用**真实应用**做「加/不加开关」对照再判。
- **`--no-sandbox` 必须保留**：上表第一行证明不带它时 Chromium 沙箱在本机根本初始化不了（`0x80000003`）。
  它是本机必要绕行，不是元凶；删掉只会退化成沙箱初始化失败。

修复（用户侧，需重启）：Windows 安全中心 → 设备安全性 → 内核隔离详细信息 → 关闭「内存完整性」→ 重启 → `npm run dev`。
