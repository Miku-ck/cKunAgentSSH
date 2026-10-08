/**
 * Command classification for the ssh_exec approval gate.
 *
 * Three classes, fail-closed:
 *  - readonly  -> runs without approval
 *  - mutating  -> user approval required (prod hosts denied unless configured)
 *  - dangerous -> user approval with a strong warning (prod hosts always denied)
 *
 * Compound commands (`&&`, `||`, `;`, `|`, subshells) are classified per
 * segment; the STRONGEST segment wins.
 */

export type CommandClass = 'readonly' | 'mutating' | 'dangerous'

export interface CommandVerdict {
  cls: CommandClass
  /** Which rule fired (audit/UI). */
  rule?: string
  /** One-line Chinese explanation of the likely effect (approval card text). */
  zh?: string
}

interface Rule {
  id: string
  zh: string
  re: RegExp
  /** Optional extra predicate for compound logic. */
  test?: (cmd: string) => boolean
}

/** rm -rf/-fr/-r targeting something outside /tmp (root, home, globs, /etc, /var, ...). */
function rmRootDanger(cmd: string): boolean {
  const m = cmd.match(/\brm\s+((?:-\S+\s+)*)\S+/g)
  if (!m) return false
  for (const call of m) {
    const flags = (call.match(/-\S+/) ?? [''])[0]
    const recursive = /r|R/.test(flags)
    if (!recursive) continue
    // collect non-flag arguments
    const args = call.split(/\s+/).filter((t) => !t.startsWith('-'))
    for (const a of args) {
      if (/^\/(var|etc|usr|opt|srv|home|root|boot|lib|bin|sbin|sys)\b/.test(a) || a === '/' || a === '~' || a === '$HOME' || a === '*' || a.startsWith('~/')) return true
    }
  }
  return false
}

