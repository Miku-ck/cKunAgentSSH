"""白名单子命令的「形态」判定：既能列又能写的那几条不能只比第一个 token。

审查发现的三条同族误放（实测，均为修复前行为）：

    git branch -D feature-x          safety=safe  plan=放行
    git tag v1.0.0                   safety=safe  plan=放行
    git remote set-url origin <url>  safety=safe  plan=放行
    sort -o/tmp/pwned /etc/hostname  plan=放行（计划模式写着「不能改任何东西」）
    ip link set eth0 down            plan 这一层 _check_verb 放行（比错了 token）

前四条是同一根因：子命令表只认第一个非 flag token，而 `branch` / `tag` / `remote`
这几个子命令**既能列也能写**，区别只在后面跟了什么。第五（`git remote set-url`）
最重 —— 换掉 remote 之后，后续每一次 push/fetch 都把代码交给新地址，属于
supply-chain 侧路，不是「改改本地状态」。

`sort -o/tmp/x` 是另一条：写 flag 表按 `t.split("=")[0]` 比对，粘连短 flag 整段
去查表必然不命中。`ip link set eth0 down` 又是第三条：计划模式那侧拿子命令 `link`
去查 {set,del,flush,down,add}，永远不命中；深动词在子命令**之后**。

判定口径统一收在 safety（`git_form_writes` / `subaction_writes`），plan_mode 复用，
不再各存一份表 —— 同一份风险写两处就是这个仓库反复出现的「跨层字面量漂移」。

放松方向也一并锁死：只读形态（`git branch -a` / `git remote show origin`）不能因为
新规则被误拦，否则运维在审批侧和只读侧都会失去可用的查询手段。

运行: python -m pytest backend/tests/test_safety_git_form_and_glued_flags.py -v
"""
import pytest

from agent.harness import safety
from agent.harness.command_explain import explain_for_reason
from agent.harness.reins import plan_mode
from agent.harness.safety import (
    classify_command,
    classify_constructed_command,
    git_form_writes,
    subaction_writes,
)

# ── 1. 写形态：审批侧必须升级为 dangerous ────────────────────────────────────

WRITE_FORM_COMMANDS = [
    "git branch -D feature-x",
    "git branch -d feature-x",
    "git branch new-feature",
    "git branch -m old new",
    "git tag v1.0.0",
    "git tag -a v1.0.0 -m release",
    "git tag -d v1.0.0",
    "git remote add evil https://evil.example/repo.git",
    "git remote set-url origin https://evil.example/repo.git",
    "git remote rename origin old-origin",
    "git remote prune origin",
    "git diff --output=/tmp/leak.patch",
    "git log --output=/tmp/leak.log",
]


@pytest.mark.parametrize("cmd", WRITE_FORM_COMMANDS)
def test_git_write_form_requires_approval(cmd):
    """这些形态以前判 safe ⇒ 不审批就在目标机改动仓库状态。"""
    level, reason = classify_command(cmd)
    assert level == "dangerous", f"{cmd!r} 仍然免审批：{level}/{reason}"
    assert "writes repository state" in reason


@pytest.mark.parametrize("cmd", WRITE_FORM_COMMANDS)
def test_git_write_form_rejected_in_plan_mode(cmd):
    """审批侧的漏洞在计划模式同样存在：只读承诺比审批更强，两条都得堵。"""
    ok, why = plan_mode.classify_plan_command(cmd)
    assert ok is False, f"计划模式允许 {cmd!r}：{why}"


# ── 2. 反向锁：只读形态不能被新规则误拦 ──────────────────────────────────────

READ_FORM_COMMANDS = [
    "git status",
    "git branch",
    "git branch -a",
    "git branch -r",
    "git branch -vv",
    "git branch --show-current",
    "git branch --merged",
    "git tag",
    "git tag -l",
    "git remote",
    "git remote -v",
    "git remote show origin",
    "git remote get-url origin",
    "git log --oneline -n 20",
    "git diff HEAD~1",
    "git rev-parse HEAD",
]


@pytest.mark.parametrize("cmd", READ_FORM_COMMANDS)
def test_git_read_form_stays_allowed(cmd):
    ok, why = plan_mode.classify_plan_command(cmd)
    assert ok is True, f"只读 git 形态被误拦：{cmd!r} → {why}"
    assert classify_command(cmd)[0] == "safe", f"只读 git 形态被误升审批：{cmd!r}"


# ── 3. 粘连短 flag：写 flag 表不能只认独立 token ─────────────────────────────

