# AI 运维助手的安全边界

> 状态：2026-09-20，随 `ea7768f`（ops_* 内容级判定）+ `9a647d1`（批量下发接唯一判定源）+ 判定精度修正 F1–F5（由 `backend/tests/test_safety_misjudgment.py` 双向钉住）落地。
>
> 本文回答三件事：**边界在哪**、**什么算高危命令**、**为什么这样设计是对的**。
> §3.1 给判定精度的四条修正口径，§7 登记已知取舍（B/D/E/F/不对称/口径差异）——
> 这些是有意不做的，不是漏掉的。
>
> 行号会漂，判定逻辑不会：改任何一条规则前先读 `backend/agent/harness/safety.py` 本身。

## 0. 一句话结论

被约束的对象是**一个无判断力、可被远端输出注入、能一次动作打 N 台机器的自主代理**。
所以边界的形态是：**默认不信任 + 单一判定源 + 逐条命令绑定授权 + 生产环境只认预登记**，
而不是"枚举坏命令然后拦住"。枚举永远不全，默认审批才兜得住。

## 1. 威胁模型（防谁）

| 威胁 | 边界内的应对 | 边界外的依赖 |
|---|---|---|
| 模型误判/幻觉，执行破坏性命令 | 内容级分类 + dangerous 逐条发卡审批 + 120s 超时按拒绝 | — |
| 模型被远端输出注入（prompt injection） | 间接：命令仍需人批；hardline 无条件阻断 | 已知取舍 D |
| 模型经"合法工具"绕底线（写 authorized_keys） | 工具自报 `bound_command`，与 shell 同口径判定 | — |
| 一次批准 = 永久放行任意路径 | 授权键绑定命令/路径哈希（`tool:<sha16>`） | allowlist 无 TTL：取舍 E |
| `environment` 由客户端谎报以躲 prod 闸门 | — | 已知取舍 B，真上限在远端最小权限账户 |
| 人自己敲错命令 | 交互 PTY **不在边界内**（见 §5）；批量下发在（hardline 中止） | 事后审计 |
| 本机后端被完全控制后的上限 | — | `docs/p3-remote-least-privilege.md`：`ckunagent` + `ForceCommand` + 动词级白名单 |

## 2. 四层判定与处置

任一层都能独立阻断；上层放行不等于下层放行。

| # | 层 | 位置 | 职责 | 可否禁用 |
|---|---|---|---|---|
| 1 | 命令风险分类器 | `safety.py:1018 classify_command` / `:1086 classify_constructed_command` | 唯一判定源，产出 `hardline / dangerous / safe` | 否（无开关） |
| 2 | 策略层拦截器 | `reins/guards.py:29 FilesystemGuard`(priority 10)、`reins/gate.py:33 PermissionGate`(priority 8) | 按**内容**或工具级风险决定 阻断/审批/放行 | `safety_critical=True`，异常即拦截；`contracts.py` 无禁用开关 |
| 3 | 执行前底线复检 | `saddle.py:50 Sandbox.validate` | 不可禁用的第二道 hardline 复检 | 否（**当前仅覆盖 `shell_exec`**，见取舍 E） |
| 4 | 远端纵深 | `scripts/deploy/agent-exec.py` + `ckunagent` + `ForceCommand` | 不依赖任何一端自觉的真上限 | 由部署侧决定 |

拦截器执行顺序按 priority **升序**，即 `PermissionGate(8)` 先于 `FilesystemGuard(10)`。
两条入口规则：

- `PermissionGate` 对 `_COMMAND_EXEC_TOOLS`（`shell_exec`/`job_run`）直接放行给守卫，
  避免工具级 `risk_level` 一刀切挡住内容级判定；对**自报 `bound_command` 且内容非 safe**
  的工具同样让位（`gate.py:440`），因为 dangerous 的审批三态/超时/拒绝记忆/prod 闸门
  已在守卫里实现 —— 同一判定有两处实现必然漂移。
- 内容 safe 的绑定命令工具仍回到 ToolMeta 流程，但**授权键仍绑定该命令**（§4）。

处置语义：

| 判定 | 处置 |
|---|---|
| `hardline` | 无条件阻断，allowlist 也放不走（守卫在 allowlist 之前判定），审计 `conclusion=blocked`，注入 `[系统拦截]` 让模型换路 |
| `dangerous` | 发审批卡片；超时 120s（`reins/core.py:24`）按拒绝并记忆；prod 环境只认**预登记 allowlist**，临时审批一律拒（`prod_deny_no_allowlist`） |
| `safe` | 放行 |
| 权限门缺失 | fail-closed 拒绝（`permission_gate_missing`） |

