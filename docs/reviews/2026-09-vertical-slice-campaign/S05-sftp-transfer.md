# S5 · SFTP 文件传输

锚点 `a8e588c`。本文件第一部分（S5a）是我本人逐行验证的核心传输/续传面，含**遗留项 #1 的重审判定**；第二部分（S5b，渲染层与其余 15 个 sftp/fs 通道）由并行审查路补写，见文末待补节。

## 缝表

| 渲染层 | IPC 通道 | 主进程处理器 | 载荷方向 |
|---|---|---|---|
| `sftp.ts` → `getApi().sftpUpload(id,sid,local,remote)` | `sftp:upload` | `main.ts:1259-1335` 组 → `ssh-manager.ts:1371` | 4 参定长契约（M-18：多传的参数被 preload 静默丢弃） |
| 同上 download/cancel/readFile/writeFile/list/mkdir/… | `sftp:*` ×15 | 同组 | invoke |
| `sftp:progress` | push | `ssh-manager.ts` `sendProgress`（上传 `:1424-1428`） | `{id,transferred,total}`，`stalled` 后不再发 |
| `sftp:stalled` | push | 心跳回调 `:1403` | `{id}` 仅此一个字段 |
| `sftp:closed` | push | `openSftp` 的 close 回调 | `{id}` |

## S5a 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|
| **S5-01** | a | **Critical** | **LEGACY #1 确认成立**：上传是 8 路并发绝对偏移写，`.part.size` 必然越过连续边界，而停滞路径**故意跳过截断** → 续传跳过零填充洞 → rename 出静默损坏文件 | `ssh-manager.ts:1525` + `:1549` + `:1466` | 两条独立触发路径，见下 |
| S5-02 | b | Major | 兜底注释宣称的「顺序写」在上传侧**不存在**，M-5 的顺序流水线只用于下载；注释把两侧都写成「同形」 | `:1506` `:1548` vs `:1757` | 恒（是判据本身的错误） |
| S5-03 | a | Major | 主进程被杀/通道硬断时截断代码根本不执行，跨重启续传同样落进洞里 | `:1549`（只在正常收尾可达） | 上传中退出应用 / SSH 通道硬断 |
| S5-04 | a | Minor | 停滞时 `resumeSize` 只有 4KB 头部校验，洞在 4KB 之后即完全不可见；`VERIFY_SIZE` 的语义被注释当作全量保证来引用 | `:1380` `:1455-1466` | 恒（同 S5-01） |
| S5-05 | b | Minor | 停滞回调注释「渲染层按 STALLED 自动续传」的前提未证（S0-15：`retryable` 自定义属性疑不过 IPC）——注释描述了一条可能是死的腿，而用户手动重试走的是同一条易损路径 | `:1404-1420` + `sftp.ts:967` | 依赖 S0-15 确证 |
| S5-06 | a | Minor | `partStat.size === total` 即判「已完成」直接 rename，未校验内容——与 D12 只防「源缩小」的口径一致，但**洞恰好补到同长度**时（零填充把 size 顶到 total）会被当作完成 | `:1441-1443` | 停滞后续传再被中断的复合场景 |

## S5-01 · LEGACY #1 的重判（本轮最高优先级）

**当年登记的表述**（2026-09-24 清理轮，memory）：并发分块写使 `.part.size` 越过连续边界（chunk i+3 先于 i+1 落盘留零洞），stall 路径故意跳过截断，渲染层按 STALLED 重试的新传输 `resumeSize=partStat.size` 只验前 4KB → 从零洞之后续写、洞永不回填 → rename 出损坏文件。当时结论是「blast radius 高，交用户裁决，勿擅自打补丁」。

**本轮逐行复核结果：成立，且触发路径比当年记载的多一条。**

机制链（每步都打开了源码）：

