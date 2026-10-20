# GitHub 加速产品化：自检、失效巡检与页面收敛

- 日期：2026-10-20
- 状态：草稿，待审
- 范围：`shared/github-accel-core.ts`、`electron/github-accel.ts`、`electron/gh-hosts.ts`（复用测速）、
  `electron/main.ts`、`electron/preload.ts`、`src/env.d.ts`、`src/stores/github-accel.ts`、
  `src/components/ssh/GithubAccelView.vue`、`src/layouts/TitleBar.vue`

## 1. 背景

当前功能已覆盖 git insteadOf、SSH 443、hosts 优选、链接转换、镜像管理、续传下载器。内核扎实，
但作为产品缺一个闭环：它从不证明加速真的生效，也不会在公共镜像死亡时自救。

四个已确认的问题（按优先级）：

1. **push 语义错误**。总控文案写「git / SSH 推拉自动走加速」（`GithubAccelView.vue:27`），但
   `url.<prefix>.insteadOf = https://github.com/` 会同时改写 push 地址；ghproxy 类镜像基本是只读
   缓存，原本可用的 HTTPS push 会因此失败。
2. **没有生效验证**。镜像测速只测首字节，且 `probeOnce` 把 `status < 500` 一律判为可用
   （`electron/github-accel.ts:245`）——回 403 的镜像也能拿到「最快」徽标；hosts 写完只回读托管块
   是否在文件里，没验证系统解析；没有任何端到端读取检查。
3. **镜像失效无自救**。无后台巡检、无全局告警；生效前缀不能直接换（需关闭还原 → 重选 → 开启）。
4. **页面重复且冗长**。总控 chips 与三张措施卡是同一状态的两套投影；三张卡占近半屏；五段解释性
   hint 增加扫读成本。
5. **常见链接形式解析失败**（2026-10-20 实测）：`releases/latest/download/<file>` 被判成 `page`
   （只给网页镜像，不给下载链接）；完整 `git clone …` 命令、`gh repo clone owner/repo`、`owner/repo`
   简写均为 `unknown`。

## 2. 目标与非目标

### 目标

- 加速语义与真实能力一致：明确「只加速读」，push 引导到 SSH 443。
- 提供可手动触发的端到端自检；主控启动成功后自动跑一次。
- 主进程巡检当前生效镜像与已写入的 hosts；失效时页面红灯 + 一键切换，并让标题栏 GitHub 图标带红点。
- 三张措施卡并成一张三行措施表，删掉总控 chips，压缩 hint。
- 链接转换接受 `releases/latest/download`、完整 clone 命令、`gh repo clone` 与 `owner/repo` 简写。

### 非目标（本轮明确不做）

- 不自动切换镜像、不自动改任何配置；巡检只观察和提示。
- 不修改用户仓库 remote，不提供「把 origin 切到 SSH」工具。
- 不做吞吐测速：没有稳定的 1 MB 公共探测文件，用 `git ls-remote` 端到端 + TTFB + TCP 代替。
- 不持久化测速/巡检结果（进页自动测；巡检状态仅内存态，重启后由首检恢复）。
- 不改下载器（空态入口、可选目录等另案）。
- 不动 hosts 优选算法与 UAC 写入路径。

## 3. 设计

### 3.1 探测判定修正

`probeOnce` 不再用统一的 `status < 500`。探测目标带 `kind`：

- `proxy`（ghproxy 类前缀 + raw README）：仅 **2xx** 判可用。镜像对真实存在的 RAW 文件回
  403/404/429 即不可用，不能再参与「最快」排序。
- `web`（网页镜像站根地址）：维持 `status < 500`。根地址对爬虫回 403 不代表浏览器不可用，
  这里只验证「站点可达」。`bestProxyId` 只取代理，不受该放宽影响。

纯函数 `isProxyProbeOk(status: number): boolean` 放进 `shared/github-accel-core.ts`，供测试与两处
调用共用。

### 3.2 加速自检

新增 IPC `ghaccel:selfCheck` → `SelfCheckRow[]`。主进程只读，不写配置、不弹 UAC。

```ts
interface SelfCheckRow {
  key: 'git' | 'proxy' | 'ssh443' | 'hosts'
  state: 'ok' | 'fail' | 'skip'
  message: string
  latencyMs?: number
}
```

