# Agent 意图识别、安全边界与审计优化设计

- 日期：2026-10-23
- 状态：待用户评审
- 范围：`backend/agent/intention.py`、`backend/agent/harness/` 安全与审批链、审计接口与审批 UI
- 目标：削减只读误判与重复审批，避免过度人工审核；不放宽写操作授权

## 1. 背景与实测结论

现有链路已经具备：确定性命令分级（`hardline` / `dangerous` / `safe`）、主机级 allowlist、prod 预登记、LLM 自动审批、计划模式只读强制、哈希链审计。

本次对 84 条常见运维只读命令做分类实测，31 条仍被判 `dangerous`。其中约 29 条是无歧义的只读误判，主要来自：

1. 未知命令默认审批：`jq`、`dig`、`lsblk`、`docker stats`、`crontab -l`、`iptables -L`、`git ls-remote` 等。
2. 引号内分隔符被误切：

   ```bash
   grep -E 'error|warn' /var/log/app.log   # 被切成两段，第二段 warn' 成为未知命令
   awk '/error|warn/{print}' /var/log/app.log
   grep 'a;b' /var/log/app.log
   ```

3. 个别命令的 `passwd` 关键字在参数位被误判为改口令：`getent passwd`。

同时实测发现三个破坏性写操作当前被判 `safe`，属于边界一致性缺陷：

```bash
date -s '2026-10-23 10:00:00'                  # 改系统时钟 → 当前 safe
date -u 010100002026                           # 同上
find /tmp -name '*.log' -delete                # 删除 → 当前 safe
find /tmp -name '*.log' -exec rm -f {} +        # 执行删除 → 当前 safe
tar -xzf app.tar.gz                            # 解包写盘 → 当前 safe
```

## 2. 目标与非目标

### 2.1 目标

- 常见只读运维动作不再弹审批卡，重复只读命令不再重复弹。
- 多步骤任务中只读部分完全静默；写操作仍逐条确认，但卡片按 run/主机聚合，减少弹窗打断。
- 意图识别更可评测、可解释，并用于减少误发写命令，不参与授权。
- 每次只读自动放行都有规则 ID 与审计记录，可按原因、命令、主机、意图复核。

### 2.2 非目标

- 不自动放行任何写操作。
- 不改变 prod “危险命令仅接受预登记 allowlist”的规则。
- 不引入 LLM 做意图分类或只读判定。
- 不收紧 `wget` 默认落盘、`curl -o/-O`、`tee`、普通重定向到非敏感路径等既有良性写侧口径（避免审批卡反弹），仅在本文档登记。
- 不改变**计划模式**白名单。计划模式的承诺（“什么都没改”）强于审批门，其白名单必须继续是审批白名单的严格子集；新增只读规则不自动进入计划模式。
- 不做审计表结构迁移；沿用现有 `audit_log` 字段与哈希链。
- 不放宽 `/audit/append`：该通道只允许渲染层真实执行结果（`executed` / `failed`），审批类结论一律服务端写入。

## 3. 不变量

1. **意图不授权**：`IntentionResult` 的任何字段都不能改变命令的安全等级。安全结论只来自命令内容、目标主机与运行环境。
2. **只读规则只扩不放**：只读规则只在“默认审批”这一步把可证明只读的命令提升为 `safe`；hardline、dangerous 正则、写目标、凭据外传、解释器、子 shell、破坏性写守卫等既有升级路径永远优先。
3. **默认拒绝**：解析失败、规则未覆盖、引号未闭合、上下文不确定 → `dangerous`，进入人工审批。
4. **prod 不放松**：危险命令仍只认 `is_permanently_allowed`；只读命令本来就不需要授权，prod/test 一致。
5. **可解释**：每条自动放行的 `safe` 必须带 `source`；由新增只读规则判定的还必须带非空 `rule_id`。既有白名单命令标 `source=legacy_safe_verb`、`rule_id` 为空，不伪装成“只读证明”。每次人工审批请求必须可审计。
6. **计划模式不放松**：`PlanModeGuard` 继续按自己的严格白名单拒绝写操作；新增只读规则不会被自动视为计划模式允许。
7. **审批请求可追**：每次首次弹出的审批卡都写一条 `requested` 审计；审批等待时长、拒绝、超时都能按 `approval_key` 与授权记录配对。

## 4. 设计总览

```text
用户任务
  │
  ├─ 意图层 intention.py          → task_type / level / is_change / confidence / readonly_expected
  │                                 （只影响提示词、工具路由、卡片上下文、审计，不影响授权）
  │
  ├─ 安全层 command_parser.py     → 归一化 / 引号感知分段 / 包装器剥离 / token 提取
  │   safety.py                   → hardline / dangerous / 写目标 / 凭据 / 解释器 / 子 shell
  │   readonly_policy.py          → 只读证明（规则 ID）+ 破坏性写守卫
  │                                 （只有“默认审批”这一步会被只读规则替代）
  │
  ├─ 审批层 reins/gate.py         → 写操作逐条授权；prod 只认预登记；不新增危险会话授权
  │   reins/guards.py             → 命令级判定、审批卡、审计、意图漂移标记
  │   ApprovalDock / Card         → 按 run/主机聚合；写操作仍逐条确认
  │
  └─ 审计层 audit.py              → 规则 ID / 决策来源 / requested 记录 / 统计端点
```

## 5. 意图识别

### 5.1 数据结构

`IntentionResult` 新增：

```python
readonly_expected: bool = False
```

推导规则：

```text
readonly_expected = (not is_change) and task_type in {
    QUERY, RESEARCH, DEBUG, CONVERSATION
}
```