1. `:1505-1508` 注释自述设计：「**并发写**：各 chunk 写偏移互不重叠，可安全并发（M-5 的"顺序追加写"约束是为续传无洞，这里改为失败时截断回连续写入边界来保证）」——原 M-5 的顺序流水线在上传侧被并发写替换掉了（`CONCURRENCY = 8`，`:1379`）。
2. `:1514-1541` 8 个 worker 各自 `await sftpWrite(sftp, uploadRemoteHandle, buf, chunk.offset)`（`:1525`）**绝对偏移写**。SFTP 侧文件大小随最靠后的那次写扩张；`contiguousIdx`（`:1511`，仅在 `:1533-1538` 的连续前缀推进里更新）**只是内存里的记账，从不是远端 `.part` 的属性**。故 `.part.size` ≥ 连续边界，差额正是尚未落盘的 chunk 区间，服务端以零填充。
3. `:1549` `if (firstError || (cancelled && !stalled))` —— 停滞时 `stalled===true`（`:1406` 设）且 `cancelFn()` 已被心跳调用（`:1418`），条件为假 ⇒ **跳过截断**。跳过理由写在 `:1545-1548`：怕旧传输迟到的 `setstat` 把新传输已写范围截小。
4. 新传输 `:1440` `sftpStat(partPath)` → `:1455-1465` 只读前 `VERIFY_SIZE = 4096` 字节比对 → `:1466` `startOffset = resumeSize`（= 越过了洞的 `.part.size`）→ `:1490-1493` 分块表从 `startOffset` 起建 → 洞所在区间**永不重读写** → `:1576` `sftpRename` → 交付一个中段为全零的文件，无任何错误、无任何提示。
5. `firstError` 分支的截断本身是**正确的**（`boundary = chunks[contiguousIdx].offset` 保守取连续前缀；worker 出错即 return，不推进记账；截断失败还有 P1 兜底删 `.part`，`:1558-1564`）——唯独停滞分支把这个保护摘掉了。

**可达性（两条独立路径，都不需要竞态巧合）**

- T1 网络劣化 → 30s 无进度 → 心跳判停滞（`TransferHeartbeat` `:176-208`，间隔 5s、阈值 30s）→ 走上面 3-5 步。用户视角：面板提示「传输停滞」→ 重试 → 大文件（>4KB，即任何真实运维文件）拿到静默损坏的结果。
- T2 上传途中 Electron 主进程退出/被杀，或 SSH 通道硬断（`sftpWrite` 挂起）→ 第 3 步的截断代码位于 `Promise.all` **之后**，根本执行不到 → `.part` 停在「size 已越过连续边界」的形态 → **跨重启**的下次续传同样跳过洞。这条是当年表述里没有的。

**为什么仍未修（尊重原裁决并补一条技术理由）**：修复要动的是「连续边界」的权威记账位置。`.part.size` 不可信已被第 2 步证明；`contiguousIdx` 只在单传输生命周期内存在于内存。可信做法是把连续已写字节持久化到旁路元数据（如 `partPath + '.meta'`），续传以它为准、且新传输写成功后原子更新——这既解 T1 也顺带解 T2。改动落在并发最敏感、已叠 M-5/D12/C10/P1/M7 五层修复、且另一会话正在高频改的 `ssh-manager.ts`，blast radius 仍高。**按本轮口径只报告不修。**

**候选修复方向（未实施，供裁决）**：`.part.meta` 记连续边界；停滞路径不再跳过保护，而是「旧传输一律不截断、新传输一律不信 size」。二者都要求 size 不再是续传依据。

**证伪尝试（我主动找过反证，未成立）**：曾怀疑 SFTP 服务端会对写间隙保持文件不扩张（无零填充）——但 `:216-218` 的 M-5 注释正是以「SFTP 服务端（OpenSSH）与本地文件系统都会用零字节填充中间间隙」为前提写的，且下载侧至今坚持顺序追加，说明该前提在当年是被实测过的。

## S5-02 / S5-04 · 注释与实现脱节（这条 Critical 之所以能活着的元凶）

`:1548` 用「由续传侧的 4KB 校验 + **顺序写**兜底」来为跳过截断辩护，但：
- 上传侧**没有顺序写**（同文件 `:1506` 自己就说改成了并发写）；
- 4KB 校验只覆盖头部，对 `:1466` 之后被跳过的大段区间无任何判别力——它的真实作用是「识别是不是同一个源文件」，不是完整性校验；
- `:1408` 与 `:1657` 两处都写着「上传并发 worker 的截断竞态防护，**下载同形**」，而下载实为 `pipelineReadWrite`（`:1757`，`await writeChunk` 严格串行，`:272-284`）——两者形态**不同**，风险模型也不同。

