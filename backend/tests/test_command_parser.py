"""命令解析器：归一化迁移 + 引号感知分段 + wrapper / 执行位置工具。"""
import pytest

from agent.harness import command_parser as cp
from agent.harness import safety


def _raws(command: str) -> list[str]:
    return [seg.raw for seg in cp.split_command_chain(command)]


def test_归一化与展示函数已从_safety_迁出且仍可回导():
    cmd = r"rm$'\x2f' -rf $'\x2f'"
    assert cp.normalize_command_for_detection(cmd) == "rm/ -rf /"
    assert safety.normalize_command_for_detection(cmd) == "rm/ -rf /"
    assert cp.get_normalized_for_display("rm   -rf   /tmp/x ") == "rm -rf /tmp/x"
    assert safety.get_normalized_for_display is cp.get_normalized_for_display


def test_引号内的分隔符不切段():
    assert _raws("echo 'a;b' && rm -rf /tmp/x") == ["echo 'a;b'", "rm -rf /tmp/x"]
    assert _raws('printf "%s|%s" a b | cat') == ['printf "%s|%s" a b', "cat"]


def test_子shell内部的分隔符不切外层段():
    segs = cp.split_command_chain("echo $(date; id) && uptime")
    assert [s.raw for s in segs] == ["echo $(date; id)", "uptime"]


def test_fd复制与重定向保护():
    assert _raws("ps aux 2>&1 | grep nginx") == ["ps aux 2>&1", "grep nginx"]
    assert _raws("cmd &> /tmp/out") == ["cmd &> /tmp/out"]


def test_detection_剥引号但raw保留原文():
    seg = cp.split_command_chain('rm -rf "/"')[0]
    assert seg.raw == 'rm -rf "/"'
    assert seg.detection == "rm -rf /"


@pytest.mark.parametrize("cmd", ["echo 'unclosed", "echo $(date", "echo )"])
def test_未闭合引号或括号抛解析错误(cmd: str):
    with pytest.raises(cp.CommandParseError):
        cp.split_command_chain(cmd)


def test_wrapper与环境赋值剥离():
    assert cp.strip_prefix_wrappers(
        ["env", "FOO=1", "nice", "-n", "5", "jq", "."]
    ) == ["jq", "."]
    assert cp.strip_prefix_wrappers(["sudo", "id"]) == ["id"]


def test_动词与执行位置():
    assert cp.verb_of("/usr/bin/rm/") == "rm"
    assert cp.first_exec_verb(["then", "reboot", "-f"]) == ("reboot", ["-f"])
    assert cp.tokenize_segment("echo 'a b'") == ["echo", "a b"]


def test_posix_转义单引号不被归一化拆毁():
    """"it'\\''s" 是 awk/sed 里最常见的 POSIX 写法：反斜杠必须先于引号走位存活，
    否则归一化把 `\\'` 折成 `'` 后引号配平被打乱，整条只读命令 fail-closed。"""
    cmd = "awk '/it'" + chr(92) + "''s/' f"
    assert cp.normalize_command_for_detection(cmd) == cmd
    assert [s.raw for s in cp.split_command_chain(cmd)] == [cmd]
    assert cp.get_normalized_for_display(cmd) == "awk /it's/ f"


def test_转义引号造成的未闭合形态仍然抛错():
    bad = "echo 'a'" + chr(92) + "'b'"
    with pytest.raises(cp.CommandParseError):
        cp.split_command_chain(bad)


def test_空串剥除仍防住引号拼接混淆():
    assert cp.normalize_command_for_detection("r''m -rf /") == "rm -rf /"
    assert cp.normalize_command_for_detection('e""cho hi') == "echo hi"