- `CONVERSATION` 仍会清空工具；`readonly_expected` 只用于其它路径。
- `is_change=True` 的任务始终为 `False`，避免把“清理日志”“重启服务”等误标为只读。
- `classify()` 的四个 `IntentionResult(...)` 构造点都必须按最终 `task_type` / `is_change` 推导该字段，而不是只改其中一处；`to_dict()` 增加 `readonly_expected`，driver 已用 `intention.to_dict()` 写入 `ctx.metadata["intention"]`，无需改 driver。
- `reclassify()` 原样透传 `readonly_expected`，不因执行过程中的工具结果改变任务性质。

### 5.2 提示词引导

新增 `_READONLY_DISCIPLINE`，仅当 `readonly_expected=True` 时由 `get_guide_for_iteration()` 追加（driver 已把该返回值作为最末一条 user 消息注入）：

```text
[只读纪律] 本任务判定为只读/排查。优先使用只读工具；不要执行会改变目标机状态的命令。
确需变更时，先说明变更内容、影响范围与回滚方式，等待用户批准后再执行。
```

目的：减少模型误发写命令导致的审批卡，不是授予任何权限。写命令仍会被安全层弹卡。

低置信度（`confidence < 0.55`）时只追加同样的只读纪律，不改变授权；置信度只在卡片与审计中展示，用于事后判断。

### 5.3 意图评测集

新增 `backend/tests/fixtures/intention_corpus.jsonl`，字段：

```json
{"task": "看下 nginx 为什么 502", "task_type": "debug", "level": "debug", "is_change": false, "readonly_expected": true}
{"task": "重启一下 nginx", "task_type": "modify", "level": "simple", "is_change": true, "readonly_expected": false}
{"task": "在吗，服务器挂了", "task_type": "debug", "level": "debug", "is_change": false, "readonly_expected": true}
```

覆盖：中文运维口语、问候+动作、事故描述、只读/变更/调试边界、英文同义词、含路径/数字/日志字面量。测试断言 `task_type`、`is_change`、`readonly_expected`；`level` 对明确样本断言，模糊样本用允许集合。

### 5.4 安全解耦测试

新增 `test_intent_security_decoupling.py`：

- 同一命令、同一环境，只改变 `ctx.metadata["intention"]` 的 `readonly_expected` / `task_type`，`FilesystemGuard` 得到的 `intercept_reason`、风险等级与审批键必须完全一致。
- 静态检查：`safety.py` 与 `readonly_policy.py` 不得导入 `intention.py`。

### 5.5 审计与卡片上下文

- `ctx.metadata["intention"]` 已由 driver 写入；新增字段随 `to_dict()` 透出。
- 审批卡 payload 增加：

```json
{
  "intent": {"task_type": "debug", "level": "debug", "confidence": 0.85, "readonly_expected": true},
  "intent_drift": true
}
```

- `intent_drift` 定义：`readonly_expected=True`，但本次调用需要审批（命令被判 `dangerous`/`hardline`，或工具级风险不是 `safe`）。守卫/权限门在审计 detail 中记录 `intent_drift=1`，卡片显示“任务本应只读，此命令会改变状态”。**不改变授权结果**，只提高用户判断力并留痕。
- 命令级卡与工具级卡共用同一判定，避免只有 `shell_exec` 有漂移提示、`service_manage` 写操作没有。

## 6. 安全边界：只读证明

### 6.0 先区分两个概念：`safe` ≠ `readonly`

现有 `_SAFE_VERBS` 是**免审批集合**，不是只读集合：`tee`、`wget`（默认落盘）、`curl`、`find`、`journalctl`、`echo > file` 都在里面。本轮不改变这个口径，否则会把既有良性写操作全部推回审批卡。

因此引入 `readonly` 作为**更窄的证明**：

| 概念 | 含义 | 本轮变化 |
|---|---|---|
| `safe` | 不需要人工审批 | 只扩大：新增只读规则把未覆盖的只读命令提升为 `safe` |
| `readonly` | 可证明不改变目标机状态 | 新增证明；用于提升未知命令、审计标签、卡片/摘要展示 |
| `legacy_safe_verb` | 旧 `_SAFE_VERBS` / `_SAFE_VERB_SUBCOMMANDS` 判定的 safe | 行为不变；`rule_id` 为空，不冒充只读证明 |

新增只读规则只负责**旧白名单未覆盖的命令**和**破坏性写守卫**，不重复定义旧表已有结论；两表冲突时取更高严重度，并由测试锁定。

### 6.1 解析器拆分

新增 `backend/agent/harness/command_parser.py`，从 `safety.py` 迁出纯解析函数，`safety.py` 保留同名 re-export 兼容既有导入：

- `normalize_command_for_detection()` / `get_normalized_for_display()`
- `split_command_chain()` —— 新的引号感知顶层分段
- `strip_prefix_wrappers()` / `verb_of()` / `first_exec_verb()`
- `tokenize_segment()`

**引号感知分段规则**：

1. 在**原始命令**上分词，而不是在剥引号副本上切分。使用 `shlex`（`posix=True`、`punctuation_chars=';&|()'`）取得 token；raw 段保留原始引号文本，token 只用于该段的动词/参数分析。
2. 跟踪括号深度（`$(` 与裸子 shell `(`）：深度 > 0 时 `;`、`|` 不作为顶层分隔符；`$()`/反引号内层仍由 `_classify_subshells` 递归分类。引号内的 `$()` 通常已是单个 token，不会被误切，段级检测副本仍会看到它。
3. 跟踪反引号成对区间，区间内不切分。
4. 顶层分隔符：`;`、`&&`、`||`、`|`、单独 `&`、换行。
5. 每段保留 raw 与 detection 两种形态：
   - raw：原始段文本，用于 `shlex` token 分析；
   - detection：`normalize + strip quotes`，用于该段的正则与路径扫描。
