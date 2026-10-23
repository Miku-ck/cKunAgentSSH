"""只读证明 / 破坏性写 / 执行注入三条判定链的规则级测试。"""
import pytest

from agent.harness.readonly_policy import (
    classify_destructive_write,
    classify_exec_injection,
    classify_readonly,
    tar_list_mode,
)
from agent.harness.command_parser import tokenize_segment

READONLY_CASES = [
    ("jq '.items[] | .name' app.json", "ro.verb.jq.v1"),
    ("jq -n '{a: 1}'", "ro.verb.jq.v1"),
    ("yq '.spec.replicas' deploy.yaml", "ro.verb.yq.v1"),
    ("getent passwd", "ro.sub.getent.v1"),
    ("getent group wheel", "ro.sub.getent.v1"),
    ("dig +short example.com A", "ro.verb.dig.v1"),
    ("nslookup example.com", "ro.verb.nslookup.v1"),
    ("host example.com", "ro.verb.host.v1"),
    ("ping -c 3 10.0.0.1", "ro.verb.ping.v1"),
    ("traceroute -n 10.0.0.1", "ro.verb.traceroute.v1"),
    ("lsblk -f", "ro.verb.lsblk.v1"),
    ("blkid /dev/sda1", "ro.verb.blkid.v1"),
    ("findmnt /", "ro.verb.findmnt.v1"),
    ("dmidecode -t system", "ro.verb.dmidecode.v1"),
    ("smartctl -a /dev/sda", "ro.flag.smartctl.v1"),
    ("ethtool eth0", "ro.flag.ethtool.v1"),
    ("ethtool -i eth0", "ro.flag.ethtool.v1"),
    ("tc qdisc show dev eth0", "ro.sub.tc.v1"),
    ("nft list ruleset", "ro.sub.nft.v1"),
    ("iptables -t nat -L -n -v", "ro.flag.iptables.v1"),
    ("iptables -S", "ro.flag.iptables.v1"),
    ("docker ps --format '{{.Names}}'", "ro.sub.docker.v1"),
    ("docker logs -n 100 web", "ro.sub.docker.v1"),
    ("docker stats --no-stream", "ro.sub.docker.v1"),
    ("docker system df", "ro.sub.docker.v1"),
    ("docker compose -f docker-compose.yml ps", "ro.sub.docker_compose.v1"),
    ("kubectl -n prod rollout status deploy/web", "ro.sub.kubectl.v1"),
    ("kubectl auth can-i get pods", "ro.sub.kubectl.v1"),
    ("helm -n prod show values app", "ro.sub.helm.v1"),
    ("unzip -l app.zip", "ro.flag.unzip.v1"),
    ("zipinfo app.zip", "ro.verb.zipinfo.v1"),
    ("openssl x509 -in cert.pem -noout -text", "ro.flag.openssl_x509.v1"),
    ("tar -tzf app.tgz", "ro.flag.tar_list.v1"),
    ("git ls-remote origin", "ro.sub.git_ls_remote.v1"),
    ("git config --get user.name", "ro.flag.git_config.v1"),
    ("systemctl list-dependencies nginx", "ro.sub.systemctl.v1"),
    ("crontab -l", "ro.flag.crontab.v1"),
    ("at -c 7", "ro.flag.at.v1"),
    ("ssh -V", "ro.verb.ssh_version.v1"),
    ("date +%Y-%m-%d", "ro.verb.date.v1"),
    ("find /var/log -name '*.log' -mtime +7", "ro.verb.find.v1"),
]


@pytest.mark.parametrize("cmd,rule_id", READONLY_CASES)
def test_只读证明命中且_rule_id_非空(cmd: str, rule_id: str):
    verdict = classify_readonly(cmd)
    assert verdict.safe is True, f"{cmd!r} 应被证明只读"
    assert verdict.rule_id == rule_id, f"{cmd!r} 命中了错误规则 {verdict.rule_id!r}"