后果：任何按注释推理的后续维护者都会以为已有兜底。`docs/security-boundary.md` 未覆盖此路径，无规格承载。**建议裁决项**：至少要先把这三处注释改对（纯注释改动，零行为风险）。

## S5-06 · 长度相等即判完成的连带风险

`:1441-1443`：`partStat.size === total` 时不校验内容直接 rename。D12（`:1447-1454`）把 `>=` 收成 `===` 防的是「源文件缩小」，但零填充本身会把 size 顶到写过的最远终点——若停滞时最远 chunk 恰为末块，`.part.size` 已等于 total 而中段是洞，下次进入即「秒完成」并交付损坏文件。与 S5-01 同根（size 不可信），列为独立条目是因为它的用户感知完全不同：**没有重试、直接显示成功**。

## 基线核对

- `npx vue-tsc --noEmit -p tsconfig.node.json` → RC=0（S0 节已记，本切片复用）。
- S5a 全部 6 条为 `static-read-only`：每条 LOCATION 我均用 `sed -n` 打开原文核对，未跑后端测试（本切片无后端代码）。

## 待补（S5b）

渲染层 `sftp.ts`(1205) / `SftpView.vue`(1460) / `SftpPane.vue`(792) / `SftpFileEditor.vue`(648) / `monaco-runtime.ts`(301)，以及 `ssh-manager.ts` 的 sftp 其余算子（list/mkdir/mkdirRecursive/exists/rename/unlink/readFile/writeFile/zipRemote/unzipRemote/cancel）与 `main.ts` fs 组 20 个通道的路径守卫审计——由并行审查路产出，回填至本文件。

---

# S5b · SFTP 其余面（渲染层 + 15 sftp / 20 fs 通道）