6. 整串检测继续使用 `_detection_copy(original)` 跑 hardline/dangerous 正则，不做分段。这样 `grep 'rm -rf /' 日志` 仍按已登记取舍判 hardline，而 `grep 'error|warn'` 不再被误切。
7. 路径/目标判定（敏感路径、写目标、凭据读取）继续使用归一化 detection 文本，不依赖 `shlex` 对 Windows 反斜杠的转义语义。

解析失败（未闭合引号、`shlex` 抛错）→ 该命令直接 `dangerous`，原因 `unparseable command (conservative)`。

### 6.2 只读规则表

新增 `backend/agent/harness/readonly_policy.py`：

```python
@dataclass(frozen=True)
class ReadonlyVerdict:
    safe: bool
    rule_id: str
    reason: str | None
```

规则类型：

- `SIMPLE_VERBS`：动词本身只读，除非命中 `write_flags`。
- `SUBCOMMAND_RULES`：第一个非 flag token（或二级子命令）必须在白名单。
- `FLAG_RULES`：仅特定 flag 组合只读（如 `crontab -l`、`iptables -L`）。
- `DESTRUCTIVE_WRITE_RULES`：已知写副作用，返回 `safe=False` 并给出原因；用于 6.5 的修复。
- `EXEC_INJECTION_RULES`：参数可把执行权交给外部程序，返回 dangerous（`git -c`、`tar --to-command`、`openssl -engine`、`awk system(`）。

规则 ID 命名：`ro.verb.jq.v1`、`ro.sub.docker.v1`、`ro.flag.crontab.v1`、`rw.destructive.find_delete.v1`、`ex.inject.git_config_env.v1`。

判定入口：

```python
def classify_readonly(segment: str) -> ReadonlyVerdict
def classify_destructive_write(segment: str) -> ReadonlyVerdict
def classify_exec_injection(segment: str) -> ReadonlyVerdict
```

三者都不直接决定最终等级，只回答“是否可证明只读 / 是否已知破坏性写 / 是否执行注入”。最终严重度由 `safety.py` 取最大值。

**只读证明的否定条件**（任一命中即 `safe=False`，交回默认审批）：

- 段内出现写重定向（`>`/`>>`，`/dev/null` 除外）、`tee`、`cp`/`mv`/`install`/`ln`/`rsync`、`sort -o`、`sed -i`、`tar -c/-r/-u/-x`、`git` 写形态等；
- 段内出现命令替换、子 shell、解释器、`sudo`/`su`、`env` 前缀等既有升级形态；
- 段内出现 `curl`/`wget` 等默认落盘的取回动词（它们可以 `safe`，但不能标成 `readonly`）。

只有“动词与参数都落在只读规则内，且没有上述任何写形态”的段才会拿到 `ReadonlyVerdict(safe=True)`。这样审计里的 `source=readonly_rule` 才真的能作为“没有改状态”的证据，而不是“没弹卡”的同义词。

### 6.3 判定顺序（不可调整）

在 `safety.classify_command_detailed()` 中：

1. `sudo -S` 密码守卫 → hardline。
2. 全串 hardline 正则 → hardline。
3. 全串 dangerous 正则 → dangerous。
4. 全串写目标扫描（重定向/tee/cp/mv/tar/curl/wget）→ 最高严重度。
5. 顶层分段，对每段：
   1. 段内 hardline/dangerous 正则；
   2. 执行位置判定（电源动词、sudo/su、mkfs/dd）；
   3. 间接展开 / 子 shell 递归 / 解释器；
   4. 凭据读取 / 外传；
   5. **执行注入守卫**（`git -c`、`tar --to-command`、`openssl -engine`、`awk system(`）→ dangerous；
   6. **破坏性写守卫**（`date -s`、`find -delete`、`tar -x` 等）→ dangerous；
   7. **只读证明**（仅当以上全部未命中且 `apply_default_approval=True`）→ safe；
   8. 默认审批 → dangerous。
6. 跨段下载执行启发式。
7. 取最高严重度；`hardline > dangerous > safe`。

要点：只读证明位于最后一步，永远不能覆盖 1-5 的任何升级结果。

实现落点：现有 `_classify_unknown_default()` 同时承担“旧白名单命中 → safe”和“未知命令 → dangerous”两件事。新增证明必须插在**它前面**（或把该函数拆成 `known_safe` 与 `default_approval` 两段），否则未知命令会先被默认审批判成 `dangerous`，只读规则永远跑不到。旧白名单命中的命令继续保持 `legacy_safe_verb`，不进入新证明。

### 6.4 本轮新增只读规则

