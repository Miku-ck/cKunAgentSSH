# AI 运维助手的安全边界

> 状态：2026-09-20，随 `ea7768f`（ops_* 内容级判定）+ `9a647d1`（批量下发接唯一判定源）+ 判定精度修正 F1–F5（由 `backend/tests/test_safety_misjudgment.py` 双向钉住）落地。
>
> 2026-10-11 补记（审批审查）：once 授权的 run 终态 409（§4）、plan 姿态 ⇒ permission_mode 配对强制（`routes/agent.py` 的 `_paired_permission_mode`，plan 时 prompt 声称只读则 PlanModeGuard 必须激活）、自动审批裁决同源取舍登记（§7「裁决同源」）。
>
> 本文回答三件事：**边界在哪**、**什么算高危命令**、**为什么这样设计是对的**。
> §3.1 给判定精度的四条修正口径，§7 登记已知取舍（B/D/E/F/不对称/口径差异）——
> 这些是有意不做的，不是漏掉的。
>
> 行号会漂，判定逻辑不会：**本文锚点一律用符号名**（改符号会编译/收集失败，改行号不会）。
> 改任何一条规则前先读 `backend/agent/harness/safety.py` 本身。

## 0. 一句话结论

被约束的对象是**一个无判断力、可被远端输出注入、能一次动作打 N 台机器的自主代理**。
所以边界的形态是：**默认不信任 + 单一判定源 + 逐条命令绑定授权 + 生产环境只认预登记**，
而不是"枚举坏命令然后拦住"。枚举永远不全，默认审批才兜得住。

## 1. 威胁模型（防谁）

| 威胁 | 边界内的应对 | 边界外的依赖 |
|---|---|---|
| 模型误判/幻觉，执行破坏性命令 | 内容级分类 + dangerous 逐条发卡审批 + 未决期间该命令不放行（兜底到期也不放行） | — |
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
| 1 | 命令风险分类器 | `safety.py` 的 `classify_command` / `classify_constructed_command` | 唯一判定源，产出 `hardline / dangerous / safe` | 否（无开关） |
| 2 | 策略层拦截器 | `reins/guards.py` 的 `FilesystemGuard`(priority 10)、`reins/gate.py` 的 `PermissionGate`(priority 8) | 按**内容**或工具级风险决定 阻断/审批/放行 | `safety_critical=True`，异常即拦截；`contracts.py` 无禁用开关 |
| 3 | 执行前底线复检 | `saddle.py` 的 `Sandbox.validate` | 不可禁用的第二道 hardline 复检 | 否（**当前仅覆盖 `shell_exec`**，见取舍 E） |
| 4 | 远端纵深 | `scripts/deploy/agent-exec.py` + `ckunagent` + `ForceCommand` | 不依赖任何一端自觉的真上限 | 由部署侧决定 |

拦截器执行顺序按 priority **升序**，即 `PermissionGate(8)` 先于 `FilesystemGuard(10)`。
两条入口规则：

- `PermissionGate` 对 `_COMMAND_EXEC_TOOLS`（`shell_exec`/`job_run`）直接放行给守卫，
  避免工具级 `risk_level` 一刀切挡住内容级判定；对**自报 `bound_command` 且内容非 safe**
  的工具同样让位（`gate.py` 的绑定命令让位分支），因为 dangerous 的审批三态/超时/拒绝记忆/prod 闸门
  已在守卫里实现 —— 同一判定有两处实现必然漂移。
- 内容 safe 的绑定命令工具仍回到 ToolMeta 流程，但**授权键仍绑定该命令**（§4）。

处置语义：

| 判定 | 处置 |
|---|---|
| `hardline` | 无条件阻断，allowlist 也放不走（守卫在 allowlist 之前判定），审计 `conclusion=blocked`，注入 `[系统拦截]` 让模型换路 |
| `dangerous` | 发审批卡片；**未决期间该命令不放行**。审批不再设"超时即拒"的短窗口 —— 待授权签名保持等待至 grant/deny/会话结束，只留一个可配置的**兜底时长**（`reins/core.py` 的 `approval_fallback_seconds()`，默认 600s，设置面板 PUT `/settings/permission-policy` 即时生效，0 = 永久等待，上限 86400s）：到期发 `permission_expired` 把卡片标记为超时并**保留待办**（不移出等待集、同命令重发复用待办而不重复建卡），审计记 `denied` 且 `result` 带 `approval_expired`。prod 环境只认**预登记 allowlist**，临时审批一律拒（`prod_deny_no_allowlist`） |
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

