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
  re?: RegExp
  /**
   * Extra predicate. When a rule carries both, they are ANDed: `re` is the
   * cheap pre-filter and `test` the compound logic. (Previously `test` silently
   * replaced `re`, so e.g. rm-recursive-root's /\brm\s+/ was decorative.)
   */
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
  // Anchored to the command position so path names like /etc/passwd never match,
  // but the anchor must tolerate what actually precedes a command in practice:
  // `FOO=1 useradd x`, `sudo -u root passwd y`, `env PATH=/x useradd z`. Without
  // those, the whole prefix-skipping family classified as READONLY and ran with no
  // approval card on a prod host — an approval gate that only covers the tidy form.
  // `(?:\S+\s+)*` is greedy but backtracks, so `sudo useradd x` still matches the
  // verb right after the wrapper (a fixed `\s+` after the flag list broke exactly that).
  {
    id: 'user-mgmt',
    zh: '修改账户 / 口令 / 授权，可能影响登录与提权',
    re: /^\s*(?:[\w.]+=(?:'[^']*'|"[^"]*"|\S+)\s+)*(?:(?:sudo|doas|pkexec|env|nohup|time|command)\s+(?:\S+\s+)*)?(?:userdel|useradd|usermod|groupadd|groupdel|groupmod|gpasswd|passwd|chpasswd|visudo|chsh|chfn|newusers)\b/,
  },
  { id: 'etc-write', zh: '覆盖写入 /etc 下的系统配置', re: />>?\s*\/etc\// },
  // no \b before -F: a hyphen is a non-word char, so \b never holds there
  { id: 'firewall-flush', zh: '清空/关闭防火墙规则，暴露所有端口', re: /\b(iptables|nft|firewall-cmd|ufw)\b[\s\S]*?(?:\s-F\b|\s+flush\b|\s+disable\b|--permanent)/ },
  { id: 'kill-pid1', zh: '终止 1 号进程，导致系统/容器崩溃', re: /\bkill\s+(-\d+\s+|-[a-z]+\s+)?1\b/ },
  { id: 'history-clear', zh: '清空 shell 历史，破坏审计痕迹', re: /\bhistory\s+-c\b/ },
  { id: 'crontab-rm', zh: '删除全部定时任务', re: /\bcrontab\s+-r\b/ },
]

const MUTATING_RULES: Rule[] = [
  // >& and /dev/* targets are not file writes (2>&1, >/dev/null must stay read-only);
  // a lookahead after \s* backtracks to zero width, so this uses a test fn instead
  { id: 'file-write', zh: '写入/覆盖文件', test: hasFileRedirect },
  { id: 'file-mut', zh: '移动/复制/新建/改属主权限文件', re: /\b(mv|cp|mkdir|touch|ln|chmod|chown|install|truncate|unlink)\b/ },
  // plain (non-recursive) deletion is mutating; recursive deletion of critical
  // paths is caught earlier by the dangerous rm-recursive-root rule
  { id: 'file-delete', zh: '删除文件/目录', re: /\b(rm|rmdir)\b/ },
  { id: 'sed-i', zh: '原地修改文件内容（sed -i）', re: /\bsed\s+(-\S+\s+)*-i/ },
  { id: 'pkg-manager', zh: '安装/卸载/升级软件包，变更系统', re: /\b(apt(-get)?|yum|dnf|pacman|zypper|apk|gem|cargo|pip3?|npm|yarn|pnpm)\b[\s\S]*?\b(install|remove|uninstall|upgrade|purge|add|update|publish|link)\b/ },
  { id: 'service-start', zh: '启动/启用系统服务', re: /\b(systemctl|service)\s+[\s\S]*?\b(start|enable|reload|restart)\b/ },
  { id: 'git-write', zh: '推送/硬重置/清理 git 仓库', re: /\bgit\s+[\s\S]*?\b(push|reset\s+--hard|clean\s+(-\S+\s+)*-f|checkout\s+-f|reset\s+--keep)\b/ },
  // `docker stop x` is a single word after docker — a two-word pattern never matched it
  { id: 'docker-write', zh: '变更容器/镜像/卷（docker）', test: dockerMutates },
  { id: 'k8s-write', zh: '变更 Kubernetes 资源（kubectl）', re: /\bkubectl\s+[\s\S]*?\b(delete|apply|replace|patch|scale|drain|cordon|uncordon|rollout\s+(resume|retry)|label\s+(-\S+\s+)*-[a-z]+\s+\S+=\S*--overwrite)\b/ },
  { id: 'process-kill', zh: '终止进程', re: /\b(kill|pkill|killall|tkill)\b/ },
  // 解释器的内联程序（sh -c / python -c / eval）：真正的动作在参数里，分类器读不到，
  // 于是把它当只读就等于让载荷绕过审批卡。归到 mutating：非 prod 出卡，prod 默认拒。
  { id: 'opaque-interpreter', zh: '由 shell/解释器内联执行，实际动作不可见', test: opaqueWrapper },
  { id: 'db-write', zh: '对数据库执行写操作', re: /\b(mysql|psql|redis-cli|mongosh)\b[\s\S]*?\b(UPDATE|DELETE|DROP|TRUNCATE|INSERT|ALTER|CREATE|GRANT|REVOKE|FLUSH|SET\s+GLOBAL)\b/i },
  { id: 'crontab-set', zh: '修改定时任务', re: /\bcrontab\b(?!.*-r\b)/ },
]

/**
 * Piping into a shell/interpreter hands the pipe's content to a second parser, so
 * the dangerous verb is invisible to per-segment matching:
 * `printf 'useradd evil\n' | sh` split into `printf …` + `sh`, both READONLY, and
 * the command auto-ran on a prod host with no card. curl/wget was covered; this is
 * the same hole with any other producer.
 */
const PIPE_TO_SHELL = /\|\s*(?:sudo|doas)\s+(?:-\S+\s+)*(?:[\w./-]*\/)?(?:ba|z|k|da)?sh\b|\|\s*(?:[\w./-]*\/)?(?:ba|z|k|da)?sh\b|\|\s*(?:[\w./-]*\/)?python\d*\s+|\|\s*(?:[\w./-]*\/)?(?:perl|ruby|node|php)\b/

/**
 * An interpreter invoked with an inline program (`sh -c "…"` / `python -c "…"`) or
 * `eval` carries its real effect inside an argument the classifier does not read.
 * Classifying it READONLY because the visible wrapper looks innocent means the
 * payload never gets an approval card, so the honest answer is "opaque" → mutating.
 * First token only, after the usual wrappers/assignments are skipped.
 */
const INTERPRETERS = /^(?:ba|z|k|da)?sh$|^(?:python\d*|perl|ruby|node|php|expect|irb)$|^env$/
const OPAQUE_FLAGS = /^(-[lc]$|--eval$|-e$|-r$|--command$)/

function opaqueWrapper(cmd: string): boolean {
  const tokens = cmd.trim().split(/\s+/)
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i]
    if (/^[\w.]+=/.test(t)) continue // FOO=1 bash -c '…'
    if (/^(sudo|doas|nohup|time|command|exec)$/.test(t)) continue
    if (/^-\S+$/.test(t)) continue // sudo -u root bash -c '…'
    if (t === 'eval' || t === 'source' || t === '.') return true
    const base = t.split('/').pop() ?? t
    if (!INTERPRETERS.test(base)) return false
    // env 自己不是解释器：跳过它继续看下一个真实 token（env bash -c …）
    if (base === 'env') continue
    return tokens.slice(i + 1).some(x => OPAQUE_FLAGS.test(x))
  }
  return false
}