| 类别 | 命令 | 限制 |
|---|---|---|
| 文本/数据 | `jq` | 允许；`--rawfile/--slurpfile` 的读取路径仍走凭据/敏感路径判定 |
| | `yq` | `-i/--inplace` 除外 |
| | `getent passwd/group` | `getent shadow/gshadow` 仍 dangerous |
| 网络诊断 | `dig`、`nslookup`、`host` | 允许 |
| | `ping` | `-f/--flood` 除外 |
| | `traceroute` | 允许 |
| 系统观察 | `lsblk`、`blkid`、`findmnt`、`dmidecode` | 允许 |
| | `smartctl` | 仅信息旗标（`-a/-i/-H/-A/-x/-l/-n` 等）；`-t/-X/-s/-o/-S` 等测试/改设置除外 |
| | `ethtool` | 仅无参或信息查询（`-i/-k/-S/-g/-l/-a/-c/-T/-u/-n`）；`-s/-K/-A/-G/-L/-P/-r/-p/-t/-d/-e/-f` 等改状态/测试/物理动作除外 |
| | `tc qdisc show`、`nft list` | 只认 `show`/`list` 子命令 |
| | `iptables -L/-S` | 短旗标组合逐字符展开；只允许 `n/v/x/L/S` 等展示字符，含 `F/Z/A/I/D/P/N/X/E/R` 一律 dangerous |
| 容器/编排 | `docker ps/images/logs/inspect/stats --no-stream/system df/version/info` | 二级子命令白名单；`docker stats` 必须带 `--no-stream` |
| | `docker compose ps/logs/config/images/top` | 二级子命令白名单；`up/down/restart/exec/run` 等除外 |
| | `kubectl rollout status/history`、`auth can-i` | 二级子命令白名单 |
| | `helm show` | 二级子命令白名单 |
| 归档/证书 | `unzip -l/-t/-v`、`zipinfo` | 解包仍 dangerous |
| | `openssl x509` | 仅信息输出旗标 + `-noout`；无 `-out/-engine/-provider/-provider-path/-signkey/-CAkey` |
| | `tar -t/--list` | `-x/-u/-r/-A/--delete`、`--use-compress-program/--to-command/-I` 除外 |
| 其他 | `git ls-remote`、`git config --get/--get-all/--list/-l` | `git config` 写值除外；全局 `-c/--config-env/--exec-path/--upload-pack/--receive-pack` 视为执行注入 → dangerous；`ls-remote` 拒绝 `ext::`/`file://`/`fd::` transport |
| | `systemctl list-dependencies` | 新增子命令，不动既有白名单 |
| | `crontab -l`、`at -l/-c` | 写/删除除外 |
| | `ssh -V` | 其它 `ssh` 仍 dangerous |
| | `date`（格式化） | `-s/--set` 或任意非 `+format` 位置参数 → dangerous |
| | `find`（纯查询） | `-delete/-exec/-execdir/-ok/-okdir/-fprint/-fprint0/-fprintf/-fls` → dangerous；托管日志目录豁免保持 |

明确不收：`ssh` 连接、`scp`、`rsync` 干跑、解释器、包管理、`sudo`、`find -exec`、`tar` 非列表模式、`getent shadow`、`sed` 的 `e` 命令（见 §13 已知缺口）。

### 6.5 破坏性写守卫（边界一致性修复）

新增 `DESTRUCTIVE_WRITE_RULES`，按段判定：

- `date`：
  - `-s` / `--set` → dangerous；
  - 任一非 flag、非 `+format` 的位置参数（如 `date 010100002026`）→ dangerous；
  - `date`、`date +%s`、`date -d '...'` 保持 safe。
- `find`：
  - 含 `-delete`、`-exec`、`-execdir`、`-ok`、`-okdir`、`-fprint*` → dangerous；
  - agent 托管日志目录（`_agent_log_path`）豁免保持现有行为；
  - 纯查询 `find` 保持 safe。
- `tar`：
  - `-x`、`-u`、`-r`、`-A`、`--delete` → dangerous（`-r`/`-A` 是现有 tar 分支未覆盖的写形态，见 `safety.py:1223` 起）；
  - `-t`/`--list` 保持 safe，但执行钩子另行判定为 dangerous；
  - `-c` 创建归档且目标非敏感路径维持现有口径（本轮不收紧）。
- `journalctl`：`--rotate`、`--flush`、`--sync` 补入 dangerous（现有 `--vacuum` 已覆盖）。

同一节新增 **执行注入守卫**（这类命令的动词在旧白名单里是 safe，但参数能把执行权交出去）：

| 命令 | 危险参数 | 现状 |
|---|---|---|
| `git` | `-c`、`--config-env`、`--exec-path`、`--upload-pack`、`--receive-pack`（可借 ssh/别名执行外部命令） | 新增 |
| `tar` | `--use-compress-program`、`--to-command`、`-I`、`--rsh-command`、`--checkpoint*` | 已有（`safety.py:1223-1245`），本次迁入声明式规则表并保留现有 reason |
| `openssl` | `-engine`、`-provider`、`-provider-path` | 新增 |
| `awk`/`gawk` | 程序文本含 `system(` 或 `| getline` | 新增 |

迁移原则：已有守卫的判定结果与 reason 保持不变，只改“规则写在哪”，避免同一条规则两处维护而漂移；黄金语料同时锁定旧行为。

`getent passwd` 修复：`passwd` 的“改口令”判定改为**执行位置**判定（复用 `first_exec_verb`），参数位出现 `passwd` 不再命中；`passwd root` 的 reason 字符串保持 `password change (passwd)`，现有测试不变。`getent shadow` 由 new rule 明确 dangerous。

新增 reason 必须同步 `command_explain.py` 的静态后果表，否则审批卡会落到“无法进一步判断后果”的兜底文案；`explain_for_reason` 的测试语料要覆盖这些新 reason。判定本身不依赖文案表。

### 6.6 接口与兼容

新增：

```python
@dataclass(frozen=True)
class CommandAssessment:
    level: Literal["safe", "dangerous", "hardline"]
    reason: str | None
    source: str          # hardline_pattern / dangerous_pattern / write_target / secret_read /
                         # exec_position / subshell / exec_injection / destructive_write /
                         # readonly_rule / legacy_safe_verb / default_approval / unparseable
    rule_id: str         # 新增只读规则时非空；其余为空串
    normalized: str

def classify_command_detailed(command: str, apply_default_approval: bool = True) -> CommandAssessment
```