下列每条 LOCATION 我均打开原文核对过；`electron/main.ts`、`electron/ssh-manager.ts`、`src/stores/sftp.ts`、`SftpPane.vue` 与锚点逐字节一致（`git diff a8e588c --stat` 为空）。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|
| S5-07 | c | Major | OS 原生拖拽上传在 Electron 35 上是**死功能**：`File.path` 已于 Electron 32 移除，全仓无 `webUtils.getPathForFile` 桥 | `SftpPane.vue:505` vs `package.json:38` | 恒（拖任意文件到远程面板） |
| S5-08 | c | Major | `fs:copy` 无源/目标重叠判定：把文件夹粘贴进自身 → 递归到路径长度上限，留下删不掉的长路径残骸 | `main.ts:700-731` | 一条鼠标路径，无确认 |
| S5-09 | a | Major | 承接 S5-08：10 万文件/10GB 上限在 `collect` **之后**才判，而 P2 修复注释写的正是「递归收集全部驻留内存可 OOM 崩主进程」——守卫放错了位置 | `main.ts:732-745` | 复制超大树 |
| S5-10 | c | Major | `sftp:writeFile`（编辑器保存）原地 O_TRUNC 覆写远程文件：无 `.part`+rename、无字节上限（本地侧 `fs:writeFile` 有 50MB 上限） | `ssh-manager.ts:1891-1904` vs `:1387,1576` / `main.ts:489-491` | 网络抖动/通道关闭时按 Ctrl+S |
| S5-11 | c | Major | 「取消」对进行中的同侧复制是静默空操作：`fs:copy`/`cp -r` 从不注册取消句柄，行永远停在「复制中」 | `sftp.ts:1027-1039` + `ssh-manager.ts:1907-1910` | 复制目录后点 ✕ |
| S5-12 | c | Major | `expandRemoteDir` 读**实时** `sshSessionId` 而非批次钉住的 sid，且宽 catch 把「会话已断」报成「目录不可读」 | `sftp.ts:568-572` vs `:666` | 展开大目录期间断链/切页 |
| S5-13 | a | Major | 服务器提供的目录项名未净化即拼进本地写路径：`relPath` 来自 `e.name`，`fs:pathJoin` 会**归一化掉 `..`**，`assertSafePath` 是敏感目录黑名单而非 jail | `sftp.ts:574,695` → `main.ts:778-780,1293` | 需恶意/误配的 SFTP 服务器（readdir 由服务端书写） |
| S5-14 | b | Minor | 地址栏提交路径时把刚存下的真因丢掉：`toFriendlyMessage(store.localError)` 传的是**字符串**，`client.ts:84-101` 无 string 分支 ⇒ 恒落 `fallback`。「拒绝访问敏感路径」被说成「路径不存在」 | `SftpPane.vue:361-368` + `sftp.ts:75-76,253` | 输 `C:\Users\<u>\.ssh` 回车 |
| S5-15 | a | Minor | `fs:copy` 只对**源**侧防符号链接，目标侧盲目跟随：目标里已存在的链接可被写入敏感文件（`copyFile(f.src, f.dest)`，`assertSafePath` 只查过两个根） | `main.ts:706-731` vs `:762-767` | 需外部预先植入链接（全仓零创建符号链接的 IPC）⇒ 定 Minor |
| S5-16 | c | Minor | 编辑器保存用**实时 props** 重推 (path, side, session)，而 `loadFile` 钉的是载入时的三元组；断开/切会话也不关编辑器 | `SftpFileEditor.vue:174-176` vs `:287-312` | 今日 UI 不可达（`position:fixed; inset:0` 蒙皮挡住切页），属潜伏守卫缺口 |
| S5-17 | d | Minor | `resumeOffset`/`retryCount` 是 M-18 IPC 契约收缩后的死状态（全仓 6 命中：声明 1 + 写 1 + 注释 4，无任何读方） | `sftp.ts:28-29,1052` | 死码 |
| S5-18 | d | Tradeoff | 四条解压路径（`fs:unzipLocal`、`sftpUnzipRemote` 的 unzip 与 python 两支）无自带 zip-slip 约束，安全性外包给外部工具；而同组 `fs:zipLocal` 自带 fail-close | `main.ts:853-868` / `ssh-manager.ts:2011-2062` vs `main.ts:811-832` | 现状够用（info-ZIP 与 CPython 自剥 `..`），风险在换工具/加 tar 时 |
| S5-19 | a | Minor | `zipRemote`/`unzipRemote` 把 `exitCode===null` 当失败并**执行删除**——归档已生成但通道未回 exit-status 时，用户要的产物被 unlink | `ssh-manager.ts:1955-1959,1997-2001`（null 可达性见 `:882,901-914`） | 罕见（缺 exit-status 或 zip 被 OOM kill） |

## 我逐行证实的部分（要点）

