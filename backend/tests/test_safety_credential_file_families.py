"""各生态默认凭据落点的读取审批：aws/kube/netrc/git-credentials/docker/npmrc/pypirc/gcloud。

审查 2026-10-25 发现：`_SECRET_FILE_RE` 只覆盖 /etc 家族 + `.ssh` + dotenv，
而各生态的标准凭据文件全部免审直读 —— 实测（本文件写下时的修复前行为）：

    cat ~/.aws/credentials          → safe   （AK/SK 明文）
    cat ~/.kube/config              → safe   （内嵌客户端证书 / token）
    cat ~/.netrc                    → safe   （经典明文凭证 store）
    cat ~/.git-credentials          → safe   （git 明文凭证 store）
    cat ~/.docker/config.json       → safe   （registry auth 的 base64 user:pass）
    cat ~/.npmrc                    → safe   （_authToken 发布 token）
    cat ~/.pypirc                   → safe   （PyPI 上传 token）

这些全是"密钥落盘的默认位置"，与 `.env` 同罪而不同判。修复走 `_is_secret_path`
统一入口（读侧 `_classify_secret_read` 与 tar/cp 敏感源 `_is_sensitive_source`
共用），Windows 形态在 `_WIN_SECRET_PATH_RE` 严格对齐。

误拦侧同样钉住（收紧判定的代价必须由用例说清楚）：
- `.aws/config` 只是 profile 配置、不含密钥 —— 刻意只收 `.aws/credentials`，
  不收整个 `.aws` 目录（与 `.ssh` 目录本体那条不同：那里 id_* 是常态文件名）；
- `.npmrc.bak` / `.git-credentials.old` 是凭证的备份，**该**命中（同 dotenv
  备份不豁免的口径），不算误拦；
- `kubectl --kubeconfig ~/.kube/config get pods` 这类"使用"而非"读取"的显式
  传参会多一张审批卡 —— F6 先例（--env-file=.env）已接受这种取舍：覆盖面优先，
  卡片代价由 once / 本会话允许消化。

登记未补的缺口：`/etc/ssl/private` 服务端私钥、任意路径的 `.pem/.key/.ppk`
主机私钥（退役文档 §7 的主机私钥条目）仍未收 —— 本文件不假装覆盖它们。
"""
import pytest

from agent.harness.command_explain import explain_for_reason
from agent.harness.safety import classify_command, classify_constructed_command
from agent.tools.builtin.ops_common import base64_read_cmd

_LEVEL = {"safe": 0, "dangerous": 1, "hardline": 2}

# 自由文本 cat 形态（每条都曾是"免审直读"的实测样本）
CREDENTIAL_READS = [
    "cat ~/.aws/credentials",
    "cat /root/.aws/credentials",
    "cat /home/deploy/.aws/credentials",
    "cat ~/.kube/config",
    "cat ~/.netrc",
    "cat /root/.netrc",
    "cat ~/.git-credentials",
    "cat ~/.docker/config.json",
    "cat /root/.docker/config.json",
    "cat ~/.npmrc",
    "cat /srv/app/.npmrc",  # 项目级 .npmrc 常含发布 token，同样命中（文档化的误拦取舍）
    "cat ~/.pypirc",
    "cat ~/.config/gcloud/legacy_credentials/me@example.com/adc.json",
    "cat ~/.config/gcloud/credentials.db",
    "cat ~/.config/gcloud/application_default_credentials.json",
]


@pytest.mark.parametrize("cmd", CREDENTIAL_READS)
def test_凭据落点读取要审批(cmd):
    level, reason = classify_command(cmd)
    assert _LEVEL[level] >= _LEVEL["dangerous"], f"{cmd!r} 判 {level}：默认凭据落点不该免审直读"
    assert reason, f"{cmd!r} 非 safe 必须带 reason"
    assert explain_for_reason(reason), f"reason {reason!r} 没有中文后果说明"


@pytest.mark.parametrize("path", [
    "/root/.aws/credentials",
    "/home/deploy/.kube/config",
    "/root/.netrc",
    "/home/deploy/.git-credentials",
    "/root/.docker/config.json",
    "/home/deploy/.pypirc",
])
def test_file_read_代理模板同样命中(path):
    """`file_read` 的分类代理是 `base64 <path>`（constructed 通路），与自由文本同判。"""
    level, reason = classify_constructed_command(base64_read_cmd(path))
    assert _LEVEL[level] >= _LEVEL["dangerous"], f"base64 读 {path!r} 判 {level}"


def test_粘连flag与等号flag形态():
    """F6 两种粘连形态不得因 flag 位而漏过。"""
    from agent.harness.safety import _classify_secret_read

    assert _classify_secret_read("grep -f~/.netrc pattern")[0] == "dangerous"
    assert _classify_secret_read("kubectl --kubeconfig=~/.kube/config get pods")[0] == "dangerous"
    # 反向锁：不含凭据名的 flag 值不得被牵连
    assert _classify_secret_read("grep -f/tmp/patterns.txt log")[0] == "safe"


def test_备份后缀同罪():
    """凭证的备份抄的就是真身（dotenv `.bak` 同口径），落审批而不是放行。"""
    from agent.harness.safety import _classify_secret_read

    for path in ("~/.npmrc.bak", "/root/.git-credentials.old", "~/.aws/credentials.20260101"):
        assert _classify_secret_read(f"cat {path}")[0] == "dangerous", path


NOT_CREDENTIALS = [
    "~/.aws/config",          # profile 配置不含密钥：刻意只收 credentials（最重要的反例锁）
    "~/.aws/cli/cache",       # aws cli 缓存目录不在判据内
    "/srv/app/docker/config.json",  # 无 .docker 前缀点
    "/srv/app/npmrc.txt",           # 无前导点，不是 npmrc
    "/srv/app/.npmrcfoo",           # 兄弟名字：\b 边界挡住
    "/srv/app/.kube/configs/notes.txt",  # .kube/config 的兄弟目录不是 kubeconfig 本体
    "/var/log/nginx/access.log",
    "/etc/ssh/ssh_host_rsa_key.pub",     # 公钥不是秘密
    "/srv/app/.env.example",             # dotenv 占位模板照旧豁免（别被本次改动牵连）
]


@pytest.mark.parametrize("path", NOT_CREDENTIALS)
def test_非凭据路径不得被牵连(path):
    from agent.harness.safety import _classify_secret_read

    level, _reason = _classify_secret_read(f"cat {path}")
    assert level == "safe", f"{path!r} 被判 {level}：正常排查被误拦"


def test_windows_形态与_posix_同罪():
    """同一条路径换盘符/大小写写法必须同判（Windows 本机分支走 Python open()）。"""
    for path in (
        r"C:\Users\alice\.aws\credentials",
        "C:/Users/Alice/.AWS/CREDENTIALS",   # Windows 文件系统大小写不敏感
        r"C:\Users\alice\.kube\config",
        r"C:\Users\alice\.npmrc",
        r"C:\Users\alice\.docker\config.json",
    ):
        level, reason = classify_command(f"cat {path}")
        assert _LEVEL[level] >= _LEVEL["dangerous"], f"读取 {path!r} 判 {level}"
        assert explain_for_reason(reason)


def test_敏感tar源同口径():
    """`_is_sensitive_source` 复用同一条正则：把凭据打进 tar 包也必须拦。"""
    level, _reason = classify_command("tar czf /tmp/x.tgz ~/.aws/credentials")
    assert _LEVEL[level] >= _LEVEL["dangerous"], f"tar 打包凭据判 {level}"
