"""凭据路径判定的三种真实形态：POSIX 斜杠、Windows 反斜杠+大小写、dotenv 家族。

`_SECRET_FILE_RE` 与写侧的持久化判据最初都按 POSIX 单形态写，而本产品有两条真实的
Windows 通路：
- `file_read` / `file_grep` / `file_glob` 在无 SSH 会话时降级读**本机**文件
  （已退役的 `docs/security-boundary.md` §7 的"不对称"条目），本机就是 Windows，路径形态是
  `C:\\Users\\alice\\.ssh\\id_rsa`，且文件系统大小写不敏感；
- `file_read` 的分类代理模板是 `base64 <path>`，斜杠/反斜杠两种写法进的是同一条判定。

实测（本文件写下时的 HEAD 行为）：
    cat C:\\Users\\alice\\.ssh\\id_rsa      → safe      （同一路径写成 / 分隔是 dangerous）
    cat C:/Users/alice/.ssh/ID_RSA         → safe      （Windows 上这就是同一个私钥）
    cat /app/.env                           → safe      （dotenv 是应用凭据的标准落点）
而已退役的 `docs/security-boundary.md` §4 把 `.env` 列在"敏感目录 → dangerous"的落点里 ——
文档声明的边界在代码里不存在。

误拦侧同样钉住，因为收紧凭据判定的代价必须由用例说清楚：
- 占位模板 `.env.example` / `.env.sample` / `.env.dist` / `.env.template` 惯例上不含真凭据，
  开发里 `cat` 它们是常态，不该逐条弹卡；
- `backend.env`、`.environment` 这类兄弟名字不是 dotenv（前缀匹配会把它们一起拖进来）；
- `/etc/passwdx`、`/etc/shadow_backup` 不是 PAM 读的文件：写它们原本判 **hardline**
  （allowlist 也放不走、没有审批出口），而 `cp /etc/passwd /etc/passwd.new` 之后编辑再
  `mv` 回去本是标准运维手法。判据补上路径边界后它们落到 dangerous（一张审批卡），
  真正的 `/etc/passwd` 仍照旧 hardline —— 这是本次唯一一处"放松"，放松的方向由边界
  而不是由删规则实现，同文件的 `_is_sensitive_path` 早就是这个写法（两处规则必然漂移，
  这次是让它们对齐）。
"""
import pytest

from agent.harness.command_explain import explain_for_reason
from agent.harness.safety import classify_command, classify_constructed_command
from agent.tools.builtin.ops_common import (
    base64_read_cmd,
    base64_write_cmd_for_classification,
)

_LEVEL = {"safe": 0, "dangerous": 1, "hardline": 2}

WINDOWS_KEY_READS = [
    r"cat C:\Users\alice\.ssh\id_rsa",
    r"C:/Users/alice/.ssh/id_rsa",
    r"C:\Users\alice\.ssh\id_ed25519",
    r"D:/deploy/secrets/id_dsa",
    r"C:\Users\alice\.ssh",
]

DOTENV_READS = [
    "/app/.env",
    "/srv/www/.env.production",
    r"C:\inetpub\wwwroot\.env",
    "/home/deploy/releases/20261008/.env",
]

CREDENTIAL_WRITES = [
    "/app/.env",
    "/srv/www/.env.production",
    r"C:\inetpub\wwwroot\.env",
    r"C:\Users\alice\.ssh\authorized_keys",
    "/root/.ssh/authorized_keys",
]

# 这些不是凭据路径：既不是 dotenv 家族，也不是 .ssh 下的密钥
NOT_CREDENTIALS = [
    "/srv/app/backend.env",
    "/srv/app/.environment",
    "/srv/app/.envs/notes.txt",
    "/var/log/nginx/access.log",
    "/etc/ssh/ssh_host_rsa_key.pub",
    "/srv/app/.env.example",
    "/srv/app/.env.sample",
    "/srv/app/.env.dist",
    "/srv/app/.env.template",
    "/srv/app/.env.tpl",
    "/srv/app/.env.default",
]


def _constructed(path: str) -> tuple[str, str | None]:
    return classify_constructed_command(base64_read_cmd(path))


def _constructed_write(path: str) -> tuple[str, str | None]:
    return classify_constructed_command(base64_write_cmd_for_classification(path))