枚举表要挡的是"没见过的"，不能因此把"见得到的正常排查"打成高危。修正口径：

| 修正 | 规则 | 位置 | 挡掉的误判 |
|---|---|---|---|
| **执行位置** | 电源动词与 `sudo`/`su` 只在**执行位置**命中：剥掉包装器（`sudo`/`nohup`/`env`/`xargs -a f`）、shell 关键字（`do`/`then`/`for`）、赋值与括号后落在首位的动词。首 token 是解释器（`bash -c 'reboot'`）或 awk/sed 段内带 `system(` 时退回整段匹配 | `_classify_exec_position` | `journalctl -b \| grep -i reboot`、`cat /var/log/reboot.log`、`grep -c sudo /var/log/secure` 曾被判 hardline/dangerous |
| **安全设备** | `/dev/null`、`/dev/zero`、`/dev/urandom`、`/dev/stdout`、`/dev/tty*` 等既不是写敏感路径也不是敏感读取；`/dev/tcp/*`（反弹 shell）与 `/dev/sd*`（裸盘）不在其内 | `_is_safe_device` | `cmd 2>/dev/null` 这一族静默写法曾逐条要审批 |
| **路径驱动** | 凭据读取不再要求首 token 是 `cat/dd/...`，任何非 flag 参数命中 `_SECRET_FILE_RE` 即 dangerous；awk/sed 的内联 `system()` 单独成条 | `_classify_secret_read` | `sort /etc/shadow`、`awk -F: '{print $1}' /etc/shadow`、`wc -c /root/.ssh/id_rsa` 曾判 safe |
| **写开关** | 只读白名单动词的写副作用参数按写目标/独立成因判定：`sort -o <路径>`、`dmesg -C`、`ifconfig <网卡> down/promisc`、`hostname <新名>`；`kill` 进破坏性动词表（`kill -9 $(pgrep …)` 走间接展开规则）；curl/wget 的上传侧 `-T`/`-F @`/`--data-*`/`--post-file=` 命中凭据或敏感路径 → 外传 | `_classify_write_targets` / `_classify_exfiltration` / `_classify_unknown_default` | `curl -T /etc/shadow ftp://`、`dmesg -C`、`ifconfig eth0 down` 曾判 safe |
| **提权链记忆** | `sudo`/`doas`/`runuser`/`pkexec`/`su` 为了对真正执行的命令做位置判定必须被剥掉，剥完链上就没人记得这是提权跑的——改为在剥离时按真实动词之前的前缀段回收一次。只看执行位置，`sudo` 出现在参数里不算 | `_privilege_verb_in_chain` | `nohup sudo id`、`env sudo ls`、`nice -n 5 sudo id`、`command sudo id` 曾判 safe，一张卡都不出 |
| **动词归一** | 动词比较统一走 `_verb_of`：剥标点 → 去尾部分隔符 → 取 basename → 小写。绝对路径与 ANSI-C 拼接形态必须落到同一个名字上，且"目标分析"与"默认审批"两段共用，不许一段认得、一段认不出 | `_verb_of` | `/usr/bin/rm -rf ~` 曾判 safe（basename 只在默认审批那半生效）；`rm$'\x2f' -rf $'\x2f'` 解码后的 `rm/` 曾退化成 dangerous 兜底 |
| **源侧与多目标** | 写目标扫描覆盖动词的**每一个**文件参数与**源**侧：`tee a b` 的第二个目标、`mv/cp/rsync` 的源、`tar` 归档的源、`dd if=`。tar 的 `--checkpoint[-action]=exec=`、`--to-command`、`-I/--use-compress-program` 等把外部程序接进数据流的开关按 dangerous | `_classify_write_targets` / `_is_sensitive_source` | `echo p \| tee /tmp/innocent /etc/ld.so.preload`（同内容直接重定向是 hardline）、`mv /root /tmp/x`、`tar cf x --checkpoint-action=exec=id /tmp` 曾判 safe |
| **凭据路径的三种真实形态** | 凭据判据只有一条入口 `_is_secret_path`，它同时认：POSIX 斜杠（`_SECRET_FILE_RE`）、Windows 盘符/家目录（`_WIN_SECRET_PATH_RE`：大小写不敏感、`\` 与 `/` 同形、`.ssh` 目录本体与 `id_*` 私钥）、dotenv 家族（`_is_dotenv_path`：`.env` 与 `.env.<环境>`，`.example/.sample/.dist/.template/.tpl/.in/.default/.origin` 八个占位后缀豁免）。读侧与写侧共用这一条入口，`classify_command` 与 `classify_constructed_command` 因此不可能给出两个答案 | `_is_secret_path` / `_WIN_SECRET_PATH_RE` / `_is_dotenv_path` | `cat C:\Users\alice\.ssh\id_rsa`、`cat C:/Users/alice/.ssh/ID_RSA`（Windows 上就是同一个文件）、`cat /app/.env` 曾判 safe —— 而 §4 早就把 `.env` 写成敏感落点，文档声明的边界在代码里不存在 |
| **植入前缀的路径边界** | 持久化前缀按 `t == p or t.startswith(p + "/")` 命中，与相邻的 `_is_sensitive_path` 同口径；兄弟名字（`/etc/passwdx`、`/etc/shadow_backup`、`/etc/passwd.new`）落到 dangerous 而不是 hardline。表里每项都必须是**完整文件名或完整目录名**：drop-in 目录（`/etc/sudoers.d`）单独登记，不再靠裸前缀顺带盖住 | `_is_persistence_target` | 裸前缀把"备份一份再编辑换回"这类标准手法永久封死（hardline 没有审批出口、预授权也放不走）；真身那一段 `mv /etc/passwd.new /etc/passwd` 照旧 hardline。`/etc/cron.d` 按契约表仍钉 dangerous，两种姿态是有意的 |

误拦与误放是同一枚硬币的两面，所以回归测试 `test_safety_misjudgment.py` 双向都钉：
上面每一条修正都带一组"必须仍是 hardline/dangerous"的反向锁（12 种真实 `reboot` 形态、
`rm -rf /`、`dd of=/dev/sda`、`echo x > /dev/tcp/…/4444`）。
收紧侧同样带正向锁：`rm -rf build dist node_modules`、`tar czf /tmp/backup.tar.gz /home/user/project`、
`tar xzf app.tgz -C /srv`、`grep -c sudo /var/log/secure`、`find . -name '*.log' -mtime +7 -delete`
必须仍是 safe —— 归档目标目录（`-C` 的值）不是数据源，把日常备份打进审批卡只是把成本
摊到每次正常操作上。

抗混淆：`normalize_command_for_detection` 做 NFKC、剥引号、剥反斜杠、命令链分段、
子 shell 递归，然后分段送分类。剥反斜杠（抵御 `r\m` 这类拼写混淆）会连带吃掉 Windows
路径的分隔符，所以它前面有一步 **Windows 路径折算**（`_WIN_PATH_FOLD_RE`：只折盘符
`C:\…` 与家目录折叠后的 `~\…` 两段，命令位照旧被还原）—— 没有这一步，
`cat C:\Users\alice\.ssh\id_rsa` 会先变成 `cat C:Usersalice.sshid_rsa`，形状没了，
后面每一条路径判据都无从命中。

两种口径，差别只在要不要默认审批：

| 口径 | 函数 | 用于 | 理由 |
|---|---|---|---|
| 自由文本 | `classify_command` | `shell_exec`、`job_run`、`schedule_op(create)` 的 command、批量下发端点 | 模型/人写的原文，未知即审批 |
| 客户端构造模板 | `classify_constructed_command` | `file_write`/`file_edit`/`file_read`/`file_grep`/`log_read` 的 `*_cmd` 模板 | 模板自造（含 `base64`），关掉默认审批才不会让写 `/tmp` 也逐条审批；收紧的方向由**路径驱动**的 `_classify_secret_read` 与写目标扫描保证 —— 凭据读取不再依赖动词表（原 `_READ_VERBS` 已删：任何动词命中凭据路径都升 dangerous） |

## 4. 授权键语义（"批准这一条"而不是"批准这个工具"）

`gate.py` 的 `_grant_key`：**只要拿得到命令**，键就是 `tool:<sha256(规范化命令)[:16]>`；
拿不到才是裸工具名。于是：

- `shell_exec` / `job_run`：批准的是那一行命令。
- `file_write` / `file_edit`：批准的是"往这个路径写"（模板里的内容用固定占位符
  `_CLASSIFY_PAYLOAD` 替换 —— 否则每次改文件都要重新授权，且卡片 payload 爆大）。
  **文件内容级风险不在范围内**：对任意文件体做静态分析误报不可控，真正的植入面是路径，已覆盖。
- `file_read` / `file_grep` / `log_read`：绑定"读这个路径"的模板；`file_glob` 不绑定
  （`find -name` 只返回路径名，不外泄内容）。
- `schedule_op`：`create` 的自由文本按 shell 口径判定并绑定；其他 action 回落到工具级。

prod 下 `file_write` 的三档落点（本轮决策：**moderate 工具在 prod 不强制预授权**）：
植入类 → hardline 阻断；敏感目录（`/etc/...`、`/var/log/...`）与 dotenv 家族
（`_is_dotenv_path`，含 `.env.production` 这类环境变体）→ dangerous
且仅预授权、键按路径绑定；非敏感路径（`/tmp/x`）→ 会话授权，等同临时目录写。
再收紧会让生产每次改配置都要预登记，收益/成本不成立。

**once 授权的 run 终态语义**（2026-10-11 P2 修复）：once 键按 run 隔离，run 终态时
`_release_run_interaction_state` → `cleanup_run_once` 清池。因此
`POST /agent/permission/grant` 对 `scope=once` 前置校验目标 run 非终态
（`db.get_agent_run`），行缺失或状态 ∈ `TERMINAL_RUN_STATES` 一律 409 —— 否则
run 结束后（审批兜底到期、SSE 迟到/断连、`settling` 在途窗口）的迟到点击会落进
一个永不被消费也不再清理的死键，前端却显示「已授权」。状态查询本身异常时
告警放行：这条校验防的是死键泄漏 + 假授权提示的**完整性**问题，不是安全闸门
（once 键消费时 PermissionGate 本就还要查表），拿不到证据不等于 run 已死。
前端随同收敛：run 终态后「允许本次」与批量「全部批准本次」禁用
（`utils/approval-scopes.ts` 的 `onceGrantBlocked`，超时角标改写为
"本轮已结束，请在新的运行中重新确认"）；session（按会话键控）与 always（永久）
跨 run 仍可消费，不受影响。残留窄缝：前置读库与 run 收尾清池不是事务（TOCTOU），
读库在途期间 run 恰好终态时死键仍会落成 —— 取舍登记在 §7「once 终态 409 的
TOCTOU 窗口」。plan 姿态侧的配对强制见文件头 2026-10-11 补记 ——
`workflow_mode='plan'` 时服务端把 `permission_mode` 压成 `'plan'`，prompt 声称
只读则 PlanModeGuard 必须激活，两者只发其一就是「亮的空开关」。

**预授权条目可选带保留时长**（设置 → Agent 治理 → 运维策略 → 授权保留时长）：
默认「永久」即本节全部文字的原始口径；改成 24h/7d/30d 后，`_permanent_active`
在查表时把到期键当"从未预授权"处理（prod 危险命令因此回到 `prod_deny_no_allowlist`
拒绝路径，而不是放行），并就地清理 + 落盘。授予时间落在 `allowlist.meta.json`
（与 `allowlist.txt` 同目录的 sidecar），键文件格式不变；缺记录的旧行按
`allowlist.txt` 的 mtime 兜底，所以调小时长不会让既有预授权当场集体蒸发，
但**确实可能**让某条很久没再被保存过的登记到期失效 —— 面板提示里写明了这点。

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

**唯一可由用户关掉的环节是"prod 额外确认"**（设置 → Agent 治理 → 运维策略，默认开）：
关掉后去掉的只有两张 prod 专属提示 —— 审批卡片顶部的红字警示条、批量下发中**仅因目标包含
prod 主机**而弹的确认框。三条主判定不受该开关影响：dangerous 仍逐条审批、prod 危险命令仍
必须预登记 allowlist、hardline 仍无条件阻断（判定接口不可达也仍 fail-closed 拒绝下发）。

## 6. 审计

`backend/agent/audit.py`：链式签名 append-only。`conclusion ∈ {executed, failed, blocked,
denied, auto_allowed, approved, released, revoked, answered, skipped}`（`audit.py` 的
`LEGAL_CONCLUSIONS` 是唯一枚举；三份清单 —— 枚举、审计页筛选下拉
`src/views/ssh/AuditView.vue`、本节 —— 逐字相等，钉在
`backend/tests/test_ai_loop_round13c.py::test_audit_conclusion_三处清单同源`。
非法值仍落 `executed` 以保住列的有界性，但原始结论会追加进 `result` 并记一条 warning ——
过去它是静默改写，等于让一条不可篡改的记录声称「这条命令执行过了」），
含 `environment`、`command_raw`、`run_id`。阻断记 `blocked`、prod 无预授权
与审批超时/拒绝记 `denied`（`result` 里带 `prod_deny_no_allowlist` / `approval_needed` /
`approval_expired` 等成因），
因此"AI 试了什么没成功"同样可追 —— 这是 §5 里人工通道不加事前阻断的前提。

## 7. 已知取舍登记（有意不做，不是漏了）

| 编号 | 内容 | 为什么接受 | 真上限在哪 |
|---|---|---|---|
| **B** | `environment` 由客户端声明（`routes/agent.py` 请求模型里的 `environment: Literal["test","prod"] = "test"`，来源是渲染层抄连接标签），后端无主机清单、不校验 `ssh_session_id ↔ environment` | 后端不持有可信主机清单，派生不出真值；加校验需要先做主机资产 | prod 强化是"客户端诚实则诚实"的闸门；上限是远端 `ckunagent` 最小权限 |
| **D** | 间接 prompt 注入：远端输出以 `role=tool` 原样回灌，无不可信标记（`ops_common.py` → `driver.py`），且记忆/反思/host_profile 三条持久化回路会以 system 消息注回 | 内容级标记与被注入检测是独立一轮工作，误报面大 | dangerous 命令仍必经人批；prod 仅预授权 |
| **E** | ① `Sandbox.validate` 第二层仅覆盖 `shell_exec`（`job_run` 与绑定命令工具无第二道）；② "始终允许"无二次确认（allowlist TTL 本轮已补，但**默认仍是永久**，且 TTL 只作用于 `always` 条目 —— once/session 各自按 run/会话终态清理）；③ `agent-exec.py` 的规则副本既没有归一化层，也仍是**整串**电源动词匹配 —— 后端改成执行位置判定后远端更严（`grep -i reboot` 在远端会被挡），方向安全但不等价 | 均为收敛度问题，不改变"默认不信任"的骨架 | 契约测试 `test_remote_whitelist_contract.py` 钉跨端一致性 |
| **F** | 判定精度上仍接受的四处（本轮误判审查后剩下的）：① 相对路径递归删除 `rm -rf ./build`、`find ./logs -delete` 判 safe；② 在日志里 grep 字面量 `grep -n 'rm -rf /' audit.log` 仍 hardline；③ `env \| grep -i pass`、`cat /proc/1/environ` 判 safe；④ 无子命令的裸 `docker`/`kubectl`（含 `command -v docker 2>/dev/null` 这种探测写法）判 dangerous | ①绝对路径与敏感目标已全覆盖，相对路径的破坏面等于该账户本就能写的范围，收紧会让每次 `make clean` 都弹卡；②根删除保持整串口径，位置化会把 `find / -exec rm -rf {} +` 从 hardline 降成 dangerous，得不偿失；③读取进程环境既不是写操作也不命中凭据路径表，加黑名单会让所有 `env`/`printenv` 排查都要审批 —— 真正的风险是输出内容进模型上下文，与取舍 D 同源；④保守方向，成本是一张卡 | ①远端最小权限账户；②③④审计链 + dangerous 必经人批 |
| **不对称** | `file_read/file_glob/file_grep` 在无 SSH 会话时降级读**本机**文件（仅 `..` 校验），与 `shell_exec` 的严格 fail-closed 不一致 | 有意的产品功能（本地文件浏览） | 路径遍历校验 + 审计 |
| **凭据形态** | 收紧后的两处剩余面：① **主机私钥**不在表里（`cat /etc/ssh/ssh_host_rsa_key`、`cat C:/ProgramData/ssh/ssh_host_rsa_key` 实测均 safe）——POSIX 侧原本就没有这条规则，Windows 侧按"与既有覆盖面严格对齐"补，加宽它属于另一轮决策；② **UNC 网络共享**不折算（`cat \\srv\share\.ssh\id_rsa` 实测 safe）：无盘符前缀，而 POSIX 里以 `\\` 开头的 token 按 shell 语义是转义后的字面反斜杠，折成 `/` 会把 `\etc\passwd` 这类字面文件名当成 `/etc/passwd` 凭空误拦 | ①②都是覆盖面与凭空误拦的交换，本轮只钉"同一个文件换写法不换结论"这一条不变量 | 误拦侧实测：`find /data -name .env`、`--exclude=/data/app/.env` 这类"提到 dotenv 文件名但不读它"的写法现在会多一张审批卡（判据只看名字段是不是 dotenv，分不清是读取还是模式串）；ANSI-C 形态的盘符路径 `$'C:\\Users\\…'` 实测**已**覆盖（转义解码在前、折算在后），不要以为它和 UNC 一样漏；真上限是"读得到本机文件"这条不对称本身（上一行）+ 审计链，`.env.example` 家族与 `backend.env`/`.environment` 兄弟名字的豁免、上面每条实测都由 `test_safety_windows_and_dotenv_paths.py` 钉住 |
| **口径差异** | 批量下发换用后端唯一判定源后，`kill -9 <pid>`、`cp` 非敏感路径判 safe → 不再弹确认；此前渲染层自带词表对任意 `kill/cp/mv` 都会问一句 | 保留第四套词表换取的收敛；与 AI 通道同口径 | 同一判定源、同一后果说明 |
| **出网正文解析** | `web_fetch` 的 URL 由模型选，且它 `risk_level="safe"`、在计划模式白名单里 ⇒ 不需审批就能走到。三道新上限各自的取舍：① Content-Type **缺失时放行**（大量静态站与旧服务器不给这个头，一票否决会让功能在真实环境不可用；长度与解析窗口仍受约束）；② 解析挪进总时限并 `to_thread` —— 线程**不可强杀**，所以真正兜底是「线性扫描 + 1M 解析窗口 + 类型闸门」而不是那个 timeout；③ 未闭合 `<script>` 现在按 HTML 语义把开标签之后的内容整段丢弃（浏览器就是这么解析的），代价是被截断的页面少一段正文 | 修复前实测：4MB 正文 + 800 个未闭合 `<script>` 解析要 **28.66s**（非贪婪 DOTALL 每个未闭合标签重扫全文，标签 8 字节一个 ⇒ 10MB 内可放几十万），且这段同步 CPU 写在 `asyncio.timeout` 之外 ⇒ 事件循环被钉住，本进程所有 run/SSE 一起停；10MB 二进制经 `decode(utf-8, replace)` 后仍占约 60MB 峰值再进正则 | `test_web_fetch_body_parse_bounds.py`（含「解析必须在 timeout 之内、且走 to_thread」的位置锁）；真上限是「一次抓取最多占一个线程 + 1M 字符解析窗口」，仍没有按主机配额限并发 |
| **子命令形态** | 白名单子命令里「既能列又能写」的那几条（`git branch` / `git tag` / `git remote`）现在按**形态**判定：出现任何未登记 token（含位置参数）即按写处理。换来的误拦实测有两处：`git tag -l 'v1*'` 的 pattern、`git branch --no-merged devel` 的 commit 都是位置参数 ⇒ 多一张审批卡。**另一侧不变**：`sort -o <file>`、`tee <file>` 这类"往非敏感路径落盘"在审批层仍判 safe（Agent 的正常工作区就是能写文件，`file_write` 另有闸门），只有计划模式把它挡下 | 误拦侧成本是一次点击；误放侧 `git remote set-url` 换掉远端后每次 push/fetch 都把代码交给新地址，是 supply-chain 路数，不是"改改本地状态" | 后果表已登记新 reason（`writes repository state`）；`test_safety_git_form_and_glued_flags.py` 双向钉：13 条写形态必经审批 + 16 条只读形态不被误拦 |

| **出网重定向** | 两条 HTML 搜索腿原本给 `netconfig.new_client` 传 `follow_redirects=True`，而落地地址从未校验（`_read_capped` 只回 (body, 编码)，最终 URL 根本没交回来）：Location 由对端决定 ⇒ `302 → http://127.0.0.1:8080/`、`302 → http://169.254.169.254/latest/meta-data/` 走的都是同一条请求链。现在不交给 httpx 跟：`_read_search_html` 自己跟，每一跳**先**按 web_fetch 那条公网规则复核（规则本体合并成单一入口 `_resolve_public_destination`，此前 fetch 里还另抄了一遍协议/空 host 判断）折好的 URL，再发请求；跳数与 fetch 共用 `_MAX_REDIRECTS`。剩余两面：① **不固定 IP** —— fetch 用解析出的 IP 直连 + `sni_hostname`，搜索腿把解析交给 httpx，所以「校验时解析到的地址」与「实际连接的地址」之间仍有 DNS 窗口，且 `gethostbyname` 只看一条 A 记录（多 A/AAAA 时可能校验一个、连另一个）；② Tavily 那条仍然不跟随（`api_key` 在 JSON body 里，307/308 会把 body 原样重投给 Location 指定的主机） | ①固定 IP 要绕开 netconfig 的统一代理出口并自己管 Host，为两条硬编码的公开搜索站不值当；②跟随的收益为零（那是 API 端点），代价是泄密钥 | 误拦侧实测：起始 URL 不校验（照旧直连，内网自建代理不受影响），只有跳转目标解析到内网/保留段才拒；**新增失败面**是「一次跳转解析两次」—— DNS 不通的环境里原本由 httpx 独自解析成功的路径，现在会先报 `DNS 解析失败` 再判搜索失败。误放侧真上限就是那个 DNS 窗口（①）。前提证明：`test_web_search_redirect_ssrf.py` 用真 httpx + MockTransport 钉住「`follow_redirects=True` 确实会一路打到 `http://127.0.0.1/admin` 并读回正文」；对照 pristine HEAD 副本跑 30 条用例 26 红，4 条绿的都是「行为不变」锁（Tavily 两条 + fetch 跳数常量 + 那个 httpx 前提） |