## 3. 什么算高危命令

口径以代码为准，此处只给分类与量级，不复制清单（复制必然过期）。
锚点一律用符号名而不是行号 —— 本轮误判修复就往 `safety.py` 里加了两个函数，行号是最先漂移的东西。

- **hardline（`HARDLINE_PATTERNS`，14 条正则 + token 级升级）**：根目录递归删除、`mkfs`、
  `dd of=/dev/*`、fork bomb、`shutdown/reboot/halt/poweroff`、`systemctl` 电源动作、
  `telinit 0/6`、`killall5`、`kill -9 -1`、强制推主分支；token 级升级为 hardline 的还有
  写 `/etc/shadow`、`sudoers`、任意 `.ssh/` 路径、`sudo -S` 口令猜测。
- **dangerous（`DANGEROUS_PATTERNS`，37 条正则 + 结构规则）**：`sudo`、`chmod 777`/任意八进制、
  `curl | sh`、`base64 -d | sh`、`nc -e`、账户管理、内核模块、防火墙清空、crontab 安装、
  包卸载、强制 git、docker 破坏性、写敏感目录、读凭据文件、awk/sed 内联 `system()`、
  跨段"下载→执行"等。
- **默认审批（这套体系真正的防线，`_SAFE_VERBS` + `_classify_unknown_default`）**：
  只有只读动词白名单与只读子命令表免审，**未入表的一律 dangerous**。
  所以后端正则表里没有 SQL 类规则也拦得住 `DROP TABLE`（`mysql` 不在只读白名单）。
  这是"未知即审批"而非"靠枚举覆盖"的姿态。

### 3.1 判定精度：位置、路径与写开关（误判审查后的口径）

枚举表要挡的是"没见过的"，不能因此把"见得到的正常排查"打成高危。四条修正规则：

| 修正 | 规则 | 位置 | 挡掉的误判 |
|---|---|---|---|
| **执行位置** | 电源动词与 `sudo`/`su` 只在**执行位置**命中：剥掉包装器（`sudo`/`nohup`/`env`/`xargs -a f`）、shell 关键字（`do`/`then`/`for`）、赋值与括号后落在首位的动词。首 token 是解释器（`bash -c 'reboot'`）或 awk/sed 段内带 `system(` 时退回整段匹配 | `_classify_exec_position` | `journalctl -b \| grep -i reboot`、`cat /var/log/reboot.log`、`grep -c sudo /var/log/secure` 曾被判 hardline/dangerous |
| **安全设备** | `/dev/null`、`/dev/zero`、`/dev/urandom`、`/dev/stdout`、`/dev/tty*` 等既不是写敏感路径也不是敏感读取；`/dev/tcp/*`（反弹 shell）与 `/dev/sd*`（裸盘）不在其内 | `_is_safe_device` | `cmd 2>/dev/null` 这一族静默写法曾逐条要审批 |
| **路径驱动** | 凭据读取不再要求首 token 是 `cat/dd/...`，任何非 flag 参数命中 `_SECRET_FILE_RE` 即 dangerous；awk/sed 的内联 `system()` 单独成条 | `_classify_secret_read` | `sort /etc/shadow`、`awk -F: '{print $1}' /etc/shadow`、`wc -c /root/.ssh/id_rsa` 曾判 safe |
| **写开关** | 只读白名单动词的写副作用参数按写目标/独立成因判定：`sort -o <路径>`、`dmesg -C`、`ifconfig <网卡> down/promisc`、`hostname <新名>`；`kill` 进破坏性动词表（`kill -9 $(pgrep …)` 走间接展开规则）；curl/wget 的上传侧 `-T`/`-F @`/`--data-*`/`--post-file=` 命中凭据或敏感路径 → 外传 | `_classify_write_targets` / `_classify_exfiltration` / `_classify_unknown_default` | `curl -T /etc/shadow ftp://`、`dmesg -C`、`ifconfig eth0 down` 曾判 safe |

误拦与误放是同一枚硬币的两面，所以回归测试 `test_safety_misjudgment.py` 双向都钉：
上面每一条修正都带一组"必须仍是 hardline/dangerous"的反向锁（12 种真实 `reboot` 形态、
`rm -rf /`、`dd of=/dev/sda`、`echo x > /dev/tcp/…/4444`）。

抗混淆：`normalize_command_for_detection` 做 NFKC、剥引号、剥反斜杠、命令链分段、
子 shell 递归，然后分段送分类。