@pytest.mark.parametrize("path", WINDOWS_KEY_READS)
def test_windows_私钥与_ssh_目录读取要审批(path):
    """反斜杠、大小写、以及 .ssh 目录本体都必须与 POSIX 同罪。"""
    level, reason = _constructed(path)
    assert _LEVEL[level] >= _LEVEL["dangerous"], (
        f"读取 {path!r} 判 {level}：Windows 本机分支走 Python open()，这就是在偷私钥")
    assert reason, f"{path!r} 非 safe 必须带 reason"
    assert explain_for_reason(reason), f"reason {reason!r} 没有中文后果说明"
    # 同一条路径换个分隔符写法必须同判（否则模型换个写法就绕过）
    alt = path.replace("\\", "/") if "\\" in path else path.replace("/", "\\")
    assert _LEVEL[_constructed(alt)[0]] >= _LEVEL["dangerous"], f"{alt!r} 换分隔符即绕过"


@pytest.mark.parametrize("path", DOTENV_READS)
def test_dotenv_读取要审批(path):
    """`.env` 是应用凭据的标准落点；文档 §4 早就把它列为敏感目标。"""
    level, reason = _constructed(path)
    assert _LEVEL[level] >= _LEVEL["dangerous"], f"读取 {path!r} 判 {level}"
    assert explain_for_reason(reason)


def test_dotenv_自由文本读取同样审批():
    """不只看 ops 模板：人/模型手敲 `cat /app/.env` 也必经审批。"""
    for cmd in ("cat /app/.env", "grep -r TOKEN /srv/www/.env", "nl -ba D:/app/.env"):
        level, reason = classify_command(cmd)
        assert _LEVEL[level] >= _LEVEL["dangerous"], f"{cmd!r} 判 {level}"
        assert explain_for_reason(reason)


def test_home_折叠后的_windows_私钥形态():
    """`normalize_command_for_detection` 第 4 步会把绝对家目录折成 `~`，
    Windows 形态随之变成 `~\\.ssh\\id_rsa` —— 折算点若只认盘符，这条就漏了。
    """
    level, reason = classify_command(r"cat ~\.ssh\id_rsa")
    assert _LEVEL[level] >= _LEVEL["dangerous"], f"~\\.ssh 私钥形态判 {level}"
    assert explain_for_reason(reason)


def test_归一化不再把_windows_路径碾成无形状串():
    """根因锁：剥反斜杠转义（抵御 `r\\m` 混淆）会连带吃掉路径分隔符。

    实测 HEAD~ 上 `cat C:\\Users\\alice\\.ssh\\id_rsa` 的检测副本是
    `cat C:Usersalice.sshid_rsa` —— 形状没了，下游每一条路径判据（凭据正则、持久化
    前缀、敏感目录、rm 目标、正则表）都无从命中。折算必须发生在剥转义**之前**。
    """
    from agent.harness.safety import _detection_copy

    detection = _detection_copy(r"cat C:\Users\alice\.ssh\id_rsa")
    assert "C:/Users/alice/.ssh/id_rsa" in detection, detection
    # 反向锁：命令位的 `r\m` 混淆照旧被还原，本次折算没有削弱那一层防御
    assert _detection_copy(r"r\m -rf /tmp/x").startswith("rm -rf")


def test_flag_位粘连的相对_dotenv_值不再漏过():
    """`--env-file=.env` 的值既没有前导 `/`，也不在普通参数位 —— 原先两条都够不着。

    直接按段判（`_classify_secret_read`），避开「docker 不在只读白名单 → 默认审批也
    会给 dangerous」这条无关通路 —— 用 classify_command 断言会因为错误的原因通过。
    """
    from agent.harness.safety import _classify_secret_read

    assert _classify_secret_read("docker run --env-file=.env web")[0] == "dangerous"
    assert _classify_secret_read("docker run --env-file=/srv/app/.env web")[0] == "dangerous"
    # 反向锁：纯 flag 与不含凭据名的值不得被牵连（否则每条带值的长选项都弹卡）
    assert _classify_secret_read("sort -k2,2 /tmp/data.tsv")[0] == "safe"
    assert _classify_secret_read("tar czf /tmp/b.tgz --exclude=/srv/app/.env.example /srv")[0] == "safe"