| **参数键归一** | 判定层与执行层曾读不同的参数键：`command_from_args` 按 `command→shell→cmd` 取**第一个非空**，而 `JobRunTool.execute(**kwargs)` 只读 `cmd` ⇒ `{"command":"echo DECOY","cmd":"rm -rf /var/log"}` 让 FilesystemGuard / PermissionGate / 计划模式 / Sandbox 底线 / 澄清五层只看得到 echo，真正跑的是删库。现在提取器把三键的值**全部**纳入判定（换行拼成多段，分类器逐段判），两条命令入口也各自拒绝歧义（job_run 显式报错、shell_exec 因位置签名天然 TypeError）。剩余面：`ToolRegistry.execute` 仍不对照 `tool.parameters` 校验 args ⇒ 非命令类工具的多余键仍被静默接收。本轮实测内置工具**没有一处**读取未声明的参数键（脚本比对各类 `kwargs.get("x")` 与自身 `parameters.properties` 的差集，0 例），所以多余键今天改变不了执行内容 —— 但没有锁：这条不变量不在测试里，未来加 `**kwargs` 工具读新键就可能破 | 全局 schema 校验会一次改掉所有工具对模型脏参数的容忍度（模型今天多填 `explanation`/`purpose` 仍能跑通），收紧属于产品口径决策，不该夹在审批绕过的修复里 | `test_command_key_decoy_bypass.py`：14 条用例里 10 条在修复前红（含「整条审批链对 decoy 形状零拦截」这条端到端），另 4 条是「行为不变」锁（单键调用、重复同值、正常 cmd 空值报错、shell_exec 位置签名）修复前后都绿；`_COMMAND_ARG_KEYS` 是五层唯一的命令键来源，Sandbox 底线与 gate 的注释都指向它 |
| **裁决同源** | 自动审批（permission_mode="auto"）的 LLM 裁决器与主 Agent 用**同一个模型配置**（`ctx.llm_base_url/api_key/model`）：提示注入若能操纵主模型，原则上也能以相近方式影响裁决模型 | 裁决上下文与主循环隔离（只见命令原文/风险理由/任务摘要，**不含工具输出**——注入主要经由工具输出进入），且 fail-closed（超时/异常/输出不合约定/总闸关闭一律回退弹卡）+ 不越 hardline / prod 预授权 / 用户 run 内显式拒绝三道既有边界；换独立模型做裁决的收益未证实，还多一套模型配置与运维面。登记此条是为了不把"模型审模型"误读成双保险 | dangerous 命令仍必经人批；prod 仅预授权；env 总闸 `CKUN_AGENT_DISABLE_AUTO_APPROVE` 可一键退回纯手动（优先级压过设置页开关） |