两种口径，差别只在要不要默认审批：

| 口径 | 函数 | 用于 | 理由 |
|---|---|---|---|
| 自由文本 | `classify_command` | `shell_exec`、`job_run`、`schedule_op(create)` 的 command、批量下发端点 | 模型/人写的原文，未知即审批 |
| 客户端构造模板 | `classify_constructed_command` | `file_write`/`file_edit`/`file_read`/`file_grep`/`log_read` 的 `*_cmd` 模板 | 模板自造（含 `base64`），关掉默认审批才不会让写 `/tmp` 也逐条审批；收紧的方向由**路径驱动**的 `_classify_secret_read` 与写目标扫描保证 —— 凭据读取不再依赖动词表（原 `_READ_VERBS` 已删：任何动词命中凭据路径都升 dangerous） |

## 4. 授权键语义（"批准这一条"而不是"批准这个工具"）

`gate.py:133 _grant_key`：**只要拿得到命令**，键就是 `tool:<sha256(规范化命令)[:16]>`；
拿不到才是裸工具名。于是：

- `shell_exec` / `job_run`：批准的是那一行命令。
- `file_write` / `file_edit`：批准的是"往这个路径写"（模板里的内容用固定占位符
  `_CLASSIFY_PAYLOAD` 替换 —— 否则每次改文件都要重新授权，且卡片 payload 爆大）。
  **文件内容级风险不在范围内**：对任意文件体做静态分析误报不可控，真正的植入面是路径，已覆盖。
- `file_read` / `file_grep` / `log_read`：绑定"读这个路径"的模板；`file_glob` 不绑定
  （`find -name` 只返回路径名，不外泄内容）。
- `schedule_op`：`create` 的自由文本按 shell 口径判定并绑定；其他 action 回落到工具级。

prod 下 `file_write` 的三档落点（本轮决策：**moderate 工具在 prod 不强制预授权**）：
植入类 → hardline 阻断；敏感目录（`/etc/...`、`/var/log/...`、`.env`）→ dangerous
且仅预授权、键按路径绑定；非敏感路径（`/tmp/x`）→ 会话授权，等同临时目录写。
再收紧会让生产每次改配置都要预登记，收益/成本不成立。

## 5. 通道分级：谁能自主做什么

| 通道 | 起点 | 判定者 | 缺省姿态 |
|---|---|---|---|
| **AI 自主** | 模型发起 tool call | `classify_command` / `classify_constructed_command` + Reins 全链 | 默认不信任：未入只读白名单即审批；hardline 无条件阻断 |
| **人授权执行**（visible_terminal） | 模型发起、人在终端确认 | 同上（判定在前），人点"执行/拒绝" | 拒绝写 run 级记忆，本 run 不再重问同一命令 |
| **人自主 · 交互 PTY** | 人敲键盘 | **无** | 不判定（见下） |
| **人自主 · 批量下发** | 人填一条命令 × N 台 | `POST /agent/safety/classify`（渲染层不再自带词表） | hardline 中止且无"确认执行"按钮；dangerous 二次确认；**后端不可达 fail-closed 拒绝** |

**为什么交互 PTY 不在边界内**（常被质疑，这里给可辩护的理由）：边界要约束的是无判断力、
可被注入、可一次打 N 台的自主代理；给人自己敲的命令加阻断，在威胁模型上没有收益，代价是
把这个产品变成一台打不开的终端。替代防线是：① 事后审计（链式签名，谁在什么环境执行了什么
都可追）；② AI 唯一能写进终端的路径必经确认卡；③ 真正有乘数效应的批量通道本轮补了 hardline
阻断；④ 真上限是远端最小权限账户，不是本机自觉。

**为什么不给 Electron 主进程再加一层判定**：`ssh:exec` 保持哑管道。渲染层已强制过后端分类，
主进程再实现一份就是第四套规则表，与本轮"唯一判定源"的目标相反。

## 6. 审计

`backend/agent/audit.py`：链式签名 append-only。`conclusion ∈ {executed, failed, blocked,
denied, auto_allowed, approved, released, revoked}`（`audit.py:139` 是唯一枚举，非法值静默
落成 `executed`），含 `environment`、`command_raw`、`run_id`。阻断记 `blocked`、prod 无预授权
与审批超时/拒绝记 `denied`（`result` 里带 `prod_deny_no_allowlist` / `approval_needed` 等成因），
因此"AI 试了什么没成功"同样可追 —— 这是 §5 里人工通道不加事前阻断的前提。

## 7. 已知取舍登记（有意不做，不是漏了）