UNSAFE_READONLY_CASES = [
    "jq -i '.a=1' app.json",
    "yq -i '.a=1' deploy.yaml",
    "getent shadow",
    "ping -f 10.0.0.1",
    "blkid -w /tmp/cache /dev/sda",
    "dmidecode --dump-bin /tmp/d.bin",
    "smartctl -t short /dev/sda",
    "ethtool -s eth0 speed 100",
    "tc qdisc add dev eth0 root pfifo",
    "nft add rule inet filter input drop",
    "iptables -F",
    "iptables -L -Z",
    "docker compose up -d",
    "docker stats",
    "kubectl rollout restart deploy/web",
    "helm upgrade app chart",
    "unzip app.zip",
    "openssl req -new -key k -out r.csr",
    "tar -xzf app.tgz",
    "git ls-remote --upload-pack='sh -c id' origin",
    "git config user.name Neo",
    "systemctl restart nginx",
    "crontab -r",
    "at -f /tmp/job now",
    "ssh root@10.0.0.1",
    "jq '.x' > /tmp/out",
    'jq "$(id)"',
    "env jq .",
    "echo hi",
    "make -j8",
]


@pytest.mark.parametrize("cmd", UNSAFE_READONLY_CASES)
def test_只读证明对写动作_注入_未知命令拒绝(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"


REVIEW_BYPASS_CASES = [
    "jq . <(rm -rf /tmp/x)",
    "jq . >(cat)",
    "git ls-remote --exec='sh -c id' origin",
    "yq --split-exp '.name' f.yaml",
    "yq -s '.name' f.yaml",
    "docker compose config -o /tmp/out.yml",
    "ethtool --set-priv-flags eth0 rx on",
    "tar -t --index-file=/tmp/idx -f a.tar",
    "iptables --modprobe=/tmp/evil -L",
]


@pytest.mark.parametrize("cmd", REVIEW_BYPASS_CASES)
def test_评审发现的绕过必须拒绝(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"


@pytest.mark.parametrize("cmd", [
    "jq '.a <(b)' f.json",
    "git ls-remote origin",
    "docker compose config",
    "iptables -L -n",
])
def test_反锁_正常用法保持只读(cmd: str):
    assert classify_readonly(cmd).safe is True, f"{cmd!r} 应保持只读"


DESTRUCTIVE_CASES = [
    ("date -s '2020-01-01'", "rw.destructive.date_set.v1"),
    ("date 202001010000", "rw.destructive.date_positional.v1"),
    ("find /tmp -delete", "rw.destructive.find_action.v1"),
    ("find /tmp -exec rm {} +", "rw.destructive.find_action.v1"),
    ("find /tmp -fprint /tmp/out", "rw.destructive.find_action.v1"),
    ("tar -xzf app.tgz", "rw.destructive.tar_write.v1"),
    ("tar --delete -f app.tar old.txt", "rw.destructive.tar_write.v1"),
    ("journalctl --rotate", "rw.destructive.journalctl_write.v1"),
    ("journalctl --vacuum-time=1s", "rw.destructive.journalctl_write.v1"),
    ("yq -i '.a=1' deploy.yaml", "rw.destructive.yq_inplace.v1"),
]


@pytest.mark.parametrize("cmd,rule_id", DESTRUCTIVE_CASES)
def test_破坏性写守卫(cmd: str, rule_id: str):
    verdict = classify_destructive_write(cmd)
    assert verdict.safe is False
    assert verdict.rule_id == rule_id


@pytest.mark.parametrize("cmd", [
    "date +%s",
    "date -u +%Y-%m-%dT%H:%M:%SZ",
    "date -d yesterday +%F",
    "find /var/log -name '*.log' -mtime +7",
    "tar -czf /tmp/backup.tgz /home/user/project",
    "tar -tzf app.tgz",
    "journalctl -u nginx -n 50",
    "yq '.a' deploy.yaml",
])
def test_破坏性写守卫不误伤纯只读(cmd: str):
    assert classify_destructive_write(cmd).safe is True


def test_托管日志目录的_find_delete_可豁免():
    log_dir = "/home/admin-pl/.trae-cn-server/manager-logs"
    cmd = f"find {log_dir} -name '*.log' -mtime +7 -delete"
    assert classify_destructive_write(cmd).safe is False
    assert classify_destructive_write(cmd, find_actions_allowed=True).safe is True


EXEC_INJECTION_CASES = [
    ("git -c core.pager=sh status", "ex.inject.git_config_env.v1"),
    ("git --exec-path=/tmp status", "ex.inject.git_config_env.v1"),
    ("git log --upload-pack='sh -c id'", "ex.inject.git_transport.v1"),
    ("git ls-remote ext::sh origin", "ex.inject.git_transport.v1"),
    ("tar -I 'sh -c' -cf /tmp/x.tar /tmp/data", "ex.inject.tar_hook.v1"),
    ("tar --to-command=sh -xf app.tar", "ex.inject.tar_hook.v1"),
    ("openssl x509 -engine dynamic -in c.pem -noout", "ex.inject.openssl_engine.v1"),
    ("awk '{system(\"id\")}' /tmp/x", "ex.inject.awk_system.v1"),
]


@pytest.mark.parametrize("cmd,rule_id", EXEC_INJECTION_CASES)
def test_执行注入守卫(cmd: str, rule_id: str):
    verdict = classify_exec_injection(cmd)
    assert verdict.safe is False
    assert verdict.rule_id == rule_id


def test_git_exec_也被识别为传输注入():
    assert classify_exec_injection("git ls-remote --exec='sh -c id' origin").safe is False


@pytest.mark.parametrize("cmd", [
    "git log -c --oneline",
    "git config --get core.pager",
    "tar -czf /tmp/x.tgz /tmp/data",
    "openssl x509 -in c.pem -noout",
    "awk '{print $1}' /tmp/x",
])
def test_执行注入守卫不误伤正常用法(cmd: str):
    assert classify_exec_injection(cmd).safe is True


def test_tar_模式判定():
    assert tar_list_mode(tokenize_segment("tar -tzf app.tgz")) is True
    assert tar_list_mode(tokenize_segment("tar --list -f app.tar")) is True
    assert tar_list_mode(tokenize_segment("tar -czf app.tgz dir")) is False
    assert tar_list_mode(tokenize_segment("tar -xzf app.tgz")) is False


# ── Fix round 2：长选项前缀缩写 / 短选项簇 / compose -o 贴写 ─────────────────

PREFIX_ABBREV_BYPASS_CASES = [
    "git ls-remote --exe='sh -c id' origin",
    "git ls-remote --upload-p='sh -c id' origin",
    "tar -tv --index-f=/tmp/idx -f a.tar",
    "docker compose config -o=/tmp/x.yml",
    "docker compose config -o/tmp/x.yml",
    "yq -is '.name' f.yaml",
    "ethtool --set-priv eth0 rx on",
]


@pytest.mark.parametrize("cmd", PREFIX_ABBREV_BYPASS_CASES)
def test_前缀缩写与短选项簇必须拒绝(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"


def test_git_传输选项前缀缩写也被识别为注入():
    assert classify_exec_injection("git ls-remote --exe='sh -c id' origin").safe is False


@pytest.mark.parametrize("cmd", [
    "git ls-remote --heads origin",
    "tar -tvf a.tar",
    "docker compose config --quiet",
    "yq -r '.a' f.yaml",
    "ethtool --show-priv-flags eth0",
])
def test_前缀缩写修复不误伤正常用法(cmd: str):
    assert classify_readonly(cmd).safe is True, f"{cmd!r} 应保持只读"


# ── Fix round 3：git 全局 --exec-path 的长选项前缀缩写 ───────────────────────

EXEC_PATH_ABBREV_CASES = [
    "git --exec-p=/tmp status",
    "git --exec-pat=/tmp status",
]


@pytest.mark.parametrize("cmd", EXEC_PATH_ABBREV_CASES)
def test_git_exec_path_前缀缩写必须拒绝(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"


def test_git_exec_path_前缀缩写也被识别为注入():
    assert classify_exec_injection("git --exec-p=/tmp status").safe is False


# ── Fix round 4：tar 外部程序 hook 的缩写/短选项簇、compose --output 分离形态、
#    blkid 长选项缩写 ─────────────────────────────────────────────────────────

TAR_HOOK_AND_ABBREV_CASES = [
    "tar -tI 'sh -c id' -f a.tar",
    "tar -tf a.tar --use-compress-pro='sh -c id'",
    "tar -cF 'sh -c id' -f /tmp/x.tar dir",
    "docker compose config --output /tmp/x.yml",
    "blkid --write-c /dev/sda",
]


@pytest.mark.parametrize("cmd", TAR_HOOK_AND_ABBREV_CASES)
def test_tar_hook_与长选项缩写必须拒绝(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"


@pytest.mark.parametrize("cmd", [
    "tar -tI 'sh -c id' -f a.tar",
    "tar -tf a.tar --use-compress-pro='sh -c id'",
    "tar -cF 'sh -c id' -f /tmp/x.tar dir",
])
def test_tar_外部程序_hook_也被识别为注入(cmd: str):
    assert classify_exec_injection(cmd).safe is False, f"{cmd!r} 仍是执行注入"


@pytest.mark.parametrize("cmd", [
    "tar -tzf app.tgz",
    "docker compose config",
    "blkid /dev/sda1",
])
def test_hook_与缩写修复不误伤正常只读(cmd: str):
    assert classify_readonly(cmd).safe is True, f"{cmd!r} 应保持只读"


@pytest.mark.parametrize("cmd", [
    "tar --zstd -tvf a.tar.zst",
    "tar --gzip -tzf a.tar.gz",
])
def test_过滤器长名不被误当外部程序_hook(cmd: str):
    assert classify_readonly(cmd).safe is True, f"{cmd!r} 应保持只读"


@pytest.mark.parametrize("cmd", [
    "tar -tf a.tar --zstd-prog='sh -c id'",
    "tar -tf a.tar --gzip-p='sh -c id'",
])
def test_过滤器_program_形态的缩写仍被拦截(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"


# ── Task 3 评审修复：POSIX 裸模式串（不带 - 的 tar xzf）───────────────────────

@pytest.mark.parametrize("cmd", [
    "tar xzf app.tgz",
    "tar xzf app.tgz -C /srv",
    "tar xvf a.tar",
    "tar -xf app.tgz",
    "tar x app.tar",
    "tar x -f app.tar",
    "tar u -f a.tar f",
    "tar r -f a.tar f",
    "tar A -f a.tar f",
])
def test_裸模式解压同样是破坏性写(cmd: str):
    verdict = classify_destructive_write(cmd)
    assert verdict.safe is False, f"{cmd!r} 解包写盘未被拦下"
    assert verdict.rule_id == "rw.destructive.tar_write.v1"


@pytest.mark.parametrize("cmd", [
    "tar tvf a.tar",
    "tar -tzf app.tgz",
    "tar t a.tar",
])
def test_裸模式列表仍是只读(cmd: str):
    assert classify_readonly(cmd).safe is True, f"{cmd!r} 应保持只读"


@pytest.mark.parametrize("cmd", [
    "tar czf /tmp/x.tgz dir",
    "tar cf a.tar rat",
    "tar cf a.tar cat",
])
def test_裸模式判定不把成员名误当模式串(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"
    assert classify_destructive_write(cmd).safe is True


def test_成员名与裸操作字母同形时保守升级_是已登记取舍():
    """`tar --file=a.tar x` 里的 x 是成员名，但裸操作字母只在 argv[1] 生效这条
    规则无法从字符串区分；保守按写盘拦一次审批，代价是多一张卡，不放过解压。"""
    assert classify_destructive_write("tar --file=a.tar x").safe is False
