# S14 — 战役未覆盖的五个后端模块（2026-10-09）

来源：把报告里点过名的 ID 与 `git ls-files '*.py'`（83 个非测试模块）对账后，五个模块在任何一份
S00~S13 报告里**零次出现**：`agent/tools/builtin/ops_file.py`(468) · `agent/host_profile.py`(227)
· `agent/remote_protocol.py`(78) · `agent/tools/builtin/ops_image.py`(110) · `sse_utils.py`(10)。

⚠️ 「零次出现」这个信号本身要先打折：模块名不出现在报告里，不等于没被修过。`ops_file.py` 就有
`f1708e4`（管道模板退出码）与 `094df28`（glob 的 pattern 也是路径参数）两次修复和 41 例专测，
其余四个模块都有引用方与清理提交 ⇒ 本片的定位是**补审**，不是「这片从没看过」。

## 状态图例

- **[已复核]** 我逐行读过 HEAD 的代码并确认成立。
- **[待复核]** 由审查 agent 报告，方向合理但我尚未逐行验证 —— 动手前必须自己确认，
  这条纪律在本战役里已经省下过重复劳动，也拦下过假信号。

## 发现表

| ID | 维度 | 级别 | 位置 | 一句话 | 状态 |
|---|---|---|---|---|---|
| S14-01 | a | **Critical** | `ops_file.py` FileWriteTool / FileEditTool 两处成功出口 | 写类工具只看 `ok`，而 `printf '%s' '…' \| base64 -d > path` 的退出码属于最后一段：`>` 先把既有内容截成 0 字节，printf 失败时 base64 收空输入仍 0 退出 ⇒ 回「已写入 N 字节」而文件是空的；撤销腿在预快照不可用（>200KB）时是 NoOp | **[已复核，已修 `341517b`]** |
| S14-02 | a | **Major** | `remote_protocol.py:71-76` | 拆解用第一处匹配 + 子串判据，把正文里的 `[exit_code=0]` 当远端退出码、并丢掉该字样之后的正文 | **[已复核，已修 `3bf9e7f`]** |
| S14-03 | a | **Major** | `ops_image.py:85-94` ↔ `ssh-manager.ts:1036-1040,1066` | 主进程 exec stdout 有 1MiB 上限并在末尾追加「[输出已截断…]」，而 base64 通道在它之前就截 ⇒ ≥~786KB 的远端图片拼成非法 base64 仍回成功；文档承诺的 10MB 上限远程永不触发 | **[已复核 2026-10-09，未修]**：`ssh-manager.ts:30` 的 `MAX_EXEC_OUTPUT_BYTES = 1024*1024` 在 `:1037-1038` 直接截累计串，**与 `keep_full` 无关**；`ops_image.py` 既不认截断标记、估长上限又被这个 1MiB 顶死。修法要么在后端认标记（要跨端约定字符串），要么读前先 `stat` 探大小 —— 后者单侧可做，但改的是工具行为，留给下一轮 |
| S14-04 | b | **Major** | `ops_file.py` FileGrepTool 本地分支 | 本地 grep 把逐个文件的读失败静默 `continue`，最后回「未匹配到内容」；远程分支会带 Permission denied ⇒ 两条腿给相反答案 | **[已复核，已修 `4debeba`]** |
| S14-05 | a/c | **Major** | `ops_file.py:174-176` `:364` `:443-451` | 本地分支在事件循环上做无上限阻塞文件 I/O（整文件 `readlines()`、全量 `rglob` 物化、整树扫行），`meta.timeout` 无人执行；仓内同形状写法是 `asyncio.to_thread` | [待复核] |
| S14-06 | c | Major | `host_profile.py:13-22` + `routes/agent.py:730-735` + `memory/blocks.py:61` | 主机画像块按临时会话 id 建、从不删除，每次 run `list_blocks()` 全量扫 | [待复核] |
| S14-07 | b | Minor | `ops_file.py:218-223` ↔ `ops_common.py:207` | `content` 无上限，整份 base64 拼进单个 shell 参数 | [待复核] |
| S14-08 | b | Minor | `ops_file.py` FileGrepTool 两条腿的 `max_lines` | 截断不加标记，「正好 200 条」与「被截到 200 条」读起来一样 | **[已复核，本地腿已修 `4debeba`；远程腿未动]** |
| S14-09 | d | Minor | `ops_image.py:85` vs `:61` | 字面量 `20` 与 `meta.timeout` 重复（`ops_file` 传的是 `self.meta.timeout`） | [待复核] |
| S14-10 | d | Minor | `host_profile.py:69-74` | 按会话清缓存的分支只有测试调用，断连时没有失效钩子 | [待复核] |

## 已修：S14-02（`3bf9e7f`）

模块自己的 docstring 写着「拼装顺序由 `ops_common.exec_remote` 决定（正文 → [stderr] 段 →
[exit_code=N]），拆分口径必须与那里一致」，也写着「拿正文里的字样当判据会把日志本身当成故障」。
实现用的却是 `_EXIT_SENTINEL_RE.search()`（第一处）与 `STDERR_SENTINEL in head`（子串）——
两条都违反它自己的成文原则。这个工具读的恰恰是 CI 日志与 agent 自己的输出文件，正文里出现
`[exit_code=0]` / `[stderr]` 是常态而不是边角。

修法：两处都取**最后**一次出现（`ops_job.py` 剥同一个哨兵早就用 `rfind`）；`[stderr]` 必须独占一行；
并认下「前导换行被链路上某处 strip 削掉」那种形状（stdout 为空时生产侧写的就是 `\n[stderr]…`，
标记会落到 0 位，认漏它等于把「tail 读不到文件」的真空错误段当无事发生）。

**发现过程值得留一句**：`test_ops_obs_channel_failure_not_fabricated` 里那条失败用例不是我改坏的 ——
它此前一直靠子串判据侥幸通过，我给「独占一行」收紧判据时才暴露。测试的 `_wire_s` 在 body 为空时
漏掉了生产侧那个前导 `\n`（helper 与真实线形不完全同源）。

**S14-02 后半（待裁决，已进 90 表）**：正文里独占一行的 `[stderr]`、而这次调用根本没有 stderr 段时，
字符串里没有任何东西能区分「日志里的一句话」与「远端真报了错」。要真正闭合需要每次调用带唯一
nonce（或换更不可能撞见的标记），那是跨层协议变更。已用 `test_known_residual_...` 钉成触发器。

## 探针自查（别重复劳动）

agent 报的以下三条**不成立**，别再当漏洞查：跨主机缓存投毒不可达（会话 id 是
`kind-<bootTag>-<seq>`，bootTag 每次进程随机，见 `ssh-manager.ts:420,638-646`）；
`host_profile` 的注入判据站得住（`_clean` 覆盖四个被插值字段，pkg/container 走白名单，
子命令 stderr 重定向到 /dev/null）；`remote_protocol` 没有死导出（五个符号全有消费者）。
`sse_utils.py` 不是死码（`routes/agent.py:35` import，`:952/:1199/:1237` 调用），
唯一瑕疵是两处 `"data: [DONE]\n\n"` 字面量绕过了公共格式化函数 —— 登记，不追。
