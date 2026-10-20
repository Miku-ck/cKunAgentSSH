# 文档导航

四类文档职责不同，**不要互相代偿**：想理解「为什么这么设计」看 adr，想确认跨端契约看
contracts，想知道某功能当初的设计与取舍看 superpowers/specs，想复现一次审查的取证过程看
reviews。

## adr/ — 架构决策记录

不可逆决策及其否决过的备选方案。编号递增，只追加不重写；结论被推翻时新开一条并在前一条标注。

| 编号 | 决策 |
|---|---|
| 0001 | 主进程不再无条件禁用 GPU 渲染 |
| 0002 | 内嵌 Python 运行时 + 硬门禁 |
| 0003 | 共享 userData / 单实例串行 |
| 0004 | 审计结论的载体契约 |

## contracts/ — 跨语言契约

前后端 / 主进程两侧必须一致的东西。`audit-conclusions.md` 是审计结论文案的单一来源，
改它要看两侧是否都跟着动了。

## reviews/ — 审查战役档案

历史快照。文中行号是**审查当时的 anchor commit**（如 `a8e588c`）的行号，不是当前行号；
被审的文件此后大量变动，**不要按当前代码去核对这些行号**，也不要回头修正它们。
`2026-09-vertical-slice-campaign/` 的入口是 `00-INDEX.md`，待裁决项在 `90-ADJUDICATION-SHEET.md`。

## superpowers/ — 设计与计划

`specs/` 按日期命名的一次性设计文档，`plans/` 是拆出来的工单。

约定：**功能退役后 spec 原文不改不删**，只在下表标注状态。要表达当前真实形态，改
`DESIGN.md` / `README.md`，而不是回去改历史 spec。

### specs 落地状态

| 文件 | 状态 |
|---|---|
| 2026-09-02-model-seed-env-driven | 已落地（首启模型探测走 env） |
| 2026-09-14-bridge-ws-channel | 已落地（`/ws/bridge` 反向通道，断线 fail-closed） |
| 2026-09-22-agent-memory-redesign | 已落地（记忆 v2） |
| 2026-09-22-goal-plan-mechanics | 已落地（目标 / 计划机制、计划模式） |
| 2026-09-29-home-command-panel | 已落地，形态被下一条修订 |
| 2026-09-30-retire-ai-side-panel | 已落地（2026-10-20，AI 侧栏退役完毕） |
| 2026-10-08-ssh-tunnel | 已落地（TCP / SOCKS5 / Dynamic） |
| 2026-10-08-tls-dataplane | 已落地（隧道 TLS 数据面 + `gateway/hillgw.mjs`） |
| 2026-10-09-dsh-ssh-ui-polish | 已落地（dsh-ssh 与 TLS 数据面两个新面已扫净） |
| **2026-10-13-glass-theme-macos-vibrancy** | **已退役** — 玻璃两档主题随 `8c80e46` 删除，代码里 glass/aero/vibrancy 零残留 |
| **2026-10-14-aero-glass-theme** | **已退役** — 同上，`THEMES` 白名单已无此键，旧键值由 `asEnum` 闸门回落 `dark` |
| **2026-10-14-glass-light-translucency** | **已退役** — 同上 |
| 2026-10-20-docker-image-save-load | 已落地（镜像导出 tar / 加载） |
| 2026-10-20-docker-unused-image-prune | 已落地（未用镜像清理） |
| 2026-10-20-github-accel-productization | 已落地（`866224d`） |
| 2026-10-20-home-agent-workbench | 已落地（首页任务台成为 AI 唯一界面） |
| 2026-10-20-sftp-path-bookmarks | 已落地（远端目录书签） |

`previews/` 放设计阶段的交互预览稿（HTML），与 spec 同源但属于工件，不作为实现依据。