const DANGEROUS_RULES: Rule[] = [
  { id: 'rm-recursive-root', zh: '递归强制删除系统/家目录等关键路径，数据不可恢复', re: /\brm\s+/, test: rmRootDanger },
  { id: 'filesystem', zh: '格式化磁盘 / 修改分区，目标数据将全部丢失', re: /\b(mkfs(\.\w+)?|wipefs|fdisk|sgdisk|parted)\b/ },
  { id: 'dd-blockdev', zh: '直接写块设备（dd of=/dev/...），可摧毁磁盘', re: /\bdd\b[\s\S]*?\bof=\/dev\// },
  { id: 'power', zh: '关机 / 重启 / 停机，远程服务立即中断', re: /\b(shutdown|reboot|halt|poweroff|init\s+[06])\b/ },
  { id: 'service-stop', zh: '停止/重启/禁用系统服务，相关服务中断', re: /\b(systemctl|service)\s+[\s\S]*?\b(stop|restart|disable|mask)\b/ },
  // anchored to the command position so path names like /etc/passwd never match
  { id: 'user-mgmt', zh: '修改账户 / 口令 / 授权，可能影响登录与提权', re: /^\s*(?:sudo\s+(?:-\S+\s+)*|doas\s+|pkexec\s+(?:-\S+\s+)*)?(?:userdel|useradd|usermod|groupdel|passwd|chpasswd|visudo|chsh)\b/ },
  { id: 'etc-write', zh: '覆盖写入 /etc 下的系统配置', re: />>?\s*\/etc\// },
  { id: 'firewall-flush', zh: '清空/关闭防火墙规则，暴露所有端口', re: /\b(iptables|nft|firewall-cmd|ufw)\b[\s\S]*?\b(-F|flush|disable|--permanent)\b/ },
  { id: 'kill-pid1', zh: '终止 1 号进程，导致系统/容器崩溃', re: /\bkill\s+(-\d+\s+|-[a-z]+\s+)?1\b/ },
  { id: 'history-clear', zh: '清空 shell 历史，破坏审计痕迹', re: /\bhistory\s+-c\b/ },
  { id: 'crontab-rm', zh: '删除全部定时任务', re: /\bcrontab\s+-r\b/ },
]

const MUTATING_RULES: Rule[] = [
  // >& and /dev/* targets are not file writes (2>&1, >/dev/null must stay read-only)
  { id: 'file-write', zh: '写入/覆盖文件', re: /\btee\b|>>?\s*(?!\/dev\/|&)/ },
  { id: 'file-mut', zh: '移动/复制/新建/改属主权限文件', re: /\b(mv|cp|mkdir|touch|ln|chmod|chown|install|truncate|unlink)\b/ },
  { id: 'sed-i', zh: '原地修改文件内容（sed -i）', re: /\bsed\s+(-\S+\s+)*-i/ },
  { id: 'pkg-manager', zh: '安装/卸载/升级软件包，变更系统', re: /\b(apt(-get)?|yum|dnf|pacman|zypper|apk|gem|cargo|pip3?|npm|yarn|pnpm)\b[\s\S]*?\b(install|remove|uninstall|upgrade|purge|add|update|publish|link)\b/ },
  { id: 'service-start', zh: '启动/启用系统服务', re: /\b(systemctl|service)\s+[\s\S]*?\b(start|enable|reload|restart)\b/ },
  { id: 'git-write', zh: '推送/硬重置/清理 git 仓库', re: /\bgit\s+[\s\S]*?\b(push|reset\s+--hard|clean\s+(-\S+\s+)*-f|checkout\s+-f|reset\s+--keep)\b/ },
  { id: 'docker-write', zh: '变更容器/镜像/卷（docker）', re: /\bdocker\s+\w+\s+[\s\S]*?\b(rm|stop|start|kill|restart|system\s+prune|volume\s+rm|network\s+rm|tag|load|save|build)\b/ },
  { id: 'k8s-write', zh: '变更 Kubernetes 资源（kubectl）', re: /\bkubectl\s+[\s\S]*?\b(delete|apply|replace|patch|scale|drain|cordon|uncordon|rollout\s+(resume|retry)|label\s+(-\S+\s+)*-[a-z]+\s+\S+=\S*--overwrite)\b/ },
  { id: 'process-kill', zh: '终止进程', re: /\b(kill|pkill|killall|tkill)\b/ },
  { id: 'db-write', zh: '对数据库执行写操作', re: /\b(mysql|psql|redis-cli|mongosh)\b[\s\S]*?\b(UPDATE|DELETE|DROP|TRUNCATE|INSERT|ALTER|CREATE|GRANT|REVOKE|FLUSH|SET\s+GLOBAL)\b/i },
  { id: 'crontab-set', zh: '修改定时任务', re: /\bcrontab\b(?!.*-r\b)/ },
  { id: 'package-upload', zh: '在远程安装/写入二进制或脚本', re: /\b(curl|wget)\b[\s\S]*?\|\s*(ba)?sh\b/ },
]

const SEGMENT_SPLIT = /\s*(?:&&|\|\||;|\||\$\(|`)/

/** Classify one whole (possibly compound) shell command string. */
export function classifyCommand(command: string): CommandVerdict {
  const segments = command.split(SEGMENT_SPLIT).filter((s) => s.trim() !== '')
  if (segments.length === 0) segments.push(command)
  let cls: CommandClass = 'readonly'
  let verdict: CommandVerdict = { cls }
  for (const segment of segments) {
    const v = classifySingle(segment)
    if (v.cls === 'dangerous') return v
    if (v.cls === 'mutating') {
      cls = 'mutating'
      verdict = v
    }
  }
  return verdict
}

function classifySingle(cmd: string): CommandVerdict {
  for (const rule of DANGEROUS_RULES) {
    const hit = rule.test ? rule.test(cmd) : rule.re.test(cmd)
    if (hit) return { cls: 'dangerous', rule: rule.id, zh: rule.zh }
  }
  for (const rule of MUTATING_RULES) {
    if (rule.re.test(cmd)) return { cls: 'mutating', rule: rule.id, zh: rule.zh }
  }
  return { cls: 'readonly' }
}

/**
 * The approval decision for one agent-originated ssh_exec call.
 * (GUI-originated execs bypass this gate: a human in the panel is the operator.)
 */
export interface ExecPolicyInput {
  command: string
  /** The host's environment tag ('prod' is security-significant). */
  environment?: string
  /** Plugin config: allow mutating ops on prod hosts (still approval-gated). */
  allowProdMutations: boolean
  hostAlias: string
}

export type ExecPolicy =
  | { decision: 'allow' }
  | { decision: 'deny'; reason: string }
  | { decision: 'ask'; reason: string; zh: string }

export function execPolicy(input: ExecPolicyInput): ExecPolicy {
  const v = classifyCommand(input.command)
  const isProd = input.environment === 'prod'
  if (v.cls === 'readonly') return { decision: 'allow' }
  if (v.cls === 'dangerous') {
    if (isProd) {
      return { decision: 'deny', reason: `生产主机 ${input.hostAlias} 禁止高危操作（${v.rule}）。请人工在 SSH 面板终端中操作。` }
    }
    return {
      decision: 'ask',
      reason: `dangerous remote command (${v.rule}) on ${input.hostAlias}: ${input.command}`,
      zh: `高危命令（${v.zh ?? v.rule}）：${input.command}`,
    }
  }
  // mutating
  if (isProd && !input.allowProdMutations) {
    return { decision: 'deny', reason: `生产主机 ${input.hostAlias} 禁止变更操作（规则 ${v.rule}）。只读命令可直接执行；变更请人工在 SSH 面板终端操作。` }
  }
  return {
    decision: 'ask',
    reason: `mutating remote command (${v.rule}) on ${input.hostAlias}: ${input.command}`,
    zh: `变更操作（${v.zh ?? v.rule}）：${input.command}`,
  }
}
