"""prod 可见终端交付的整串只读证明：新只读规则 + 旧白名单纯读动词。

`is_provably_readonly` 只回答「每段是否可证明不改状态」；调用方必须先用
`classify_command` 确认整条命令是 safe。旧白名单证明的 rule_id 为空 ——
新只读规则才带 rule_id，审计口径不混。
"""
import pytest

from agent.harness.readonly_policy import is_provably_readonly

PROVABLE = [
    # 用户截图里的真实命令
    "du -sh /home/admin-pl/.trae-cn-server 2>/dev/null",
    "du -sh /home/admin-pl/.trae-cn-server /home/admin-pl/.qoder-cn-server 2>/dev/null",
    'echo "---"',
    # 旧白名单纯读动词
    "grep -n error /var/log/syslog",
    "ls -la /tmp",
    "df -h",
    "cat /etc/hostname",
    "ps aux",
    "ss -lntp",
    "systemctl status nginx",
    "service nginx status",
    "ip addr show",
    "git status",
    "git log --oneline -5",
    "journalctl -u nginx --since '1 hour ago'",
    "sort -h /tmp/x",
    "du -sh /tmp | sort -h | head -5",
    # 新只读规则
    "docker ps",
    "kubectl get pods",
    "tar -tzf app.tgz",
    "find /var/log -name '*.log' -mtime +7",
    "jq '.a' app.json",
    "getent passwd",
]

NOT_PROVABLE = [
    # safe 但会写盘
    "tee /tmp/x",
    "echo hi > /tmp/x",
    "curl -o /tmp/x https://example.com",
    "wget -O /tmp/x https://example.com",
    "sort -o /tmp/x /tmp/y",
    "sort --compress-program=sh /tmp/y",
    "sed -i 's/a/b/' /tmp/x",
    "tree -o /tmp/tree.txt /tmp",
    "uniq /tmp/in /tmp/out",
    "xxd /tmp/in /tmp/out",
    "diff --output=/tmp/d.txt a b",
    "git log --output=/tmp/x",
    "awk '{print > \"/tmp/x\"}' /tmp/y",
    # 破坏性写 / 状态变更
    "find /tmp -name '*.log' -delete",
    "date -s '2020-01-01'",
    "journalctl --rotate",
    "tar -xzf app.tgz",
    "tar -czf /tmp/x.tgz /tmp/data",
    "git branch -D feat",
    "git tag v1.0.0",
    "apt update",
    "apt upgrade -y",
    "systemctl restart nginx",
    "service nginx restart",
    "ip addr add 10.0.0.9/24 dev eth0",
    # 结构不可证明
    "env FOO=1 du -sh /tmp",
    "echo $(id)",
    "echo `id`",
    "cd /tmp",
]


@pytest.mark.parametrize("cmd", PROVABLE)
def test_整串可证明只读(cmd: str):
    assert is_provably_readonly(cmd).safe is True, cmd


@pytest.mark.parametrize("cmd", NOT_PROVABLE)
def test_写动作与未证明命令拒绝(cmd: str):
    assert is_provably_readonly(cmd).safe is False, cmd


def test_旧白名单证明不冒领_rule_id():
    v = is_provably_readonly("du -sh /tmp 2>/dev/null")
    assert v.safe is True and v.rule_id == ""


def test_新只读规则带_rule_id():
    v = is_provably_readonly("tar -tzf app.tgz")
    assert v.safe is True and v.rule_id == "ro.flag.tar_list.v1"


def test_混合命令整串证明_rule_id_来自新规则():
    v = is_provably_readonly("echo x | jq .")
    assert v.safe is True and v.rule_id == "ro.verb.jq.v1"


# ── 评审修复：getopt 合法形态（短选项粘连取值 / 长选项缩写）与执行开关 ──────────
# 精确字面量匹配会被这三种形态躲过，而它们在 prod 是真实写盘或直接执行外部程序。

GETOPT_FORM_CASES = [
    "sort -oout f",
    "sort --out=/tmp/x f",
    "sort --ou=/tmp/x f",
    "diff -oout a b",
    "diff --out /tmp/f a b",
    "tree -oout /tmp",
    "dmesg -c",
    "dmesg -D",
    "dmesg -E",
    "dmesg -n 1",
    "iostat -o /tmp/x",
    "rg --pre 'sh -c id' pat f",
    "rg --hostname-bin evil pat",
    "rg --index pat f",
    "kubectl get --raw /api/v1/nodes/x -X DELETE",
    "kubectl get pods -X POST",
    "kubectl get pods --method=PATCH",
    "ls && sort -oout f",
    "echo x | rg --pre=sh pat",
]


