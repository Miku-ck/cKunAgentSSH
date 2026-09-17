#!/usr/bin/env bash
# setup-ckun-agent.sh — 在目标 Linux 服务器上部署 cKunAgentSSH P3 远端最小权限账户
#
# 作用（对应 docs/p3-remote-least-privilege.md）：
#   1. 创建专用无登录系统账户 ckunagent（无 sudo/特权组，nologin）
#   2. 创建工件目录 /srv/ckun-agent（含 jobs/）+ 远程任务根 /tmp/ckun_agent_jobs
#   3. 安装白名单执行器 agent-exec.py 到 /usr/local/bin/agent-exec（root 属主，锁权）
#   4. 写 sshd_config Match 块（密钥 Only + ForceCommand + 禁 TTY/转发）
#   5. 可选：把本机公钥写入 authorized_keys（带 restrict,from=,command= 约束）
#
# 用法（以 root 在目标机执行）：
#   bash setup-ckun-agent.sh [选项]
#     --user NAME            账户名（默认 ckunagent）
#     --workspace DIR        工件目录（默认 /srv/ckun-agent）
#     --jobs-root DIR        远程任务根（默认 /tmp/ckun_agent_jobs）
#     --from CIDR            authorized_keys 的来源地址约束（可选，强烈建议填）
#     --publickey FILE       要追加的公钥文件（可选；不填则只改账户/sshd，不动 authorized_keys）
#     --agent-exec SRC       agent-exec.py 来源路径（默认本脚本同目录 agent-exec.py）
#     --allow-dangerous-job  允许 job 内部命令为 dangerous（self-use 信任场景，默认关闭）
#     --skip-sshd            不写 sshd_config（仅建账户/目录/wrapper，手动配 sshd）
#
# 幂等：重复执行安全（账户存在则复用，sshd 块存在则跳过，公钥存在则不重复追加）。
set -euo pipefail

# ── 默认参数 ────────────────────────────────────────────────
AGENT_USER="ckunagent"
WORKSPACE="/srv/ckun-agent"
JOBS_ROOT="/tmp/ckun_agent_jobs"
FROM_CIDR=""
PUBKEY_FILE=""
AGENT_EXEC_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agent-exec.py"
ALLOW_DANGEROUS_JOB="0"
SKIP_SSHD=0

log()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 参数解析 ────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --user)              AGENT_USER="$2"; shift 2 ;;
    --workspace)         WORKSPACE="$2"; shift 2 ;;
    --jobs-root)         JOBS_ROOT="$2"; shift 2 ;;
    --from)              FROM_CIDR="$2"; shift 2 ;;
    --publickey)         PUBKEY_FILE="$2"; shift 2 ;;
    --agent-exec)        AGENT_EXEC_SRC="$2"; shift 2 ;;
    --allow-dangerous-job) ALLOW_DANGEROUS_JOB="1"; shift ;;
    --skip-sshd)         SKIP_SSHD=1; shift ;;
    -h|--help)           grep '^# ' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知参数: $1（用 --help 查看用法）" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "本脚本需以 root 运行（创建系统账户/改 sshd）"
[ -f "$AGENT_EXEC_SRC" ] || die "找不到 agent-exec 来源: $AGENT_EXEC_SRC"
command -v python3 >/dev/null || die "目标机缺少 python3（agent-exec 运行依赖）"

# ── 1. 系统账户（无登录、无特权组）──────────────────────────
if id "$AGENT_USER" >/dev/null 2>&1; then
  log "账户 $AGENT_USER 已存在，复用"
  # 确保是 nologin + 清空补充组（若被误改过）
  usermod -s /usr/sbin/nologin "$AGENT_USER" 2>/dev/null || true
  usermod -G "" "$AGENT_USER" 2>/dev/null || true
else
  log "创建系统账户 $AGENT_USER（nologin，无补充组）"
  useradd --system --home-dir "$WORKSPACE" --shell /usr/sbin/nologin "$AGENT_USER"
fi

# 确保不在 sudo/docker/wheel 等特权组（纵深：即使误加也移除）
for g in sudo docker wheel admin; do
  if getent group "$g" >/dev/null 2>&1 && id -nG "$AGENT_USER" | tr ' ' '\n' | grep -qx "$g"; then
    warn "移除特权组 $g"
    gpasswd -d "$AGENT_USER" "$g" >/dev/null || true
  fi
done

# ── 2. 工件目录 + 远程任务根 ────────────────────────────────
log "创建工件目录 $WORKSPACE（含 jobs/）"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 750 "$WORKSPACE"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 750 "$WORKSPACE/jobs"

# 远程任务根：账户需能读写（job 脚本/日志/rc 落点）
log "创建远程任务根 $JOBS_ROOT"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 750 "$JOBS_ROOT"

