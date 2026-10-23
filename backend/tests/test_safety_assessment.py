"""CommandAssessment：分级之外的 source / rule_id 契约，以及本轮新增判定。"""
from agent.harness.safety import classify_command, classify_command_detailed


def _a(cmd: str, **kw):
    return classify_command_detailed(cmd, **kw)


def test_未知命令仍走默认审批且_source_诚实():
    a = _a("mycohortool --deploy")
    assert (a.level, a.source, a.rule_id) == ("dangerous", "default_approval", "")
    assert a.reason == "unknown command, default approval (mycohortool)"


def test_只读规则命中_readonly_rule_并带_rule_id():
    a = _a("jq '.a' app.json")
    assert (a.level, a.source, a.rule_id) == ("safe", "readonly_rule", "ro.verb.jq.v1")
    assert a.reason is None


def test_既有白名单保持_legacy_safe_verb_且不冒领_rule_id():
    for cmd in ("ls -la /tmp", "date +%s", "find /tmp -name '*.log'", "tar -czf /tmp/x.tgz /tmp/data"):
        a = _a(cmd)
        assert a.level == "safe", cmd
        assert a.source == "legacy_safe_verb", cmd
        assert a.rule_id == "", cmd


def test_getent_passwd_不再被密码规则误伤():
    a = _a("getent passwd")
    assert (a.level, a.source, a.rule_id) == ("safe", "readonly_rule", "ro.sub.getent.v1")


def test_passwd_仍按执行位置判密码修改():
    a = _a("passwd root")
    assert a.level == "dangerous"
    assert a.reason == "password change (passwd)"
    assert a.source == "exec_position"


def test_date_set_与_find_delete_进入破坏性写守卫():
    a = _a("date -s '2020-01-01'")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "destructive_write", "rw.destructive.date_set.v1")
    a = _a("find . -name '*.log' -mtime +7 -delete")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "destructive_write", "rw.destructive.find_action.v1")


def test_tar_解压危险_列表走只读证明_创建保持旧口径():
    a = _a("tar -xzf app.tgz -C /srv")
    assert (a.level, a.rule_id) == ("dangerous", "rw.destructive.tar_write.v1")
    a = _a("tar -tzf app.tgz")
    assert (a.level, a.source, a.rule_id) == ("safe", "readonly_rule", "ro.flag.tar_list.v1")
    a = _a("tar -czf /tmp/x.tgz /tmp/data")
    assert (a.level, a.source) == ("safe", "legacy_safe_verb")


def test_git_执行注入与_awk_system_在旧白名单之前拦截():
    a = _a("git -c core.pager=sh status")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "exec_injection", "ex.inject.git_config_env.v1")
    a = _a("awk '{system(\"id\")}' /tmp/x")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "exec_injection", "ex.inject.awk_system.v1")


def test_托管日志目录_find_delete_豁免仍为_safe():
    cmd = "find /home/admin-pl/.trae-cn-server/manager-logs -name '*.log' -mtime +7 -delete"
    a = _a(cmd)
    assert (a.level, a.source) == ("safe", "agent_log_exemption")


def test_构造模板口径不套默认审批_但结构守卫仍生效():
    a = _a("mycohortool --deploy", apply_default_approval=False)
    assert (a.level, a.source) == ("safe", "constructed_no_default")
    a = _a("tar -xzf app.tgz", apply_default_approval=False)
    assert (a.level, a.source) == ("dangerous", "destructive_write")


def test_未闭合引号整串_fail_closed():
    a = _a("echo 'unclosed")
    assert a.level == "dangerous"
    assert a.source == "unparseable"


def test_classify_command_仍返回二元组():
    assert classify_command("jq '.a' app.json") == ("safe", None)
    assert classify_command("date -s '2020-01-01'")[0] == "dangerous"


def test_passwd_路径形态区分调用与数据文件():
    """`/etc/passwd` 当首 token 是被读取的文件，成因不该写成"修改账户口令"；
    `/usr/bin/passwd root` 是真实改口令调用，必须继续拿 exec_position。"""
    a = _a("/etc/passwd")
    assert (a.level, a.source) == ("dangerous", "secret_read")
    assert a.reason == "read of credential file (/etc/passwd)"
    for cmd in ("passwd root", "/usr/bin/passwd root", "/sbin/passwd"):
        b = _a(cmd)
        assert (b.level, b.source, b.reason) == (
            "dangerous", "exec_position", "password change (passwd)"), cmd


def test_posix_转义引号的只读命令不被误伤():
    assert classify_command("awk '/it'" + chr(92) + "''s/' f") == ("safe", None)
    # 对照：转义引号造成的未闭合形态仍 fail-closed
    assert _a("echo 'a'" + chr(92) + "'b'").level == "dangerous"


def test_裸模式解压不再被旧白名单放行():
    """`tar xzf app.tgz` / `tar x app.tar` 不带 - 也是解包写盘：评审实测它们曾
    以 source=legacy_safe_verb 判 safe，不开卡。"""
    for cmd in ("tar xzf app.tgz", "tar x app.tar", "tar xzf app.tgz -C /srv",
                "tar u -f a.tar f"):
        a = _a(cmd)
        assert (a.level, a.source) == ("dangerous", "destructive_write"), (cmd, a.level, a.source)
        assert a.rule_id == "rw.destructive.tar_write.v1", cmd
