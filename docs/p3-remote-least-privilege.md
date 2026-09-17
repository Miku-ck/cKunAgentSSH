# P3 实施计划：远端最小权限账户 + 命令白名单 + 预变更快照集成

> 状态：**已实现（2026-09-16）**。代码层（P0/P1/路径#4/P2）此前已闭环；本计划补足的**部署/运维侧**远端权限收敛已落地——
> 交付物见 `scripts/deploy/`（`agent-exec.py` 白名单执行器 + `setup-ckun-agent.sh` 一键部署 + `ckun-agent-verify.sh` 部署后验证），
> 并由跨端契约测试 `backend/tests/test_remote_whitelist_contract.py` 钉死客户端模板与远端白名单的一致性。
> 前置依赖：Electron Bridge 已 fail-closed（`run_headless` 缺会话/桥不可用时拒绝本机执行）、
> `FileContentUndo` 已在 `UndoBuilder.build` 中落地（执行前快照远端原内容，失败时内容级还原）。
> 本计划不改动上述代码，而是让远端账户**默认就什么都不该做超出白名单的事**。

---

## 1. 威胁模型与目标

fail-closed 解决的是"命令没发到远程反而落到后端主机本机"的故障态。
P3 解决的是**另一端**的问题：即便命令正确地发到了远程 SSH 会话，
远程账户本身权限过大（root / 同账号共用工件 / 可触达全文件系统），
则一次提示注入或工具误用即可在远端造成不可逆破坏。

目标：
1. 远端账户只拥有其工件目录的读写权，不拥有系统其余部分的写权。
2. 远端账户可被执行的命令收敛到 ops 工具实际发出的**固定动词集合**。
3. 预变更快照（`FileContentUndo`）在最小权限账户下依然可工作（读/写/删均在该账户权限内）。
4. 与 fail-closed 叠加：桥故障 → 命令不发（P0）；桥正常但账户越权 → 远端拒绝执行（本计划）。

---

## 2. 账户与目录布局

```bash
# 专用无登录账户（不共用运维人员账户）
useradd --system --home-dir /srv/ckun-agent --shell /usr/sbin/nologin ckunagent

# 工件目录：仅该账户可写，其余只读
install -d -o ckunagent -g ckunagent -m 750 /srv/ckun-agent
install -d -o ckunagent -g ckunagent -m 750 /srv/ckun-agent/jobs   # 仅放预注册作业脚本

# 禁止该账户自助提权：清空其补充组、无 sudo 基础权限
usermod -G "" ckunagent
```

- 该账户**不应**加入 `sudo` / `docker` / `wheel` 等组。
- 作业脚本（`/srv/ckun-agent/jobs/*.sh`）由部署流水线以 root 写入、属主 root、mode 755；
  账户只被允许"按名调用"已注册脚本，不能编辑或新建脚本。

---

## 3. SSH 接入：密钥 Only + 命令强制

### 3.1 `sshd_config` 片段（Match 块，放文件末尾）

```
Match User ckunagent
    PasswordAuthentication no
    PubkeyAuthentication yes
    PermitTTY no
    PermitX11Forwarding no
    AllowTcpForwarding no
    ForceCommand /usr/local/bin/agent-exec
```

`ForceCommand` 把所有进入该账户的会话命令替换为 `/usr/local/bin/agent-exec`，
原始命令通过环境变量 `SSH_ORIGINAL_COMMAND` 传给它——这正是白名单执行的入口。

### 3.2 `authorized_keys` 条目（限制来源 + 禁用端口转发）

```
restrict,from="10.0.0.0/8",command="/usr/local/bin/agent-exec" ssh-ed25519 AAAA... ckun@bridge
```

`restrict` 已隐含禁用 agent/TTY/X11/端口转发；`from=` 限定桥接出口地址；
`command=` 双保险（即便 sshd 未读到 Match 块也强制 wrapper）。

---

## 4. 命令白名单执行器 `/usr/local/bin/agent-exec`

设计取舍：sudoers 的按参匹配（`/bin/base64 /srv/ckun-agent/*`）**脆弱且易误配**，
本方案用 **ForceCommand + wrapper 脚本**做真正的动词级白名单，sudoers 仅作为
"该账户可无密执行 wrapper 本身"的兜底（实际 wrapper 由 ForceCommand 直接拉起，无需 sudo）。