| 编号 | 内容 | 为什么接受 | 真上限在哪 |
|---|---|---|---|
| **B** | `environment` 由客户端声明（`routes/agent.py:268 Literal["test","prod"]="test"`，来源是渲染层抄连接标签），后端无主机清单、不校验 `ssh_session_id ↔ environment` | 后端不持有可信主机清单，派生不出真值；加校验需要先做主机资产 | prod 强化是"客户端诚实则诚实"的闸门；上限是远端 `ckunagent` 最小权限 |
| **D** | 间接 prompt 注入：远端输出以 `role=tool` 原样回灌，无不可信标记（`ops_common.py` → `driver.py`），且记忆/反思/host_profile 三条持久化回路会以 system 消息注回 | 内容级标记与被注入检测是独立一轮工作，误报面大 | dangerous 命令仍必经人批；prod 仅预授权 |
| **E** | ① `Sandbox.validate` 第二层仅覆盖 `shell_exec`（`job_run` 与绑定命令工具无第二道）；② allowlist 无 TTL，"始终允许"无二次确认；③ `agent-exec.py` 的规则副本既没有归一化层，也仍是**整串**电源动词匹配 —— 后端改成执行位置判定后远端更严（`grep -i reboot` 在远端会被挡），方向安全但不等价 | 均为收敛度问题，不改变"默认不信任"的骨架 | 契约测试 `test_remote_whitelist_contract.py` 钉跨端一致性 |
| **F** | 判定精度上仍接受的四处（本轮误判审查后剩下的）：① 相对路径递归删除 `rm -rf ./build`、`find ./logs -delete` 判 safe；② 在日志里 grep 字面量 `grep -n 'rm -rf /' audit.log` 仍 hardline；③ `env \| grep -i pass`、`cat /proc/1/environ` 判 safe；④ 无子命令的裸 `docker`/`kubectl`（含 `command -v docker 2>/dev/null` 这种探测写法）判 dangerous | ①绝对路径与敏感目标已全覆盖，相对路径的破坏面等于该账户本就能写的范围，收紧会让每次 `make clean` 都弹卡；②根删除保持整串口径，位置化会把 `find / -exec rm -rf {} +` 从 hardline 降成 dangerous，得不偿失；③读取进程环境既不是写操作也不命中凭据路径表，加黑名单会让所有 `env`/`printenv` 排查都要审批 —— 真正的风险是输出内容进模型上下文，与取舍 D 同源；④保守方向，成本是一张卡 | ①远端最小权限账户；②③④审计链 + dangerous 必经人批 |
| **不对称** | `file_read/file_glob/file_grep` 在无 SSH 会话时降级读**本机**文件（仅 `..` 校验），与 `shell_exec` 的严格 fail-closed 不一致 | 有意的产品功能（本地文件浏览） | 路径遍历校验 + 审计 |
| **口径差异** | 批量下发换用后端唯一判定源后，`kill -9 <pid>`、`cp` 非敏感路径判 safe → 不再弹确认；此前渲染层自带词表对任意 `kill/cp/mv` 都会问一句 | 保留第四套词表换取的收敛；与 AI 通道同口径 | 同一判定源、同一后果说明 |

## 8. 改判定规则要跑什么

```bash
cd backend && python -m pytest -q \
  tests/test_safety.py tests/test_safety_default_approval.py \
  tests/test_safety_misjudgment.py \
  tests/test_command_explain.py tests/test_ops_content_classification.py \
  tests/test_permission_gate_content_bound_tools.py \
  tests/test_permission_approval_lifecycle.py tests/test_audit_coverage.py \
  tests/test_remote_whitelist_contract.py tests/test_safety_classify_endpoint.py
npx vitest run && npm run typecheck
```

约束：新增工具若能产生写盘/读凭据/自由命令副作用，必须实现 `BaseTool.bound_command`
（`agent/tools/__init__.py:79`）并在 `test_ops_content_classification.py` 的契约表里登记；
不纳入的也要登记，否则后人无法区分"决策"与"漏了"。
`test_command_explain.py` 逐条断言每个 `reason` 都有中文后果说明 —— 加规则要同时加解释。
`test_safety_misjudgment.py` 是双向锁：往只读白名单 `_SAFE_VERBS` 里加动词，必须同时给它配
"写副作用开关"的反例（`dmesg -C` / `sort -o` / `ifconfig eth0 down` / `hostname x` 四个先例）；
往枚举表加豁免，必须同时给出"真实破坏形态仍被判高危"的反向锁样本。