@pytest.mark.parametrize("cmd", GETOPT_FORM_CASES)
def test_getopt_形态与执行开关必须拒绝(cmd: str):
    assert is_provably_readonly(cmd).safe is False, f"{cmd!r} 被合法缩写形态放行"


PROVABLE_AFTER_HARDENING = [
    "sort -rh /tmp/x",
    "diff a b",
    "tree /tmp",
    "dmesg -T",
    "dmesg --ctime",
    "iostat 1 3",
    "kubectl get pods",
    "kubectl -n prod get cm x -o yaml",
    "rg -n pat f",
    "rg --ignore-case pat f",
]


@pytest.mark.parametrize("cmd", PROVABLE_AFTER_HARDENING)
def test_加固不误伤常规只读(cmd: str):
    assert is_provably_readonly(cmd).safe is True, f"{cmd!r} 应保持可证明只读"


def test_dig_的执行型_option_不被证明():
    assert is_provably_readonly("dig +cmd example.com").safe is False
    assert is_provably_readonly("dig +short example.com A").safe is True


def test_三字母缩写同样躲不过写开关匹配():
    for cmd in ("sort --o=/tmp/x f", "diff --o=out a b", "tree --o=/tmp f",
                "sort --o /tmp/x f"):
        assert is_provably_readonly(cmd).safe is False, cmd


BLOCKING_FORMS = [
    "tail -f /var/log/syslog",
    "tail -F /var/log/syslog",
    "journalctl -f",
    "journalctl --follow -u nginx",
    "dmesg -w",
    "ping 10.0.0.1",
    "vmstat 1",
    "iostat 1",
    "ls && tail -f /var/log/x",
]


@pytest.mark.parametrize("cmd", BLOCKING_FORMS)
def test_跟随与无限运行形态不自动交付(cmd: str):
    """不改状态，但会把可见终端占死、交付只能等超时 —— 交给确认卡。"""
    assert is_provably_readonly(cmd).safe is False, cmd


@pytest.mark.parametrize("cmd", [
    "tail -n 50 /var/log/syslog",
    "journalctl -u nginx -n 50",
    "dmesg -T",
    "ping -c 3 10.0.0.1",
    "vmstat 1 3",
    "iostat 1 3",
])
def test_有界读取仍可自动交付(cmd: str):
    assert is_provably_readonly(cmd).safe is True, cmd


HANG_OR_WRITE_CASES = [
    # 等 stdin：PTY 里没人送输入，交付只能挂到超时
    "cat", "sort", "grep error", "wc", "tr a-z A-Z", "uniq", "xxd",
    "echo hi; cat",
    # 跟随型（-f 只在特定子命令下才是跟随）
    "kubectl logs -f web",
    "kubectl logs --follow web",
    "docker logs -f web",
    "journalctl -F",
    # 无限重复 / 无限源 / 过长等待
    "netstat -c", "netstat --continuous", "lsof -r", "lsof -r 5",
    "sleep 3600", "sleep 10m", "cat /dev/zero", "strings /dev/zero",
    "vmstat 1 999999", "ping -c 4 -i 3600 10.0.0.1",
    # 真正写盘的漏网开关
    "file -C /etc/magic", "file --compile /etc/magic",
    "dmesg -k", "dmesg --klogd",
    "journalctl --update-catalog", "journalctl --setup-keys",
]


@pytest.mark.parametrize("cmd", HANG_OR_WRITE_CASES)
def test_挂等与漏网写盘形态必须确认(cmd: str):
    assert is_provably_readonly(cmd).safe is False, cmd


@pytest.mark.parametrize("cmd", [
    "du -sh /tmp | sort -h | head -5",
    "cat /etc/hosts | sort",
    "grep error /var/log/syslog | wc -l",
    "kubectl logs --since 10m web",
    "docker logs --tail 50 web",
    "file /etc/hosts",
    "sleep 2",
    "ping -c 3 10.0.0.1",
    "grep x /dev/null",
])
def test_管道下游与有界读取不被误杀(cmd: str):
    assert is_provably_readonly(cmd).safe is True, cmd


def test_kubectl_watch_形态不自动交付():
    for cmd in ("kubectl get pods -w", "kubectl get pods --watch",
                "kubectl top pods --watch", "kubectl get pods -o watch",
                "kubectl get pods -w -o json"):
        assert is_provably_readonly(cmd).safe is False, cmd
    assert is_provably_readonly("kubectl get pods -o wide").safe is True


def test_等待时长按整链求和():
    assert is_provably_readonly("sleep 280 && sleep 280 && sleep 280 && sleep 280").safe is False
    assert is_provably_readonly("sleep 2 && sleep 2").safe is True


def test_换行接在管道后仍是管道段():
    assert is_provably_readonly("ls |\ncat").safe is True
    assert is_provably_readonly("du -sh /tmp |\nsort -h |\nhead -5").safe is True