def test_glued_output_flag_is_not_read_only():
    """`sort -o <file>` 命中表，`sort -o<file>` 以前整段查表不命中 ⇒ 落盘成功。"""
    ok, why = plan_mode.classify_plan_command("sort -o/tmp/pwned /etc/hostname")
    assert ok is False, "计划模式允许 sort -o 落盘"
    assert "写副作用" in why


def test_long_flag_with_equals_still_hits():
    ok, why = plan_mode.classify_plan_command("journalctl --vacuum-time=1s")
    assert ok is False
    assert "写副作用" in why or "vacuum" in (why or "")


def test_date_glued_set_flag():
    """`date -s2030-01-01` 与 `date -s 2030-01-01` 都必须拒（改系统时间）。"""
    assert plan_mode.classify_plan_command("date -s 2030-01-01")[0] is False
    assert plan_mode.classify_plan_command("date -s2030-01-01")[0] is False


# ── 4. 深动词在子命令之后，不是子命令本身 ────────────────────────────────────

def test_deep_subaction_scans_trailing_tokens():
    assert subaction_writes("ip", ["ip", "link", "set", "eth0", "down"]) == "ip set"
    assert subaction_writes("ip", ["ip", "addr"]) is None


def test_ip_link_set_down_rejected_in_plan_mode():
    """断网 = 远程 SSH 会话直接失联；以前 plan 这一层按子命令 `link` 查表，永不命中。"""
    ok, why = plan_mode.classify_plan_command("ip link set eth0 down")
    assert ok is False, f"计划模式允许断网：{why}"


# ── 5. 客户端模板：只放松「动词由谁选」，结构侧一条不少 ──────────────────────

def test_bound_template_still_allows_known_read_only_shape():
    """模板的动词由代码写死（base64/grep），不能被「未知动词默认审批」挡死。"""
    assert plan_mode.classify_plan_bound_command("base64 /etc/hostname")[0] is True
    assert plan_mode.classify_plan_bound_command(
        "grep -rnE --color=never 'x' /etc/hostname | head -n 200")[0] is True


@pytest.mark.parametrize("cmd", [
    "base64 /etc/shadow",
    "ip link set eth0 down",
    "sort -o/tmp/pwned /etc/hostname",
    "git remote set-url origin https://evil.example/repo.git",
])
def test_bound_template_keeps_structural_and_path_checks(cmd):
    ok, why = plan_mode.classify_plan_bound_command(cmd)
    assert ok is False, f"模板口径放行了 {cmd!r}：{why}"


def test_constructed_classifier_still_trusts_template_verb():
    """模板口径本身的行为边界（本文件其余用例的前提）：base64 不因「未知动词」升级。"""
    assert classify_constructed_command("base64 /etc/hostname")[0] == "safe"


# ── 6. 跨层漂移锁：plan_mode 不再各存一份子命令表 ────────────────────────────

def test_plan_subcommand_table_is_subset_of_safety():
    """计划模式的子命令表必须是 safety 表的严格子集。

    两张表手抄一遍就是等着漂移：safety 后来补的 `systemctl is-allowed` 在 plan 的
    副本里就没有。子集方向可以成立（plan 刻意剔除 apt 家族 —— `apt update` 改包索引），
    反向不行。
    """
    for verb, subs in plan_mode.PLAN_VERB_SUBCOMMANDS.items():
        assert verb in safety._SAFE_VERB_SUBCOMMANDS, f"plan 独有动词 {verb}，safety 表里没有"
        assert subs <= safety._SAFE_VERB_SUBCOMMANDS[verb], (
            f"{verb} 的计划白名单超出了 safety 白名单："
            f"{sorted(subs - safety._SAFE_VERB_SUBCOMMANDS[verb])}")
    # 刻意剔除的那一族必须留在注释里说明，不能在代码里悄悄回来
    assert not ({"apt", "apt-get", "dpkg"} & set(plan_mode.PLAN_VERB_SUBCOMMANDS))


def test_plan_mode_does_not_redeclare_dangerous_subactions():
    """副本表已经删掉：它既与 safety 漂移，又比错 token（见上面的深动词用例）。"""
    assert not hasattr(plan_mode, "_DANGEROUS_SUBACTIONS")


# ── 7. reason 文案不能静默退化成兜底 ─────────────────────────────────────────

@pytest.mark.parametrize("cmd", ["git branch -D feature-x", "git remote set-url origin https://e/x"])
def test_new_reason_hits_consequence_table(cmd):
    reason = classify_command(cmd)[1]
    res = explain_for_reason(reason)
    assert res.source == "static", f"新 reason 未登记后果表（卡片会退化成通用文案）：{reason}"
    assert "git" in res.text
    assert git_form_writes(cmd.split()) is not None