检查项只在对应措施生效时执行；未开启/未安装返回 `skip`（UI 灰显，不算失败）：

| key | 条件 | 动作 | ok 判据 |
|---|---|---|---|
| git | insteadOf 生效 | `git ls-remote https://github.com/git/git HEAD`，15s 超时，`GIT_TERMINAL_PROMPT=0` | 退出码 0 且 stdout 非空；耗时即 latency |
| proxy | insteadOf 生效 | 对当前生效前缀发一次 RAW 探测（2xx 判据，TTFB） | 2xx |
| ssh443 | 托管块生效 | `ssh -G github.com` 解析 hostname/port，并 TCP connect `ssh.github.com:443` | hostname=ssh.github.com 且 port=443 且 TCP 可达 |
| hosts | 托管块生效 | 取托管块前 3 个域名做 `dns.lookup` 比对，并对解析结果 TCP:443 | 至少一个域名解析命中托管块 IP 且可达 |

- `ssh -G` 解析抽成纯函数 `parseSshG(stdout)`（放 shared，便于测试）。
- TCP 测速复用 gh-hosts 的 4s 口径：抽到 `electron/utils/tcp-latency.ts`，`gh-hosts.ts` 与新代码
  共用，避免第二份实现。
- hosts 解析不命中托管块时给 `fail` 并在 message 说明「系统解析为 X，可能被 DNS 缓存/其他软件覆盖」。
- git 行 message 带镜像名：`经 ghproxy.net 读取成功（1.2s）`；失败带原始 stderr 首行。

UI：总控卡右侧「自检」按钮（无任何措施生效时禁用）。结果按行展示状态点 + 文案。主控
`masterEnable` 返回后只要有任一项 `ok`，由渲染层异步触发一次自检，不阻塞总控按钮状态。

### 3.3 镜像巡检与一键切换

`GithubAccelService` 增加内存态巡检：

```ts
interface HealthStatus {
  state: 'unknown' | 'off' | 'ok' | 'degraded'
  activePrefix?: string
  activeName?: string
  latencyMs?: number
  error?: string
  fallback?: { prefix: string; name: string; latencyMs: number }
  /** hosts 托管块生效时的实时有效性；未写入不返回 */
  hosts?: { ok: boolean; message: string }
  checkedAt: number
}
```

- 触发：主进程就绪后 15s 首检；之后每 30 分钟一次；`gitEnable`/`gitDisable`/`saveLists` 后 2s 复检
  （去抖，同一时刻只跑一轮）。
- 判据：
  - `gitStatus()` 生效 → 对当前前缀发一次 2xx 探测；失败时才并发探测其余启用代理，取最快可用者为
    `fallback`，整体 `degraded`。
  - hosts 托管块生效 → 取 `github.com`（缺失则取第一条）做 `dns.lookup` 比对托管块 IP；命中后再
    TCP:443 测一次。不一致或不可达 → `hosts.ok=false`，整体 `degraded`。
  - git 与 hosts 都未生效 → `off`；都没有问题 → `ok`。SSH 443 不在后台巡检内（由自检覆盖）。
- 推送 `ghaccel:health`；`ghaccel:healthStatus` 返回最后一次结果供标题栏挂载时拉取。
- 渲染层 `switchActiveMirror(prefix)`：`gitEnable(prefix)` + `saveOptions({lastProxyPrefix})` +
  刷新状态 + toast；手动下拉与红灯按钮共用同一入口。
- UI：
  - 总控卡 `degraded` 时显示红灯提示：镜像失败显示 `当前镜像 <name> 不可用（原因）`，有 fallback 时给
    「切换到 <name>」按钮；hosts 失败单独显示 `hosts 未生效：<原因>` 并给「重新优选并写入」。
  - git 措施行生效时展示下拉（启用代理列表，当前值选中），变更即切换；切换失败回读状态复原。
  - 标题栏 GitHub 图标在 `degraded` 时加红点（仅红点，无绿点、无托盘），title 追加「当前镜像不可用」。
    不改变点击行为。标题栏通过 store 的轻量 `initHealth()`（只拉状态 + 订阅）初始化，不加载整份配置。

### 3.4 页面收敛