- `classify_command()` 保持 `(level, reason)` 签名，内部调用 detailed 后解包；所有既有调用方与测试零改动。
- `classify_constructed_command()` 保持 `apply_default_approval=False` 语义：客户端自造模板不套只读证明/默认审批，行为不变。
- `FilesystemGuard` 改用 detailed，把 `source/rule_id/intent_drift` 放入审计与卡片事件；`PermissionGate` 的内容检查可继续用旧 wrapper。
- `shell_exec._compute_requires_confirm`（可见终端模式的确认判定）与 `routes/agent.py` 的 `/agent/safety/classify`（批量下发）都直接调用 `classify_command`，新规则会同时减少这两条路径的确认弹窗；补回归测试，避免只优化 headless 路径。
- `command_explain` 逐段分类也复用 `classify_command`，新只读命令的解释会变成“只读”；新增 destructive/exec reason 必须进静态后果表。
- `audit.command_hash()` 继续从 `safety` 导入归一化函数；`command_parser` 迁移后由 `safety` re-export，避免改调用方。
- **计划模式不继承**：`plan_mode.classify_plan_command` 仍先过 `classify_command`，再过自己的 `PLAN_READ_ONLY_VERBS` 严格白名单；新规则不会让计划模式放行 `jq` 等命令。本轮不改这一层。

### 6.7 失败处理

- 解析失败 → dangerous，reason `unparseable command (conservative)`。
- 只读规则内部异常 → 捕获后返回 `safe=False`，reason `readonly_policy_error`，最终走默认审批；不允许异常导致放行。
- 规则表未命中 → 不改变现有默认审批。
- 规则表按需加载，纯内存 O(1) 查找，不引入网络/LLM 调用。

## 7. 审批交互

### 7.1 只读静默

- 只读命令不产生 `permission_needed` 卡片。
- 仅当本次判定 `source=readonly_rule` 时，通过 `ctx.metadata["emit_event"]` 发一个 `readonly_allowed` 轻量事件（与 `auto_approve` 同一通道，避免 driver 非拦截分支丢弃 events）：
  ```json
  {"tool": "shell_exec", "command": "docker ps", "rule_id": "ro.sub.docker.v1"}
  ```
  既有 `legacy_safe_verb`（`ls`/`cat` 等）不逐条发事件，否则长任务会把 SSE 刷屏；它们的审计记录仍在。
- `FilesystemGuard` 判定后把 `CommandAssessment` 写进当前 `ctx.active_tool_call`（按调用记录挂载，不能用单个 `ctx.metadata` 键，否则并行调用会互相覆盖）。`AuditLogger` 在 POST 阶段读同一份记录写 `executed`/`failed`，不重新分类，保证审计与放行判定同源。
- 审计 `result` 采用“原有人类可读前缀 + `detail=` 后缀”，不覆盖既有前缀：
  ```text
  detail=v1;source=readonly_rule;rule=ro.sub.docker.v1;intent=query/trivial/ro/0.82
  ```
  `approval_expired` 等既有前缀原样保留（现有测试按前缀断言），detail 追加在分号后面。
- `emit_event` 失败只记 warning，不影响放行与审计；审计写失败由 `health()` 暴露。

### 7.2 写操作聚合

- `ApprovalDock` 按 `run_id + 目标主机` 分组；同组显示数量、意图、风险摘要。前端 `PendingPermission` 已有 `runId`/`host` 字段，无需新增状态源。
- 每条 `dangerous` 仍有独立批准按钮；**不提供“全部批准”**。现有 `batchApproveBlocked` 只允许 safe/moderate，保持不变。串行工具调用下一次只会有一张卡，聚合主要改善并行调用与跨 run 待办的呈现，不改变“逐条阻塞”的授权语义。
- 卡片新增意图行与 `intent_drift` 警告；`always` 授权文案明确“仅本主机 + TTL + 精确命令哈希”。
- run 结束摘要：`本轮新增只读规则自动放行 N 条 / 人工审批 M 条 / 拒绝 K 条`，由前端按 run 累计 `readonly_allowed`/审批 SSE 事件；run 结束后可用 `/audit/records?run_id=` 对账。可展开查看命令与规则 ID。

### 7.3 不新增的授权能力

- 不为 `dangerous` 新增 session 级免审。
- 不提供“按任务计划一次性批准多条写命令”。
- 不因意图为只读而绕过任何写审批。

## 8. 审计

### 8.1 记录格式

不改 `audit_log` 表结构；在现有 `result` 字段追加“原有人类可读前缀 + detail 后缀”。`approval_expired` 等既有前缀必须原样保留，现有测试按前缀断言，不能被新格式覆盖：

```text
executed:  detail=v1;source=readonly_rule;rule=ro.verb.jq.v1;intent=query/trivial/ro/0.82
requested: detail=v1;source=approval;risk=dangerous;approval_key=<grant_key>;intent=modify/simple/chg/0.80
approved:  scope=always,host=...;detail=v1;source=user_grant;approval_key=<grant_key>;intent_drift=1
expired:   approval_expired: shell_exec;detail=v1;source=approval_timeout;approval_key=<grant_key>
```

- `format_audit_detail(fields: dict[str, str])` / `parse_audit_detail(result: str)` 于 `audit.py`；解析取最后一个 `detail=v1;` 标记。
- 值内的 `%`、`;`、`=` 百分号转义（`%25`/`%3B`/`%3D`），否则 reason 里的分号会把字段劈开；解析端反向解码，未知键忽略。
- `intent` 值固定 `task_type/level/ro|chg/confidence`，`ro` 表示 `readonly_expected=True`，`chg` 表示任务声明为变更；置信度两位小数。
- `result` 已在链式 canonical 中，无需迁移；旧记录无 detail 时解析返回空 dict，不报错。