```python
#!/usr/bin/env python3
# /usr/local/bin/agent-exec  — 远端命令白名单执行器（ForceCommand 入口）
import os, re, shlex, sys, subprocess, logging

ALLOWED = [
    # 快照原内容（FileContentUndo 执行前读）
    (re.compile(r"^base64 (/\S+)$"), "base64"),
    # 写/编辑/还原：base64 仅含字母数字 +/=
    (re.compile(r"^printf '%s' '[A-Za-z0-9+/=]+' \| base64 -d > (/\S+)$"), "write"),
    # 带行号读取片段
    (re.compile(r"^nl -ba (/\S+) \| sed -n '\d+,\d+p'$"), "read"),
    # glob 查找
    (re.compile(r"^find (/\S+) -name \S+ -type f$"), "glob"),
    # 正则搜索
    (re.compile(r"^grep -rnE --color=never \S+ (/\S+) \| head -n \d+$"), "grep"),
    # 还原新建文件（删除）
    (re.compile(r"^rm -f (/\S+)$"), "rm"),
    # 预注册作业脚本（按名调用，脚本内容不可被账户篡改）
    (re.compile(r"^job ([A-Za-z0-9_-]+)$"), "job"),
]
WORKSPACE = "/srv/ckun-agent"

def _in_workspace(path: str) -> bool:
    # 归一化后必须位于 WORKSPACE 之下，且不含越界段
    norm = os.path.normpath(path)
    return norm == WORKSPACE or norm.startswith(WORKSPACE + "/")

def main() -> int:
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not cmd:
        return 1
    for rx, verb in ALLOWED:
        m = rx.match(cmd)
        if not m:
            continue
        if verb in ("base64", "write", "read", "glob", "grep", "rm"):
            target = m.group(1)
            if not _in_workspace(target):
                logging.warning("agent-exec DENY out-of-workspace: %s", target)
                return 1
        if verb == "job":
            name = m.group(1)
            script = os.path.join(WORKSPACE, "jobs", f"{name}.sh")
            if not os.path.isfile(script):
                return 1
            # 仅执行预注册脚本，且不以账户身份改写脚本
            return subprocess.call(["/bin/sh", script])
        # 其余动词：原样执行（路径已校验在 workspace 内）
        return subprocess.call(shlex.split(cmd))
    logging.warning("agent-exec DENY unmatched: %s", cmd)
    return 1

if __name__ == "__main__":
    sys.exit(main())
```

要点：
- 所有文件类动词都再经 `_in_workspace` 二次校验，路径越界（含 `..`）一律拒绝——
  与 `ops_file._is_traversal` 形成**客户端 + 服务端双校验**。
- `job` 动词只接受白名单文件名、只执行已注册脚本，杜绝 `ops_job` 远端任意命令执行。
- wrapper 本身属主 root、mode 755、`ckunagent` 不可写，防篡改。

---

## 5. 与现有 harness 的集成

| 现有机制 | 在本方案下的行为 |
|---------|----------------|
| `run_headless`（P0 fail-closed） | 命令照常经桥下发；桥故障→不发（不变）。桥正常→远端 wrapper 校验动词/路径，越权→远端拒绝（新增纵深）。 |
| `FileContentUndo`（P3 预变更快照） | 执行前 `base64 <path>` 读快照、失败时 `printf '...' \| base64 -d >` 或 `rm -f` 还原——全部落在白名单动词内，且路径均在 `/srv/ckun-agent`，账户权限自洽，无需 sudo。 |
| `ShellUndo`（路径#4） | 仅对 `shell_exec` 的反向命令生效；若走该账户，反向命令同样过 wrapper 白名单。建议 `shell_exec` 的远端场景也收敛到白名单动词，避免反向命令被拒。 |
| `ops_job` 远端 | 改用 `job <name>` 动词调用预注册脚本，不再接受任意命令字符串。 |

---

## 6. 验证清单（部署后必跑）

1. **越权路径拒绝**：桥下发送 `base64 /etc/shadow`（或含 `..` 的路径）→ 远端 wrapper 返回非 0，桥回显失败，不泄露内容。
2. **越权动词拒绝**：发送 `cat /srv/ckun-agent/x` → 不在白名单 → 拒绝。
3. **快照闭环**：对一个workspace内文件做 `file_write` 后再 `file_edit` 改错，触发 auto_rollback → `FileContentUndo` 经 wrapper 还原原内容，校验内容一致。
4. **无 sudo 提权**：该账户 `sudo -l` 应无可用提权条目；`id` 不应在特权组。
5. **密钥 only**：`ssh ckunagent@host` 用密码应被拒；仅持 authorized_keys 中的公钥可连。
6. **无 TTY/转发**：`ssh -W` 端口转发、`ssh -t` 分配 TTY 均被拒。

---

## 7. 待确认决策（需用户拍板）

- **作业执行模型**：`ops_job` 远端是否全面改为 `job <name>` 预注册脚本模式？
  若保留任意命令，则白名单的"动词收敛"在 job 场景失效，需另设独立受限账户或远端容器。
- **白名单是否含 `systemctl`/`service`**：运维作业若需启停服务，需单独动词 + 精确 unit 白名单，
  不在本基础动词集内，建议作为 P3.1 单独设计。
- **多目标机一致性**：上述配置需经配置管理（Ansible/脚本）统一下发，避免逐机漂移。