- 删除总控卡的 measure chips；总控只保留状态点、标题、按钮和必要提示。
- 三张措施卡（git / SSH 443 / hosts）合并为一张「加速措施」面板，三行：
  - 每行：名称 + 状态签 + 右侧动作；
  - 详情（生效子节、备份提示、hosts 条目表与优选表）仅在相关状态出现，作为行下二级块；
  - hosts 的优选表保持现状，只在有结果或已写入时渲染。
- hint 收敛：
  - 总控一行说明管辖范围 + UAC；另加 push 声明：`git 加速只代理读取（clone/fetch/下载）；HTTPS push 不走镜像，请用 SSH 443。`
  - 各措施行仅保留一句操作后果；机制解释移入 `title` 属性。
  - 风险注保留原文案。
- 顺序不变：总控 → 链接转换 → 下载任务 → 加速措施 → 镜像源 → 风险注。

### 3.5 链接解析补全（`shared/github-accel-core.ts`）

`parseGithubUrl` 增加四类宽容输入，均先归一化再走现有分支：

- `releases/latest/download/<file>`：与带 tag 的 `releases/download/<tag>/<file>` 同归
  `release-asset`，`normalized` 保留 `latest` 形式（GitHub 自行重定向，下载器已支持重定向）。
- 完整命令：`git clone [--flags] <url>`、`gh repo clone <url|owner/repo>` → 取最后一个地址 token
  （剥引号）后递归解析。
- `owner/repo` 简写：两段、不含空白与反斜杠 → `repo`。
- 不改动已有形式的行为：`git@github.com:…`、`ssh://git@github.com/…`、raw、codeload、archive、页面路径。

### 3.6 契约

- `electron/preload.ts`：新增 `ghaccelSelfCheck`、`ghaccelHealthStatus`、`onGhaccelHealth`（返回取消函数）。
- `src/env.d.ts`：补两方法 + 事件签名 + `SelfCheckRow`/`HealthStatus` 形状。
- `electron/main.ts`：注册两个 handle；在 `setupSSHIPC` 里启动巡检并把 `ghaccel:health` 推给
  `mainWindow`（沿用窗口销毁守卫）。
- 新增形状守卫测试：主进程 `HealthStatus` 与 `env.d.ts` 字段同名（仿 `hosts-status-shape.test.ts`）。

## 4. 测试

- 单测：`isProxyProbeOk`（200/204 true，403/404/429/500 false）；`parseSshG`（标准输出、缺字段）；
  四类新解析输入 + 现有形式回归；形状守卫。
- 回归：全量 vitest、`vue-tsc -p tsconfig.web.json`、`vue-tsc -p tsconfig.node.json`。
- 手工验证（不写用户配置）：在 Bash 里实跑 `git ls-remote https://github.com/git/git HEAD`、
  `ssh -G github.com`，确认判据与解析在真实输出上成立。
- UI：Vite 5173 + stub electronAPI 注入 ok/degraded/off 三态，核对红灯、自检结果、措施表与
  下拉切换的渲染；真实 Electron 内由用户验收 UAC 与 hosts 路径。

## 5. 待确认的默认决策

1. 自检：手动按钮 + 主控启动成功后自动一次；关闭加速不跑。
2. 巡检：30 分钟周期，启动后 15s 首检，切换后 2s 复检；失败才探测其余镜像。
3. 驻留：仅标题栏 GitHub 图标红点（degraded），不加绿点、不加托盘。
4. 切换：只给按钮/下拉，绝不自动改配置。
5. push：只改文案与风险提示，不改仓库 remote。
6. 页面：三卡并一、删 chips；下载器与镜像编辑功能不动。
7. 解析：四类宽容输入纳入本轮；`releases/latest/download` 的 `normalized` 保留 `latest`，不先解析成具体 tag。

## 6. 风险

- 2xx 判据可能把对探测文件回 403 的「半可用」镜像判死。这是有意收紧：该镜像对我们的真实
  clone/下载路径同样会失败。
- `ssh -G` 在缺少 OpenSSH 的 Windows 上不可用，返回 `skip` 而不是失败。
- hosts 的系统解析比对可能受 DNS 缓存影响，按 fail + 说明处理，不自动 flush。
- 巡检每 30 分钟只发 1 个 HTTPS 请求（失效时才多探几个），不构成额外负担。