| **once 终态 409 的 TOCTOU 窗口** | grant 的 run 状态前置校验经 `asyncio.to_thread(db.get_agent_run)` 读库：读库在途期间 run 恰好收尾清池，返回后落库的 once 键仍是死键；同理 `release-run` 端点已清池而 DB 行还是 `running`，校验也放行 | 读库返回到 `pg.grant` 之间无 await，事件循环单线程上不会与 cleanup 交错 —— 窗口只剩「读库在途」那一小段。彻底闭合需要活 run 注册表与 `cleanup_run_once` 同源摘挂（driver 防御清理 / ReflectionRein 终态 / release-run 三条通路都要接线），收益/成本不成立 | 死键只是永不被消费、不产生错误执行（once 消费时 PermissionGate 本就按 run 池查表，不扩权）；前端 `onceGrantBlocked` 先行压制绝大多数迟到点击，剩下的只有 SSE 断连与 run 终态赛跑的窄缝 |

## 8. 改判定规则要跑什么

```bash
cd backend && python -m pytest -q \
  tests/test_safety.py tests/test_safety_default_approval.py \
  tests/test_safety_misjudgment.py \
  tests/test_command_explain.py tests/test_ops_content_classification.py \
  tests/test_permission_gate_content_bound_tools.py \
  tests/test_permission_approval_lifecycle.py tests/test_audit_coverage.py \
  tests/test_permission_policy_round18.py tests/test_grant_dead_run.py \
  tests/test_remote_whitelist_contract.py tests/test_safety_classify_endpoint.py \
  tests/test_safety_windows_and_dotenv_paths.py \
  tests/test_safety_git_form_and_glued_flags.py \
  tests/test_web_fetch_body_parse_bounds.py \
  tests/test_web_search_redirect_ssrf.py tests/test_web_search_response_caps_s628.py \
  tests/test_command_key_decoy_bypass.py \
  tests/test_plan_mode.py
npx vitest run && npm run typecheck
```

约束：新增工具若能产生写盘/读凭据/自由命令副作用，必须实现 `BaseTool.bound_command`
（`agent/tools/__init__.py` 的 `BaseTool.bound_command`）并在 `test_ops_content_classification.py` 的契约表里登记；
不纳入的也要登记，否则后人无法区分"决策"与"漏了"。
`test_command_explain.py` 逐条断言每个 `reason` 都有中文后果说明 —— 加规则要同时加解释。
`test_safety_misjudgment.py` 是双向锁：往只读白名单 `_SAFE_VERBS` 里加动词，必须同时给它配
"写副作用开关"的反例（`dmesg -C` / `sort -o` / `ifconfig eth0 down` / `hostname x` 四个先例）；
往枚举表加豁免，必须同时给出"真实破坏形态仍被判高危"的反向锁样本。
