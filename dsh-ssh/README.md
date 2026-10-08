# @ckun/dsh-ssh — DeepSeek Harness SSH 运维插件

自研的 DSH SSH 插件（不依赖社区 @linxin666/dsh-ssh）：**人机共用一个安全连接池**。
- **Agent 侧**：`ssh_list` / `ssh_connect` / `ssh_exec` 三个工具；只读命令直接执行，
  变更/高危命令弹用户审批卡（中文后果说明），生产主机禁变更、恒禁高危。
- **GUI 侧**：侧边栏「SSH 运维」面板 —— 主机树（按环境分组）、xterm PTY 终端、
  SFTP 文件浏览/上传/下载、本地端口隧道、主机密钥 TOFU 固定对话框。

## 安全模型

| 层面 | 规则 |
|---|---|
| 主机密钥 | 只由人工「测试连接 → 信任指纹」固定（TOFU）；agent 连接未固定主机 **fail closed**；密钥不匹配（疑似 MITM）一律拒绝 |
| ssh_exec | `readonly` 直放；`mutating` 审批（prod 主机默认拒绝，`allowProdMutations` 可放宽为仍审批）；`dangerous` 审批+强警告（prod 恒拒） |
| GUI 执行 | 面板终端/命令框是人类操作面，不走 agent 审批门 |
| 凭证存储 | 密码/密钥口令存入 **DSH 凭证服务**的记录（`ckun-ssh/<id>` grant 记录，如 `~/.dsh/.credentials.yaml`）；宿主无凭证服务时回退到 `~/.dsh/ckun-dsh-ssh.json`（0600，原子写）。主机元数据始终在 JSON，密钥只进凭证服务 |

命令分类器（`src/security.ts`）按 `&&`/`||`/`;`/`|` 分段，取最强级别：
`rm -rf /…`、mkfs、dd of=/dev/、shutdown、systemctl stop/restart、user 管理、
覆盖写 /etc、防火墙清空、kill -9 1、history -c 等为 **dangerous**；
文件写入/包管理/服务启动/git push/docker-k8s 写/进程 kill/DB 写等为 **mutating**。

## 文件布局

```
package.json          manifest（dsh.bundle.patch -> cordis.patch.yml）
cordis.patch.yml      profile roster 行：ckun-ssh -> @ckun/dsh-ssh
build.mjs             esbuild：host -> lib/index.js (ESM)，client -> lib/client.js (loader 包裹 CJS)
src/index.ts          host 入口：Config(volatile) / apply / sync / volatile-update 热更新
src/engine.ts         ssh2 连接池 + exec + shell + SFTP + 隧道 + 环境画像
src/store.ts          主机注册表（~/.dsh/ckun-dsh-ssh.json，~/.ssh/config 导入）
src/knownhosts.ts     主机密钥 pin 存储（~/.dsh/ckun-dsh-ssh-knownhosts，SHA256 指纹）
src/security.ts       命令分类 + 审批策略（allow / deny / ask + 中文说明）
src/routes.ts         /api/ckun-ssh/* REST + 终端 WS upgrade
src/tools.ts          ssh_list / ssh_connect / ssh_exec（defineTool + terminal 卡片）
src/client/           GUI：面板注册、REST/WS 客户端、xterm 终端、SFTP、隧道、主机对话框
```

## 开发

```bash
npm install
npm run build        # 产出 lib/index.js + lib/client.js
npm run typecheck    # tsc --noEmit
```

构建不需要 pnpm：esbuild 直接打 bundle，`packages: external` 让 host 侧
ssh2/ws/@deepseek-ai/* 在 DSH host 进程里解析；client 侧 react 与
@deepseek-ai/* 由 web runtime 的 module loader 解析，xterm 及其 CSS 内联进 bundle。

## 配置（DSH 设置页自动生成，均可热更新）

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 插件总开关 |
| announceToAgent | true | 是否向 agent system prompt 通告 SSH 工具 |
| maxOutputBytes | 65536 | ssh_exec stdout/stderr 截断上限（字节） |
| keepaliveMs | 30000 | ssh2 keepalive 间隔 |
| allowProdMutations | false | 允许 prod 主机的变更命令（仍走审批） |

## 安装到当前 DSH 实例

```powershell
# 1) 构建
node build.mjs
# 2) 复制到 desktop profile（DSH 需重启一次加载）
Copy-Item -Recurse -Force lib,package.json,cordis.patch.yml,README.md,icon.svg "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\@ckun\dsh-ssh\"
# 3) 确认 profile 的 bundle 列表含 @ckun/dsh-ssh（bundle 注册机制以实际 profile 布局为准）
# 4) 重启 DSH
```

> 与已禁用的社区插件 `@linxin666/dsh-ssh`（行 id `ssh`）不冲突：本插件行 id 为
> `ckun-ssh`，面板 key、API 前缀均独立。

## 已知限制（M1）

- 隧道仅本地端口转发（-L）；SFTP 为单文件上传/下载（无断点续传/目录递归）
- 终端会话随标签页关闭而结束（连接池保持；会话重连续接在 M2）
- 不支持 ProxyJump（导入 ~/.ssh/config 时仅打标签）
- 键盘交互式认证（2FA challenge）不支持，用密钥/密码/agent