## 8. 实现记录与决策落点（2026-09-16 落地）

第 7 节三个决策的落点（self-use 场景，已拍板）：

1. **作业执行模型 —— 保留任意命令，wrapper 递归封堵 base64 洞**
   不改成纯 `job <name>` 预注册（那会让 `job_run` 远端能力退化、丢失灵活性）。
   改为：`agent-exec` 对 `job_run` 启动命令**解码 run.sh 并对内部命令递归做安全分类**
   （移植后端 `safety.py` 的 HARDLINE/DANGEROUS 正则表）——
   `hardline` 恒拒；`dangerous` 默认拒绝（可用 `CKUN_AGENT_ALLOW_DANGEROUS_JOB=1` 放宽，self-use 信任场景）。
   普通运维命令（sleep/df/grep/systemctl status 等）照常放行。
   > 这是原始设计未覆盖的关键绕过：`echo <b64>|base64 -d>run.sh; setsid bash run.sh`
   > 表面是合法模板，内部命令可藏任意 payload。契约测试
   > `test_job_base64_hole_encoded_payload_rejected` 专门钉死。

2. **systemctl —— 纳入白名单，动词收敛**
   `systemctl (status|start|stop|restart) <name>` 放行（name 严格 `[A-Za-z0-9_.-]+`）；
   `reboot/poweroff/halt/kexec/suspend` 等电源动作不在动词集内，且被 HARDLINE
   `systemctl power action` 规则双重拦截。

3. **多机一致性 —— 一键脚本 + 环境变量**
   `setup-ckun-agent.sh` 单机幂等部署（账户/目录/wrapper/sshd/authorized_keys 五步，
   重复执行安全）；wrapper 全部边界经 `/etc/ckun-agent.env`（WORKSPACE / JOBS_ROOT /
   ALLOW_DANGEROUS_JOB）可覆盖，多机统一下发时可由 Ansible 按机渲染该 env 文件，
   脚本本体零改动。

### 实现要点（与原设计稿的差异）

- **模板匹配方式**：原稿第 4 节用"逐动词正则 + shlex 执行"。落地改为**对整条命令串做
  带锚点正则的逐模板匹配**（20 个模板），默认拒绝。原因：客户端命令是固定模板串，
  整体精确匹配比逐动词拆解更稳（不受 shell 分词细节如 `;` 粘连影响），且天然
  阻止"合法模板 + 追加注入"（锚点 `$`）。
- **契约测试（防两端漂移的核心）**：客户端命令模板已全部收敛为单一事实来源函数
  （`ops_common.base64_read_cmd/base64_write_cmd/base64_restore_cmd/file_delete_cmd`、
  `ops_file.file_read_cmd/file_glob_cmd/file_grep_cmd`、`ops_image.image_read_cmd`、
  `ops_obs.*_cmd`、`ops_service.service_cmd`、`ops_job.job_*_cmd`），
  `test_remote_whitelist_contract.py` 加载 `agent-exec.py` 后用真实客户端输出断言
  "全部放行 + 18 类对抗命令全部拒绝"（34 用例）。**改任一端命令格式必须跑该测试。**
- **SFTP 兼容**：Agent 命令与 SFTP 面板共用受限连接（sshd `ForceCommand` 会接管
  子系统请求，命令置为 `sftp`），白名单含 `sftp` 模板（仅裸命令/安全 flag）。
- **pid 边界**：`job_kill`/`job_alive` 仅接受正整数 PID（`$!` 恒 ≥1），
  `kill -- -0`（杀当前进程组）被拒。
- **shell_exec 反向命令（ShellUndo）**：`_shell_reverse` 仅派生
  `mkdir/rmdir/touch/mv/cp` 的精确反向（无元字符），落在白名单外时
  apply_reverse 会失败但被捕获记 warning（best-effort 回滚，不阻断主流程）。
  受限账户下 `rmdir`/`rm` 等反向命令若不在工作区内会被 wrapper 拒绝——
  这是预期行为（受限账户本就不应越区操作）。
- **验证**：`ckun-agent-verify.sh` 实现第 6 节全部 6 项检查（越权路径/越权动词/
  快照闭环/无提权/密钥 only/无 TTY 与转发），部署后跑一遍即可。

### 使用

```bash
# 目标机（root）：
scp agent-exec.py target:/tmp/ && ssh target \
  "bash /tmp/setup-ckun-agent.sh --from 10.0.0.0/8 --publickey ~/.ssh/ckun.pub && systemctl reload sshd"

# 验证：
bash ckun-agent-verify.sh --user ckunagent --host <ip> --key ~/.ssh/ckun_ed25519
```

客户端侧无需任何配置改动：Agent 仍连同一个 SSH 连接；若希望 Agent 专用受限账户，
在连接管理里为该目标加一条 `ckunagent` 连接并在 AI 面板选它作为活动会话即可。