### 8.2 `requested` 结论

- `LEGAL_CONCLUSIONS` 增加 `"requested"`；命令级守卫与工具级权限门在**首次**发卡时各写一条（同一调用只走其中一条路径）：
  ```text
  action=tool, conclusion="requested", command_raw=...,
  result="detail=v1;source=approval;risk=dangerous;reason=...;approval_key=<grant_key>;intent=..."
  ```
- 重发同一签名不重复记（与现有 `is_first` 逻辑对齐）。`approval_key` 与卡片、授权端点使用同一把键；grant/deny/expired 记录也把该键追加到 detail，统计时按 `run_id + approval_key` 配对。
- **三处清单**是：`backend/agent/audit.py::LEGAL_CONCLUSIONS`、`src/api/audit.ts::AuditConclusion`、`src/views/ssh/AuditView.vue` 的 conclusion 下拉。守卫测试 `backend/tests/test_ai_loop_round13c.py::test_audit_conclusion_三处清单同源` 目前还硬断言 `len == 10`，必须同步改为 11。
- `/audit/append` 只接受 `executed` / `failed`（渲染层通道的真实执行结果）；`requested` 及所有审批类结论只能服务端写入，避免前端伪造审批请求污染统计。`src/api/audit.ts` 的 `AuditAppendInput.conclusion` 相应收窄为 `Extract<AuditConclusion, 'executed' | 'failed'>`。
- `requested` 写入非阻塞；失败计入 `health()`，不阻塞卡片展示。授权结论仍保持 write-ahead/critical 语义。

### 8.3 统计端点

新增 `GET /audit/approval-stats?days=7`：

```json
{
  "window_days": 7,
  "counts": {"requested": 12, "approved": 8, "denied": 2, "expired": 2, "blocked": 1, "readonly_auto": 150},
  "by_environment": {"test": 160, "prod": 12},
  "top_commands": [{"command": "systemctl restart nginx", "count": 4}],
  "top_reasons": [{"reason": "unknown command, default approval (foo)", "count": 6}],
  "median_approval_seconds": 12.3,
  "intent_drift": 1
}
```

- 基于现有 `ts/run_id/action/command_hash/command_raw/conclusion/environment/result` 聚合；新增 `approval_stats(days)` 只读函数于 `audit.py`，路由只负责参数校验与序列化。
- `counts.requested` = `conclusion=requested`；`approved` = `permission_grant:*` 的 `approved`（排除手动 allowlist 登记的 `permission_allow_manual:*`）；`denied` = `permission_deny_command:*`；`expired` **不是新结论**，由 `denied` 且 `result` 前缀为 `approval_expired` 的记录派生；`blocked` = 策略拦截/拒绝（hardline、prod 预登记缺失、权限门缺失），与用户拒绝分开。
- `readonly_auto` = `executed` 或 `failed` 且 detail `source=readonly_rule` 的记录；`legacy_safe_verb` 不计入，避免把旧白名单算成新收益。
- 审批等待时长：按 `run_id + approval_key` 配对 `requested` 与 `approved`/`denied` 的时间差，取中位数；`released` 是 run 清理，不作为审批响应时间；无配对时返回 `null`。
- `intent_drift` = detail 中 `intent_drift=1` 的记录数（含命令级与工具级）。
- 时间窗口参数限制在 1–90 天，默认 7 天；无记录时返回零值而非 500。

### 8.4 审计页

- `AuditView` 增加“审批概览”卡片与 `requested` 筛选（下拉必须同步第三处清单，见 §8.2）。
- 展示 Top 命令/原因、只读放行覆盖率、意图漂移次数、等待中位数。
- 每周抽样复核只读自动放行与 `intent_drift` 记录；不自动调参，规则变更仍走代码 + 语料测试。

## 9. 测试计划

### 9.1 新增测试

- `test_command_parser.py`：
  - 引号内 `|`/`;`/`&&` 不切分；
  - `$()`/反引号内分隔符不切分；括号深度正确；
  - `rm$'\x2f' -rf /`、Windows 路径、ANSI 转义仍按现有规则命中；
  - 未闭合引号 → dangerous；路径判定仍走归一化副本。
- `test_readonly_policy.py`：
  - 120+ 只读黄金语料全部 `safe`，新增规则每条断言 `rule_id`；
  - 危险对照组零降级：删根、mkfs、裸设备、凭据外传、`find -delete`、`date -s`、`tar -x`；
  - 执行注入对照组：`git -c core.sshCommand=... ls-remote`、`tar -tf x --to-command=...`、`openssl x509 -engine ...`、`awk 'BEGIN{system(...)}'` 全部 dangerous；
  - 易混淆项：`getent passwd` safe / `getent shadow` dangerous；`ssh -V` safe / `ssh host` dangerous；`crontab -l` safe / `crontab -e` dangerous；`iptables -L` safe / `iptables -nL` safe / `iptables -F` dangerous；`docker compose ps` safe / `docker compose up` dangerous；`docker stats`（无 `--no-stream`）dangerous；`ethtool eth0` safe / `ethtool -r eth0` dangerous。
