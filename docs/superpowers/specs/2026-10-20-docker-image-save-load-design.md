# Docker 镜像导出/加载（纯远端执行）设计

日期：2026-10-20
状态：已批准实施

## 目标

在 Docker 镜像页提供 `docker save` / `docker load`，让用户把镜像打成 tar（可选 gzip 压缩）
留在远端主机、或从远端 tar 载入镜像。**两端都在远端完成，本地不落地。**

## 非目标

- 不做镜像下载到本地 / 从本地上传（`docker save` 是二进制流，走不了 `sshExec` 的
  文本通道；改走 SFTP 要多一条传输链路、一份本地副本，还要新开本地保存对话框 IPC）
- 不做真实字节进度（`docker save -o` 无进度输出；轮询 `sftpStat` 需先 inspect 拿总量，
  且总量是近似值）
- 不做可取消（中途断 channel 只会得到残缺 tar）

## 交互

### 导出

入口两处：工具栏无专用按钮（避免工具栏膨胀），镜像行内「箱子」图标（`title` 为
`docker save`），以及镜像行 hover 时可见。

弹窗结构（精简版）：

```
箱子 导出镜像
────────────────────────────────
harbor.ckun.io/app:v3.8.2  318 MB
────────────────────────────────
保存到    [/tmp              ↻]
          .. 
          docker-backups   10-18
          images           10-20
文件名    [app_v3.8.2.tar      ]
格式      [tar] [tar.gz]  gzip 压缩，打包更慢
          /tmp/app_v3.8.2.tar          ← 一行小字
          ⚠ 已存在，导出会覆盖它        ← 命中 sftpExists 才出现
────────────────────────────────
打包期间不可取消       [取消] [导出]
```

- 目录选择器只列目录（文件不可点）
- 文件名主体拒绝 `/`、`..`、控制字符、以 `-` 开头；后缀由格式开关决定，
  `.tar` / `.tar.gz` / `.tgz` / `.gz` 会被归一到选中那一种（`.tgz` 不会变成 `.tgz.tar`）
- 完整路径 = 目录 + `/` + 文件名，实时计算

### 加载

工具栏「加载镜像」（立方体图标，与「拉取镜像」的箭头区分）。

```
立方体 加载镜像
────────────────────────────────
[/tmp                        ↻]
..
docker-backups     10-18
app_v3.8.2.tar     318 MB     ← 选中
base-image.tar.gz  1.2 GB
docker load -i '/tmp/app_v3.8.2.tar'   ← 一行小字
────────────────────────────────
加载期间不可取消       [取消] [加载]
```

- 只列 `.tar` / `.tar.gz` / `.tgz`（文案取调用方的白名单，不写死）；目录可点进
- 加载不看后缀、不看压缩：`docker load -i` 按魔数识别 gzip / bzip2 / xz
- 后缀过滤不保证内容有效（坏 tar 由 docker 报 `invalid tar header`），错误原样展示

## 命令

```ts
// 不压缩
dockerSaveCmd(ref, path)      → `docker save -o ${sq(path)} ${sq(ref)}`
// gzip：先落同目录临时文件，再压到目标（退出码带出两步的真实结果）
dockerSaveCmd(ref, path, true)
  → `docker save -o ${sq(tmp)} ${sq(ref)} && gzip -1 -c ${sq(tmp)} > ${sq(path)}; rc=$?; rm -f ${sq(tmp)}; exit $rc`
  // tmp = saveTempPath(path)，如 /tmp/.app_v3.8.2.tar.gz.part
dockerLoadCmd(remotePath)     → `docker load -i ${sq(path)}`
```

路径一律过 `sq()`（POSIX 单引号转义，已存在）。镜像 ref 同样过 `sq()`，避免自定义标签
里的特殊字符进入 shell。

压缩为什么既不用管道、也不用「先 `-o` 落到同名 `.tar` 再 gzip」：