# ── 3. 安装白名单执行器（root 属主锁权，防账户篡改）─────────
WRAPPER="/usr/local/bin/agent-exec"
log "安装白名单执行器 → $WRAPPER"
install -m 755 -o root -g root "$AGENT_EXEC_SRC" "$WRAPPER"
chmod u+s "$WRAPPER" 2>/dev/null || true  # setuid 可选，非必需
# 注入运行环境（工作区/任务根/dangerous 开关）到 wrapper 顶部
# 用环境变量文件方式：wrapper 读取 /etc/ckun-agent.env（避免改脚本本体）
cat > /etc/ckun-agent.env <<EOF
CKUN_AGENT_WORKSPACE=$WORKSPACE
CKUN_AGENT_JOBS_ROOT=$JOBS_ROOT
CKUN_AGENT_ALLOW_DANGEROUS_JOB=$ALLOW_DANGEROUS_JOB
EOF
chmod 644 /etc/ckun-agent.env

# 用一个薄 shell 包装加载 env 再 exec python3（ForceCommand 指向它）
FORCECMD="/usr/local/bin/agent-exec-force"
log "安装 ForceCommand 包装 → $FORCECMD（加载 /etc/ckun-agent.env）"
TMP_FORCE="$(mktemp)"
cat > "$TMP_FORCE" <<'EOF'
#!/usr/bin/env bash
[ -f /etc/ckun-agent.env ] && . /etc/ckun-agent.env
exec python3 /usr/local/bin/agent-exec
EOF
install -m 755 -o root -g root "$TMP_FORCE" "$FORCECMD"
rm -f "$TMP_FORCE"

# ── 4. sshd_config Match 块 ─────────────────────────────────
if [ "$SKIP_SSHD" -eq 0 ]; then
  SSHD_CONF="/etc/ssh/sshd_config"
  MARK_BEGIN="# BEGIN ckun-agent"
  MARK_END="# END ckun-agent"
  if [ -f "$SSHD_CONF" ] && grep -q "$MARK_BEGIN" "$SSHD_CONF"; then
    log "sshd_config 已含 ckun-agent 块，跳过（如需更新请手动改）"
  else
    log "写入 sshd_config Match 块（$SSHD_CONF）"
    cp -a "$SSHD_CONF" "${SSHD_CONF}.ckun-agent.bak.$(date +%s)" 2>/dev/null || true
    {
      printf '\n%s\n' "$MARK_BEGIN"
      printf 'Match User %s\n' "$AGENT_USER"
      printf '    PasswordAuthentication no\n'
      printf '    PubkeyAuthentication yes\n'
      printf '    PermitTTY no\n'
      printf '    PermitX11Forwarding no\n'
      printf '    AllowTcpForwarding no\n'
      printf '    ForceCommand %s\n' "$FORCECMD"
      printf '%s\n' "$MARK_END"
    } >> "$SSHD_CONF"
    warn "已写 sshd_config，需重载: systemctl reload sshd（或 ssh）"
  fi
fi

# ── 5. authorized_keys（可选）───────────────────────────────
if [ -n "$PUBKEY_FILE" ]; then
  [ -f "$PUBKEY_FILE" ] || die "公钥文件不存在: $PUBKEY_FILE"
  HOME_DIR="$(getent passwd "$AGENT_USER" | cut -d: -f6)"
  [ -n "$HOME_DIR" ] || die "无法解析账户家目录"
  install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "$HOME_DIR"
  install -o "$AGENT_USER" -g "$AGENT_USER" -m 600 /dev/null "$HOME_DIR/.ssh" 2>/dev/null || true
  install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "$HOME_DIR/.ssh"
  AK="$HOME_DIR/.ssh/authorized_keys"
  [ -f "$AK" ] || install -o "$AGENT_USER" -g "$AGENT_USER" -m 600 /dev/null "$AK"

  PUBKEY="$(head -n1 "$PUBKEY_FILE" | awk -v h="$(hostname -s 2>/dev/null || echo remote)" '{print $1" "$2" ckun@"h}')"
  OPTS="restrict"
  [ -n "$FROM_CIDR" ] && OPTS="$OPTS,from=\"$FROM_CIDR\""
  ENTRY="$OPTS,command=\"$FORCECMD\" $PUBKEY"
  if grep -qF "command=\"$FORCECMD\"" "$AK"; then
    log "authorized_keys 已含 ckun-agent 条目，跳过"
  else
    printf '%s\n' "$ENTRY" >> "$AK"
    chown "$AGENT_USER:$AGENT_USER" "$AK"; chmod 600 "$AK"
    log "已追加受限公钥（$OPTS）到 $AK"
  fi
else
  warn "未指定 --publickey，跳过 authorized_keys（请自行把公钥加入 $AGENT_USER 的 authorized_keys，建议带 restrict,from=,command= 约束）"
fi

log "完成。下一步：systemctl reload sshd，然后用受限公钥 SSH 登录 $AGENT_USER 测试（TTY/转发应被拒）。"
log "运行验证脚本: bash ckun-agent-verify.sh --user $AGENT_USER"
