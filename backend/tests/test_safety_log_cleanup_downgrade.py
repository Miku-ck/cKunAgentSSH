"""`/var/log` 应用子目录的递归删除：hardline → dangerous 的边界锁。

背景：`/var` 在 _SENSITIVE_DIRS 里，`rm -rf /var/log/app` 被一刀切判 hardline，
既没有审批出口也无法通过 allowlist 放行 —— 「清理应用日志」这个最典型的合法
运维任务在 prod 被彻底堵死。

降级只针对**明确的应用日志子目录字面路径**：
  正向：/var/log/<app>（含更深层级）→ dangerous，仍必须审批/预登记
  反向：/var/log 本体、系统/审计日志、通配符、尾斜杠、`..` 一律保持 hardline

运行: python -m pytest backend/tests/test_safety_log_cleanup_downgrade.py -v
"""
import pytest

from agent.harness.safety import classify_command

_LEVEL = {"safe": 0, "dangerous": 1, "hardline": 2}


def _exact(cmd: str, want: str) -> None:
    level, reason = classify_command(cmd)
    assert level == want, f"{cmd!r} 期望 {want}，实得 {level}（{reason}）"


def _ge(cmd: str, want: str) -> None:
    level, reason = classify_command(cmd)
    assert _LEVEL[level] >= _LEVEL[want], f"{cmd!r} 期望 ≥{want}，实得 {level}（{reason}）"


# ── 正向：应用日志子目录降级为 dangerous ────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf /var/log/app",
    "rm -rf /var/log/app/sub",
    "rm -rf /var/log/nginx",
    "rm -r -f /var/log/app",
])
def test_literal_app_log_subdir_is_dangerous(cmd: str) -> None:
    _exact(cmd, "dangerous")


def test_downgrade_reason_is_specific() -> None:
    _, reason = classify_command("rm -rf /var/log/app")
    assert reason
    assert "log directory" in reason
    assert "/var/log/app" in reason


def test_downgraded_delete_still_needs_approval_not_free() -> None:
    # 降级不是放行：dangerous 必须走审批 / 预登记，不能变成 safe
    level, _ = classify_command("rm -rf /var/log/app")
    assert level == "dangerous"


# ── 反向：系统/审计日志与危险形态保持 hardline ──────────────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf /var/log",
    "rm -rf /var/log/",
    "rm -rf /var/log/audit",
    "rm -rf /var/log/journal",
    "rm -rf /var/log/private",
    "rm -rf /var/log/sssd",
    "rm -rf /var/log/auth.log",
    "rm -rf /var/log/secure",
    "rm -rf /var/log/syslog",
    "rm -rf /var/log/messages",
    "rm -rf /var/log/kern.log",
    "rm -rf /var/log/daemon.log",
    "rm -rf /var/log/user.log",
    "rm -rf /var/log/cron",
    "rm -rf /var/log/cron.log",
    "rm -rf /var/log/wtmp",
    "rm -rf /var/log/btmp",
    "rm -rf /var/log/lastlog",
    "rm -rf /var/log/faillog",
])
def test_protected_log_targets_stay_hardline(cmd: str) -> None:
    _exact(cmd, "hardline")


@pytest.mark.parametrize("cmd", [
    "rm -rf /var/log/app/*",
    "rm -rf /var/log/app/.*",
    "rm -rf /var/log/app/",
    "rm -rf /var/log/app/../audit",
    "rm -rf /var/log/../etc",
    "rm -rf /var/log//app",
    "rm -rf /var/log/app?",
    "rm -rf /var/log/$APP",
    "rm -rf /var/log/$(cat /tmp/x)",
])
def test_glob_escape_and_expansion_stay_hardline(cmd: str) -> None:
    _ge(cmd, "hardline")


def test_chain_with_other_hardline_stays_hardline() -> None:
    _exact("rm -rf /var/log/app; rm -rf /etc", "hardline")
    _exact("rm -rf /var/log/app && rm -rf /var/log/audit", "hardline")


# ── 相邻路径不受影响 ────────────────────────────────────────────────────────
def test_other_var_paths_unchanged() -> None:
    _exact("rm -rf /var", "hardline")
    _exact("rm -rf /var/lib", "hardline")
    _exact("rm -rf /var/www/app", "hardline")


def test_tmp_cleanup_still_needs_approval() -> None:
    _exact("rm -rf /tmp/app", "dangerous")