- **S5-07**：`grep -rn "webUtils\|getPathForFile" electron src` → **零命中**；`SftpPane.vue:505` 是 `(files[i] as any).path`（`as any` 正是 tsc 不报的原因），`:506-511` 空则 `toast.warning('无法获取拖入文件的本地路径')`；`package.json:38` `"electron": "^35.2.1"`；`main.ts:138-143` `contextIsolation:true, nodeIntegration:false`。⇒ 从资源管理器拖文件进远程面板**永不**进入 `uploadFiles`。这条与「IPC 三腿名字对称已验干净」不冲突——名字对称查不出一个被上游平台移除的属性。
- **S5-08/09**：`main.ts:700-731` 全文打开——只有两个根各一次 `assertSafePath`，`collect` 里 `mkdir(dest)` 后 `readdir(src)` 再逐条 `collect(join(src,entry), join(dest,entry))`；`destPath = join(targetDir, basename(src))`，把 `D:\a` 粘到自己身上得 `D:\a\a`（**在 src 之内**），于是新建的 `a` 立刻出现在 `readdir(src)` 结果里 → 递归到 ENAMETOOLONG。上限判定 `:737-745` 在 `await collect(...)` 之后 ⇒ `files` 数组早已建完，P2 注释承诺的「不再整棵树驻留内存」并未兑现。
- **S5-10**：实现体就一句 `sftp.writeFile(remotePath, buf, cb)`，无 flags、无 `.part`、无 rename、无长度上限；同文件的上传/下载两支都刻意走 temp+rename（`:1387`/`:1576`、`:1698`/`:1806`）。SFTP `write` 默认 `O_TRUNC` ⇒ 调用一开始原内容即没了，半途失败留一个截断前缀且不可恢复。
- **S5-11**：`sftpCancel` 只有 `const cancel = this.transfers.get(transferId); if (cancel) cancel()`；`transfers.set` 仅出现在 `sftpUpload`/`sftpDownload` 两处 ⇒ 复制类传输无句柄。`cancelTransfer` 的 pending 分支之外直接 `try { sftpCancel(id) } catch {}` 收尾，**不改状态、不弹提示**，行仍 `running`。对照 `pauseTransfer`（`sftp.ts:1045-1049`）有显式 kind 判定与反馈——取消没有。
- **S5-12/13**：`expandRemoteDir` 签名不收 sid，内部 `getApi().sftpList(sshSessionId.value, dir)`；同文件 `downloadFiles:666` 却钉 `const sid = sshSessionId.value` 并写了理由注释，`uploadFiles` 也把 sid 透传给 `mkdirRecursive:628` 与 `runUpload:948` ⇒ 三处口径两种做法。`childRel = rel + '/' + e.name` 无净化，随后 `fsPathJoin(localBase, relPath)` 走 node `join`（会吃掉 `..`），落盘侧只有 `assertSafePath(localPath)`——`main.ts:1285-1287` 自己注明它是敏感目录黑名单、不是 jail。

## 通道级守卫对账（要点行，完整表见 subagent 原始产出）

| 通道 | 调用方给路径? | 已加守卫 | 判定 |
|---|---|---|---|
| `sftp:upload` / `sftp:download` | 本地+远程 | `assertSafePath(localPath)`（`:1289`/`:1293`） | ok / 黑名单非围栏→S5-13 |
| `sftp:writeFile` | 远程+content | **无**、无字节上限 | S5-10 |
| `sftp:list/mkdir/rename/delete/…` | 远程 | 无 | ok-by-design（远端信任模型不同） |
| `fs:copy` | 本地×2 | 仅两个根 | S5-08/09/15 |
| `fs:zipLocal` | 本地[]+dest | `:808-809` + `assertNoSymlinks :811-832` | **全组最强** |
| `fs:unzipLocal` | 本地×2 | 仅两个根 | S5-18 |
| `fs:resolveProgram` | `name` | **无**（`join(dir, name+ext)` 后 existsSync） | Minor 缺口：`..` 归一化后可探测任意绝对路径存在性与规范路径；读写执行不在此通道（属 `pty:create`） |
| 其余 `fs:readDir/readFile/writeFile/readFileForEditor/mkdir/rename/trash/exists/stat` | 本地 | `assertSafePath` 全覆盖，大小上限齐备 | ok；错误包装不一致见下 |

错误包装两制且互不覆盖：`rethrowIfSensitivePath`（唯一会**记录安全事件**并故意重抛，`main.ts:414-419`）与 `fsErrorWith`（唯一加兜底文案）重叠但不合一；`fs:mkdir/rename/copy/trash/exists/stat` **两者都不套**，于是英文 `ENOENT: no such file or directory, mkdir 'X'` 直达中文 UI——而这些恰是失败会被用户看见的操作。

## 留给下轮的未决假设（S5b 提出，我判为「不足以立条」）

1. Windows `fs.realpath`（libuv `GetFullPathNameW`）是否展开 8.3 短名——若否，`~/.ssh` 可用 `…\SSH~1\id_rsa` 绕过 `ANYWHERE_SENSITIVE`。静态无法判定，需实机一次 `node -e`。
2. PowerShell `Expand-Archive`/`Compress-Archive` 遇逐条目非终止错误是否仍退 0——若是，`fs:zipLocal/unzipLocal` 会把半成品报成成功（同文件 `:952` 的补全脚本设了 `$ErrorActionPreference`，这两条命令什么都没设）。
3. `zipRemote` 的 python 回退支会丢空目录并跟随符号链接（`zf.write`），而 zip 主支用 `-y`——两支产物语义不同。