- `test_intention_corpus.py`：语料断言；`to_dict()` 包含 `readonly_expected`，四个构造路径一致。
- `test_intent_security_decoupling.py`：意图上下文不影响安全等级。
- `test_audit_approval_stats.py`：`requested` 记录、`approval_key` 配对、`expired` 派生、只读规则 ID、`/audit/append` 拒绝 `requested`。
- `test_safety_shell_exec_confirm.py`（或并入现有测试）：`_compute_requires_confirm` 对新只读命令返回 `False`，对 `date -s` 等返回 `True`。
- `test_safety_classify_endpoint.py` 补充：`/agent/safety/classify` 对新规则与生产环境判定一致。
- `test_plan_mode.py` 补充：新只读命令仍不在计划白名单时继续被拒，证明计划模式没有被顺带放宽。
- 前端测试：`ApprovalDock` 按 run 分组、危险项无“全部批准”、意图行与 `intent_drift` 展示。

### 9.2 回归

- 现有 `test_safety*`、`test_plan_mode.py`、`test_permission_*`、`test_approval_*`、`test_audit_*`、`test_command_explain.py` 全绿。
- `test_safety_misjudgment.py::test_known_over_block_is_registered_tradeoff` 保持：`grep 'rm -rf /'` 仍 hardline；`passwd root` 的 reason 仍为 `password change (passwd)`；`getent passwd` 转 safe。
- `test_safety_default_approval.py` 的“未知命令默认审批”保持。
- `test_ai_loop_round13c.py` 的枚举计数 10 → 11；`test_audit_append_endpoint.py` 增加 `requested` 被拒用例；`test_audit_conclusion_coercion.py` 不回归。
- `test_command_explain` 覆盖新增 destructive/exec reason 的中文后果文案。
- 手工验证：启动后端 + 前端，跑一轮只读任务（0 卡）和一轮写任务（聚合卡、逐条确认）；再跑一轮可见终端任务，确认只读命令不再弹终端确认；检查审计概览与 `requested` 筛选。

## 10. 实施阶段

### P1 — 解析器 + 只读证明（主收益）

1. 新建 `command_parser.py`，迁移解析函数并 re-export。
2. 新建 `readonly_policy.py`，实现规则表与 `ReadonlyVerdict`。
3. `safety.py` 增加 `CommandAssessment` / `classify_command_detailed`，接入引号感知分段与只读规则。
4. 实现 destructive write 守卫、执行注入守卫与 `getent passwd` 修复；同步 `command_explain` 静态后果表。
5. 黄金/危险/绕过/解析器/shell_exec 确认/批量 classify 测试；现有测试全绿。

### P2 — 意图层

1. `IntentionResult.readonly_expected`、`_READONLY_DISCIPLINE`、`to_dict`。
2. 意图语料与测试。
3. `intent_drift` 标记、卡片 payload、审计 `result`。
4. 安全解耦测试。

### P3 — 审批交互

1. `ApprovalDock` 按 run/主机分组。
2. `ApprovalCard` 意图行、`intent_drift` 警告、`always` 范围文案。
3. run 结束摘要与 `readonly_allowed` 事件展示。
4. 前端测试 + 浏览器手工验证。

### P4 — 审计

1. `requested` 结论与三处同源清单（含测试计数 10 → 11）。
2. `format/parse_audit_detail`；grant/deny/expired 记录追加 `approval_key`（`routes/agent.py` 与两个审批拦截器）。
3. `/audit/approval-stats` 与 `AuditView` 概览、`requested` 筛选。
4. `/audit/append` 收窄为 `executed`/`failed`，前端类型同步。
5. 审计测试与手工抽查。

### P5 — 暂缓事项（已登记）

- `wget` 默认落盘、`curl -o/-O`、`tee`、普通重定向到非敏感路径的写侧收紧。
- `rsync --dry-run`、`mount/umount` 只读形态、更多数据库/云厂商 CLI。
- 如后续需要，按同一规则表扩展并补语料，不在本轮扩大范围。

## 11. 验收指标

| 指标 | 目标 |
|---|---|
| 120+ 条无歧义只读语料 `safe` 命中 | 100%，新增规则每条带 `rule_id` |
| 84 条实测样本中的 29 条只读误判 | 全部转 safe（`/etc/passwd` 读取、`rsync` 保留） |
| 危险对照组（hardline/dangerous） | 零降级；执行注入对照组零放行 |
| 只读任务卡片数 | 下降 ≥80% |
| 自动放行审计可解释率 | 100% 带 `source`；`source=readonly_rule` 的 100% 带 `rule_id`（审计库健康时；失败由 `health()` 暴露） |
| 审批请求可追率 | 首次发卡 100% 有 `requested` 与 `approval_key`（审计库健康时） |
| 意图漂移记录 | 100% 标记，不改变授权 |
| 计划模式 | 白名单行为零变化 |
| 现有测试 | 全绿 |

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| 新只读规则误放 | 只读规则永远在危险规则之后；危险/绕过/执行注入语料；每周审计抽样 |
| 引号感知分段回归 | 保留整串剥引号扫描；解析失败回退 dangerous；解析器单测；路径判定不走 shlex |
| 新旧白名单漂移 | 新规则只补旧表未覆盖项；冲突取高严重度；黄金语料同时锁定两表结论 |
| 卡片聚合降低单条关注度 | 危险项不提供批量批准；每条仍独立按钮；意图与后果说明保留 |
| `requested` 审计量增加 | 只记首次发卡；非阻塞；统计按天窗口；归档沿用现有轮转 |
| 新增写侧卡（date/find/tar -x/新执行注入向量） | 仅高危低频操作；在发版说明中标注；prod 仍走预登记 |
| 意图字段被误用于授权 | 安全解耦测试 + 代码评审检查 + 文档不变量 |
| 只优化 headless 路径 | 同步测试 `_compute_requires_confirm` 与 `/agent/safety/classify` |
| 计划模式与新规则预期不一致 | 明确为已知限制，本轮不改；需要时单独评审并补包含关系测试 |