- 「先落同名 .tar」会覆盖同目录里真实可能存在的旧备份（`app_v3.8.2.tar` 就是它），否掉
- 只用管道：POSIX 管道的退出码只反映最后一个命令，`docker save` 中途死掉时 `gzip`
  仍退出 0，交出一份看起来完好、实际截断的备份 —— 备份工具最不该有的失败模式
- 取折中：`save -o <同目录点前缀临时文件>` → `gzip -c` 到目标 → `rm` → `exit $rc`。
  退出码因此同时代表两步；save 失败时 gzip 不跑，目标位置的旧备份原样保留。
  临时文件放目标目录而不是 `/tmp`（后者常是 tmpfs，多 GB 镜像会把内存吃穿）。

级别取 `-1`：镜像层多是二进制，`-6` 省不下多少体积，但在 600s 预算里明显更慢。

## 目录浏览

复用现有 SFTP 通路，不新增任何 IPC：

- `sftpOpen(targetId)` — 幂等，已开即 return；SFTP 面板共用同一条通道
- `sftpList(targetId, path)` — `requireSftp`，未开则抛 `SFTP_SESSION_REQUIRED`
- `sftpExists(targetId, fullPath)` — 导出侧覆盖预检

竞态：连点「上一级」时慢的旧请求后到会覆盖新目录。沿用 `HomeRemoteFilePicker` 的
序号写法（`listSeq`）。

## 四条诚实边界

1. **超时可能留半截 tar**。`ssh:exec` 的超时被主进程硬钳在 600 秒
   （`main.ts`），超时即 `streamRef.close()`，远端 `docker save` 收 SIGHUP 后可能留下
   写了一半的文件。失败态必须明示「可能残留：<path>」并给「更换目录」。
2. **`exitCode === null` 是「不知道」**。精简 sshd 不回 exit-status 时，文件可能已经写完。
   沿用 `classifyExit` 三态，不谎报失败；大小只进文案，不参与成败判定（SFTP 不可用 /
   chroot 视角不同时读不到，不代表没产物）。
3. **`docker save -o` 静默截断同名文件**。打开弹窗时 `sftpExists` 预检，命中即红字提示。
4. **临时文件可能残留**。超时/断链时 `rm` 那一步不会执行，同目录会留下 `.<名>.part`；
   失败文案把目标路径与临时文件一起指出。`exit 0` 表示 save 与 gzip 都成功（`&&` 串联 +
   `$?` 带出），不再用产物大小猜成败 —— scratch 镜像压完可能只有几百字节，
   1KB 下限会把它误判成失败。

## 分层

| 位置 | 职责 |
| --- | --- |
| `docker-core.ts` | 命令构造、默认文件名推导、文件名/路径校验（纯函数） |
| `RemoteDirPicker.vue` | 只负责「列出远端目录 / 选目录 / 选文件」，不碰 docker |
| `DockerImageTransferModal.vue` | 两个弹窗的 UI + 表单校验；执行经 `emit('submit')` 交给父级 |
| `DockerView.vue` | 执行（`dockerExec` + 审计）、busy/error/计时、成功后 `reloadImages()` |

执行留在父组件：审计（`AUDIT_ACTION_DOCKER`）、`dockerExec` 封装、刷新都在那里，
组件内自己 exec 会把这一整套复制一遍。

## 测试

- `docker-core.test.ts`：命令拼接（含单引号路径、gz 临时文件中转）、`suggestTarName`
  各种 repo/tag（含 gz 后缀）、`validateSaveName` 拒绝清单（后缀不参与）、`ensureTarSuffix`
  后缀归一（含尾点）、`saveTempPath`、`joinRemotePath` 尾斜杠
- `DockerView.guards.test.ts`：源码级约束——超时预算用满 600、失败文案含「可能残留」、
  `classifyExit` 参与判定、成败只看退出码、reject 路径也带残留路径

## 代码规模控制

`DockerView.vue` 已 1580 行，本次只做挂载，不往里堆逻辑。