/** A redirect whose target is a real file (not /dev/* and not a file descriptor). */
function hasFileRedirect(cmd: string): boolean {
  if (/\btee\b/.test(cmd)) return true
  for (const hit of cmd.matchAll(/>>?\s*\S+/g)) {
    const raw = hit[0]
    // skip comparison/arrow operators: >=, <=, =>, ->
    if (raw.length >= 2 && ['>=', '<=', '=>', '->'].includes(raw.slice(0, 2))) continue
    const target = raw.replace(/^>>?\s*/, '')
    if (target.startsWith('/dev/')) continue
    if (target.startsWith('&')) continue // fd redirects: >&, >&2, 2>&1
    if (/^\d+$/.test(target)) continue
    return true
  }
  return false
}

const DOCKER_MUTATE_VERBS = new Set(['rm', 'stop', 'start', 'kill', 'restart', 'run', 'create', 'prune', 'tag', 'load', 'save', 'build', 'push'])
const COMPOSE_MUTATE_VERBS = new Set(['up', 'down', 'create', 'delete', 'restart', 'stop', 'rm', 'build', 'push', 'scale'])

/** Inspects the first `docker <w1> <w2>` in the segment (pipes/&& split into separate segments). */
function dockerMutates(cmd: string): boolean {
  const m = cmd.match(/\bdocker\s+(\S+)(?:\s+(\S+))?/)
  if (!m) return false
  const w1 = m[1]
  const w2 = m[2]
  if (w1 === 'compose' || w1 === 'swarm') return w2 !== undefined && COMPOSE_MUTATE_VERBS.has(w2)
  return DOCKER_MUTATE_VERBS.has(w1) || (w2 !== undefined && DOCKER_MUTATE_VERBS.has(w2))
}

const SEGMENT_SPLIT = /\s*(?:&&|\|\||;|\||\$\(|`)/

/** Classify one whole (possibly compound) shell command string. */
export function classifyCommand(command: string): CommandVerdict {
  // Pipe-to-shell crosses segment boundaries, so check the whole string first.
  if (PIPE_TO_SHELL.test(command)) return { cls: 'mutating', rule: 'pipe-to-shell', zh: '把管道内容交给 shell/解释器执行' }
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
  // 两条谓词同时存在时是「与」：re 是便宜的预筛，test 是复合判定。
  // 原先 test 存在就完全不看 re，于是 rm-recursive-root 那条 /\brm\s+/ 只是装饰。
  const matches = (rule: Rule) => (rule.re ? rule.re.test(cmd) : true) && (!rule.test || rule.test(cmd))
  for (const rule of DANGEROUS_RULES) {
    if (matches(rule)) return { cls: 'dangerous', rule: rule.id, zh: rule.zh }
  }
  for (const rule of MUTATING_RULES) {
    if (matches(rule)) return { cls: 'mutating', rule: rule.id, zh: rule.zh }
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

export function execPolicy(
  input: ExecPolicyInput,
): { decision: 'allow' } | { decision: 'deny'; reason: string } | { decision: 'ask'; reason: string; zh: string } {
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