## 13. 已知口径与开放问题

- `/etc/passwd` 读取仍按现有 `_SECRET_FILE_RE` 判 dangerous（刻意口径）；如要放开，单独评审。
- `wget`/`curl -o`/`tee`/普通重定向属于良性写，本轮不收紧；它们不会被标为“只读”，但也不新增审批卡。
- `rsync --dry-run` 暂不加入只读白名单：参数组合复杂，误放风险高于收益。
- **计划模式不继承新只读规则**：`jq`、`dig`、`docker compose ps` 等即使被判 `safe`，只要不在 `PLAN_READ_ONLY_VERBS` / `PLAN_VERB_SUBCOMMANDS` 里，计划模式仍会拒绝。原因是计划模式的“什么都没改”承诺比审批门更强；本轮不放开。若后续要放开，必须逐项加入计划白名单并补 `test_plan_mode` 的严格子集断言。
- **`sed` 的 `e` 命令与更复杂的 awk 程序**未做完整解析：本轮只拦 `awk` 的 `system(`/`| getline`，`sed '1e ...'` 仍是已知缺口。明显的 `rm -rf /`、`curl|sh` 仍由整串 hardline/dangerous 规则覆盖；完整解释器语义不在本轮范围。
- **Git / kubectl 的配置信任边界**：`git` 只读子命令（含新增 `git ls-remote`）仍会读取仓库/全局 config，恶意仓库可通过 `core.fsmonitor`、`core.sshCommand`、`credential.helper` 执行外部程序；`kubectl` 可能触发 kubeconfig 里的 exec 认证插件。这是既有白名单就存在的信任假设（`git status`/`kubectl get` 同样受影响），本轮不引入 config 隔离；对不可信仓库/集群应保持逐条审批，后续需要单独的“隔离配置执行”专项。
- **引号内的危险字面量**仍按整串口径命中：`grep 'rm -rf /'` 是已登记取舍；同理 `grep 'chmod 777'` 仍可能被判 dangerous。要消除这类误报，需要“只扫描代码、不扫描引号内容”的新检测副本，但会让 `grep 'rm -rf /'` 降级，必须单独评审。
- 只读规则表放代码而非用户可编辑配置：避免运维侧绕过测试直接放宽边界；如未来需要配置化，必须先有同样的语料与审计门。
- 是否将 `requested` 记录做成 write-ahead：本轮按“非阻塞执行记录”处理；若合规要求更强，再单独升级为 critical。
- 审批聚合只改变呈现，不改变串行工具调用的逐条阻塞：多步骤写任务仍会一次只弹一张卡、逐条确认；本轮不提供“按计划批量批准写命令”。

## 14. 文件清单

**新增**

- `backend/agent/harness/command_parser.py`
- `backend/agent/harness/readonly_policy.py`
- `backend/tests/test_command_parser.py`
- `backend/tests/test_readonly_policy.py`
- `backend/tests/test_intention_corpus.py`
- `backend/tests/test_intent_security_decoupling.py`
- `backend/tests/test_audit_approval_stats.py`
- `backend/tests/test_safety_shell_exec_confirm.py`（如不并入既有测试）
- `backend/tests/fixtures/intention_corpus.jsonl`

**修改**

- `backend/agent/harness/safety.py` — `CommandAssessment`、detailed 分类、只读接入、破坏性写/执行注入守卫
- `backend/agent/harness/command_explain.py` — 新增 reason 的中文后果文案
- `backend/agent/intention.py` — `readonly_expected`、只读纪律、`to_dict`、四个构造点
- `backend/agent/harness/reins/guards.py` — detailed 分类、规则 ID、`intent_drift`、`readonly_allowed`、`requested`、assessment 挂载
- `backend/agent/harness/reins/gate.py` — 工具级 `intent_drift`、`requested`、`approval_key`
- `backend/agent/harness/reins/core.py` — `permission_display_fields` 增加意图字段
- `backend/agent/audit.py` — `requested`、detail 格式化/解析、`approval_stats(days)`
- `backend/routes/audit.py` — `/audit/approval-stats`、`/audit/append` 收窄
- `backend/routes/agent.py` — grant/deny 记录追加 `approval_key` detail
- `src/components/agent/ApprovalDock.vue` — 按 run/主机分组
- `src/components/agent/ApprovalCard.vue` — 意图行、漂移警告、`always` 文案
- `src/components/agent/AgentRunPanel.vue` / `AgentFullTrace.vue` — run 摘要
- `src/stores/agent.ts`、`src/api/sse.ts`、`src/api/audit.ts` — 类型与字段（含 `AuditAppendInput` 收窄）
- `src/views/ssh/AuditView.vue` — 审批概览、`requested` 筛选（第三处结论清单）
- `backend/tests/test_ai_loop_round13c.py` — 枚举计数 10 → 11
- `backend/tests/test_audit_append_endpoint.py` — `requested` 拒绝用例
- `backend/tests/test_safety_misjudgment.py` — `getent passwd` / `passwd root` 回归
- `backend/tests/test_safety_classify_endpoint.py`、`backend/tests/test_plan_mode.py`、`backend/tests/test_command_explain.py` — 新规则回归

**无需修改**

- `backend/agent/harness/driver.py` — 已写入 `intention.to_dict()`，新字段自动透传
- `backend/agent/tools/builtin/shell_exec.py` — `_compute_requires_confirm` 直接复用 `classify_command`，只需补测试
- `backend/agent/harness/reins/plan_mode.py` — 本轮明确不继承新只读规则
- `src/utils/approval-scopes.ts` — dangerous 仍不可批量批准，口径不变
