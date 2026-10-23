"""相对形态逃逸（`../`）与 cwd 内相对写法的双向锁。

形状：所有路径判据都要求目标以 `/` 开头，而远端 exec 的 cwd 默认就是 HOME。
修复前 `rm -rf ../home` 判 safe、`rm -rf /home` 判 hardline —— 同一次删除换个写法
就完全不走审批；`cat .ssh/authorized_keys` 同理（`_normalize_target` 不落地，
`_is_ssh_path` 的前缀匹配走空）。

修复只落地"确实向上逃逸出 cwd"的形态（`..` / `../...`），因此两个方向都必须钉：
  正向：逃逸形态与它的绝对写法同一答案
  反向：留在 cwd 内的相对写法（`.`、`build`、`./node_modules`）不得被当成绝对路径
        —— 递归删除那组规则判的是"目标是否绝对路径"，全局落地会把"清理本目录
        日志"读成"从根往下删"（正向锁的反面，见 test_safety_misjudgment）

运行: python -m pytest backend/tests/test_safety_relative_escape.py -v
"""
import pytest

from agent.harness.safety import (
    _normalize_target,
    classify_command,
    classify_constructed_command,
)

_LEVEL = {"safe": 0, "dangerous": 1, "hardline": 2}


def _ge(cmd: str, want: str) -> None:
    level, reason = classify_command(cmd)
    assert _LEVEL[level] >= _LEVEL[want], f"{cmd!r} 期望 ≥{want}，实得 {level}（{reason}）"
    if level != "safe":
        assert reason, f"{cmd!r} 非 safe 必须带 reason"


# ──────────────────────────────────────────────────────────────────────────────
# 一、_normalize_target 的落地边界
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("../home", "/home"),
    ("../../etc/passwd", "/etc/passwd"),
    ("..", "/"),
    (".././../var/log", "/var/log"),
    ("/home", "/home"),
    ("build", "build"),
    ("./dist", "dist"),
    (".", "."),
    ("node_modules", "node_modules"),
    ("~", "~"),
    ("$HOME/.ssh/id_rsa", "$HOME/.ssh/id_rsa"),
])
def test_归一只落地向上逃逸的形态(raw: str, expect: str) -> None:
    assert _normalize_target(raw) == expect


def test_落地只发生在确实逃逸时() -> None:
    # 反向锁：cwd 内的相对形态必须保持相对，否则递归删除规则把它读成"从根往下删"
    for benign in ("build", "./dist", ".", "logs/*.log", "node_modules/.cache"):
        assert not _normalize_target(benign).startswith("/")


# ──────────────────────────────────────────────────────────────────────────────
# 二、逃逸形态与绝对写法同一答案（正向）
# ──────────────────────────────────────────────────────────────────────────────
ESCAPING_PAIRS = [
    ("rm -rf ../home", "rm -rf /home", "hardline"),
    ("rm -rf ../etc", "rm -rf /etc", "hardline"),
    ("rm -rf ../var/log", "rm -rf /var/log", "hardline"),
    ("rm -rf ../../etc/passwd", "rm -rf /etc/passwd", "hardline"),
]


@pytest.mark.parametrize("rel,absolute,want", ESCAPING_PAIRS)
def test_向上逃逸的递归删除按绝对形态定级(rel: str, absolute: str, want: str) -> None:
    _ge(rel, want)
    assert classify_command(rel)[0] == classify_command(absolute)[0]


def test_逃逸到上级目录的任何绝对路径形态都至少需审批() -> None:
    # `rm -rf ../tmp/build` 落地成 /tmp/build：不是敏感目录，但已是"绝对路径递归删除"
    _ge("rm -rf ../tmp/build", "dangerous")
    _ge("rm -rf ..", "hardline")


@pytest.mark.parametrize("cmd,want", [
    ("cat .ssh/authorized_keys", "dangerous"),
    ("cat .ssh/id_rsa", "dangerous"),
    ("cat ../etc/shadow", "dangerous"),
    ("cp .ssh/id_rsa /tmp/x", "dangerous"),
    ("cat ./config/.env", "dangerous"),
    ("echo key >> .ssh/authorized_keys", "hardline"),
    ("tee .ssh/id_rsa", "hardline"),
    ("echo x > ../etc/cron.d/evil", "dangerous"),
    ("tar czf backup.tgz ../etc", "dangerous"),
])
def test_凭据与持久化判据吃相对形态(cmd: str, want: str) -> None:
    _ge(cmd, want)
    # 写侧/读侧与模型口径共用一条入口：constructed 口径不得给出更松的答案
    assert _LEVEL[classify_constructed_command(cmd)[0]] >= _LEVEL[want]


def test_相对与绝对凭据读取同一答案() -> None:
    rel, _ = classify_command("cat .ssh/id_rsa")
    absolute, _ = classify_command("cat /home/x/.ssh/id_rsa")
    assert rel == absolute


# ──────────────────────────────────────────────────────────────────────────────
# 三、cwd 内的良性相对清理不得被误拦（反向）
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf build dist node_modules",
    "rm -rf ./dist",
    "rm -rf .",
    "find . -name '*.log' -mtime +7",
    "rm -f ./tmp.out",
])
def test_目录内清理保持安全(cmd: str) -> None:
    assert classify_command(cmd) == ("safe", None), f"{cmd!r} 被误拦：{classify_command(cmd)}"


def test_目录内_find_delete_升级为审批() -> None:
    """find -delete 是删除动作，不再因为目标在 cwd 内就免审。"""
    level, reason = classify_command("find . -name '*.log' -mtime +7 -delete")
    assert level == "dangerous"
    assert "destructive" in (reason or "")