@pytest.mark.parametrize("path", CREDENTIAL_WRITES)
def test_凭据落点写入不得放行(path):
    """写侧同一口径：`.ssh` 植入仍 hardline，dotenv 写入至少一张审批卡。"""
    level, _reason = _constructed_write(path)
    assert _LEVEL[level] >= _LEVEL["dangerous"], f"写入 {path!r} 判 {level}"
    if ".ssh" in path.lower():
        assert level == "hardline", f"authorized_keys 植入必须 hardline，实得 {level}"


@pytest.mark.parametrize("path", NOT_CREDENTIALS)
def test_兄弟名字与占位模板不得被牵连(path):
    """收紧凭据判定的误拦代价必须由用例封死：占位模板与相似名字不许进审批。"""
    level, _reason = _constructed(path)
    assert level == "safe", f"{path!r} 被判 {level}（{_reason}）：正常排查被误拦"


def test_普通路径写入照旧放行():
    # 样本必须避开 _SENSITIVE_DIRS 本身（/srv、/var、/opt 下的写入一直是 dangerous，
    # 拿它们当「普通路径」会让这条误拦锁按错误的原因通过）
    for path in ("/tmp/scratch.txt", "/data/app/config.yaml",
                 "/mnt/nas/reports/summary.json", "/workspace/.envs/notes.txt"):
        level, _ = _constructed_write(path)
        assert level == "safe", f"{path!r} 被判 {level}：非凭据路径不该逐条弹卡"


# ── 持久化判据的路径边界 ────────────────────────────────────

HARDLINE_PERSISTENCE = [
    "echo x > /etc/passwd",
    "echo x >> /etc/shadow",
    "tee /etc/sudoers",
    "echo x > /etc/ld.so.preload",
    # drop-in 目录形态：补路径边界时最容易顺手丢掉的一条（裸前缀本来意外盖住了它）
    "echo x > /etc/sudoers.d/99-backdoor",
    "tee /etc/sudoers.d",
    # 先写副本再改回真身：真身那一段必须被抓住
    "cp /etc/passwd /etc/passwd.new && mv /etc/passwd.new /etc/passwd",
]

DANGEROUS_BUT_NOT_HARDLINE = [
    "/etc/passwdx",
    "/etc/shadow_backup",
    "/etc/passwd.new",
    "/etc/sudoers.tmp",
]


@pytest.mark.parametrize("cmd", HARDLINE_PERSISTENCE)
def test_真身植入仍是硬阻断(cmd):
    level, reason = classify_command(cmd)
    assert level == "hardline", f"{cmd!r} 被判 {level}：植入类没有审批出口是有意设计"
    assert explain_for_reason(reason)


@pytest.mark.parametrize("path", DANGEROUS_BUT_NOT_HARDLINE)
def test_同名兄弟文件降级为审批而不是无路可走(path):
    """hardline 没有审批出口、allowlist 也放不走。

    `/etc/passwd.new` / `/etc/shadow_backup` 不是 PAM 读的文件，把它们判 hardline 等于
    永久封死"改配置前先备份一份再编辑"这类标准手法。补上路径边界后它们落到 dangerous
    （一张卡），而不是无路可走。
    """
    level, _reason = _constructed_write(path)
    assert level == "dangerous", (
        f"写 {path!r} 判 {level}：非植入目标不该被永久阻断（hardline 无审批出口）")


def test_边界规则与同文件敏感目录判据同口径():
    """`_is_sensitive_path` 用 `d + '/'` 做边界，持久化判据原先用裸 startswith。

    同一模块里两条相邻规则用两种边界 = 迟早漂移；这里钉它们对同一个样本给出同一形态的
    答案（前缀兄弟名字都不算命中）。
    """
    from agent.harness.safety import _is_persistence_target, _is_sensitive_path

    sibling = "/etc/passwdx"
    assert not _is_persistence_target(sibling)
    assert _is_sensitive_path(sibling), "兄弟名字仍在敏感目录内 → 落 dangerous 而不是 safe"
    assert _is_persistence_target("/etc/passwd")
    # 两个 drop-in 目录姿态不同，是按契约表定的（test_ops_content_classification.py），
    # 补边界时别把它们当成一类
    assert _is_persistence_target("/etc/sudoers.d/99-x")
    assert not _is_persistence_target("/etc/cron.d/backup")
