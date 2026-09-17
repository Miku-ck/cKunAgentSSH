#!/usr/bin/env bash
# ckun-agent-verify.sh — P3 远端最小权限账户部署后验证
#
# 对应 docs/p3-remote-least-privilege.md 第 6 节「验证清单（部署后必跑）」，
# 6 项逐一断言并给出 PASS/FAIL 汇总。需要一条能登录受限账户的 SSH 通道
# （已配好的受限公钥）；脚本用 ssh -i key user@host '<cmd>' 下发检查命令。
#
# 用法：
#   bash ckun-agent-verify.sh --user ckunagent --host 10.0.0.5 --key ~/.ssh/ckun_ed25519 \
#        [--port 22] [--workspace /srv/ckun-agent]
#
# 注意：这些检查命令本身要走 agent-exec 白名单（受限账户的 ForceCommand），
# 因此验证脚本下发的都是"受限账户允许的命令"或"故意越权应被拒的命令"，
# 通过 ssh 的远程命令（非交互 exec 通道）执行。
set -uo pipefail

USER="ckunagent"; HOST=""; KEY=""; PORT=22; WORKSPACE="/srv/ckun-agent"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --key)  KEY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    -h|--help) grep '^# ' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done
[ -n "$HOST" ] || { echo "需 --host"; exit 2; }
[ -n "$KEY" ]  || { echo "需 --key（受限公钥对应的私钥）"; exit 2; }

ssh_cmd() {
  # 在受限账户上执行远程命令，返回退出码 + 输出
  ssh -i "$KEY" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
      "$USER@$HOST" "$@"
}

pass=0; fail=0; declare -a REPORT
check() { # check <描述> <期望:0非零/零> <实际退出码>
  local desc="$1" expect="$2" actual="$3"
  if [ "$expect" = "nonzero" ]; then
    if [ "$actual" -ne 0 ]; then REPORT+=("PASS  $desc"); pass=$((pass+1));
    else REPORT+=("FAIL  $desc（期望非 0，实际 0）"); fail=$((fail+1)); fi
  else
    if [ "$actual" -eq 0 ]; then REPORT+=("PASS  $desc"); pass=$((pass+1));
    else REPORT+=("FAIL  $desc（期望 0，实际 $actual）"); fail=$((fail+1)); fi
  fi
}

echo "== P3 远端最小权限账户 验证（$USER@$HOST:$PORT）=="

# ── 0. 基本可达 ──────────────────────────────────────────────
ssh_cmd "true" >/dev/null 2>&1; rc=$?
check "SSH 受限账户可达" zero $rc
[ $rc -eq 0 ] || { printf '%s\n' "${REPORT[@]}"; echo "无法连通，终止"; exit 1; }

# ── 1. 越权路径拒绝：读 /etc/shadow ─────────────────────────
# 受限账户 exec 通道 → agent-exec；base64 /etc/shadow 不在工作区 → 拒
ssh_cmd "base64 /etc/shadow" >/dev/null 2>&1; rc=$?
check "越权路径拒绝（base64 /etc/shadow）" nonzero $rc

ssh_cmd "cat $WORKSPACE/../../etc/passwd" >/dev/null 2>&1; rc=$?
check "路径越界拒绝（.. 逃逸）" nonzero $rc

# ── 2. 越权动词拒绝：cat（不在白名单动词集）─────────────────
ssh_cmd "cat $WORKSPACE/x" >/dev/null 2>&1; rc=$?
check "越权动词拒绝（cat）" nonzero $rc
ssh_cmd "id" >/dev/null 2>&1; rc=$?
check "任意命令拒绝（id）" nonzero $rc

# ── 3. 快照闭环：工作区内文件读/写（白名单动词，应成功）──────
# base64 读工作区内存在的文件（若不存在则 base64 报错非 0，仍属"白名单放行但文件缺失"，
# 这里用一个必然存在的探针：先 file_write 再 base64 读）
ssh_cmd "printf '%s' 'dGVzdA==' | base64 -d > $WORKSPACE/.verify_probe" >/dev/null 2>&1; rc=$?
check "快照闭环·工作区写入（base64 写）" zero $rc
ssh_cmd "base64 $WORKSPACE/.verify_probe" >/dev/null 2>&1; rc=$?
check "快照闭环·工作区读取（base64 读）" zero $rc
# 还原删除
ssh_cmd "rm -f $WORKSPACE/.verify_probe" >/dev/null 2>&1; rc=$?
check "快照闭环·删除还原（rm -f）" zero $rc

# ── 4. 无 sudo 提权 ─────────────────────────────────────────
out="$(ssh_cmd "id -nG" 2>/dev/null || true)"
# 受限账户应不在 sudo/docker/wheel/admin 任一特权组
if echo "$out" | tr ' ' '\n' | grep -qE '^(sudo|docker|wheel|admin)$'; then
  REPORT+=("FAIL  无特权组提权（发现特权组）"); fail=$((fail+1))
else
  REPORT+=("PASS  无特权组提权"); pass=$((pass+1))
fi
ssh_cmd "sudo -n true" >/dev/null 2>&1; rc=$?
# 无 sudoers 授权 → 非 0（被拒）
check "无 sudo 提权（sudo -n true 被拒）" nonzero $rc

# ── 5. 密钥 only（密码被拒）─────────────────────────────────
# 用密码方式尝试登录应失败（sshd Match 块 PasswordAuthentication no）
pw_rc=0
ssh -i "$KEY" -p "$PORT" -o BatchMode=no -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new \
    "$USER@$HOST" "true" >/dev/null 2>&1 || pw_rc=$?
check "密钥 only（密码登录被拒）" nonzero $pw_rc

# ── 6. 无 TTY / 端口转发 ────────────────────────────────────
ssh -i "$KEY" -p "$PORT" -o BatchMode=yes -tt -o StrictHostKeyChecking=accept-new \
    "$USER@$HOST" "true" >/dev/null 2>&1; rc=$?
check "无 TTY（-tt 分配终端被拒）" nonzero $rc
ssh -i "$KEY" -p "$PORT" -o BatchMode=yes -W localhost:22 -o StrictHostKeyChecking=accept-new \
    "$USER@$HOST" >/dev/null 2>&1; rc=$?
check "无端口转发（-W 被拒）" nonzero $rc

# ── 汇总 ────────────────────────────────────────────────────
echo
printf '%s\n' "${REPORT[@]}"
echo
echo "== 结果: $pass 通过 / $fail 失败 =="
[ $fail -eq 0 ] && { echo "全部通过 ✅"; exit 0; } || { echo "存在失败项，请排查"; exit 1; }
