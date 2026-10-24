"""内建技能状态文件的原子性与"读不出就别清"。

_STATE_FILE 记录"此前由本模块管理过哪些 slug"，是升级后孤儿清理的唯一基准。
原实现两处都会把这个基准弄丢：

1. `write_text` 直写目标文件 —— 进程被杀在写中间就留下半个 JSON；
2. 读取侧 `except Exception: return set()` 把"文件不存在"（首次运行，空集是对的）
   与"文件读不出"（半写损坏 / 权限 / 结构异常）合并处理 —— 于是下一次同步认为
   "此前什么都没管过"，基准就此丢失且不会再自愈。

现在：写入走 tmp + os.replace（tmp 名带 pid，因为 dev 实例与已安装实例共用
~/.ckunagentic），读取侧把"不知道"用 None 诚实传出去，调用方据此跳过清理。
"""

import json
import sys
import types

import pytest

# 必须在任何测试把假的 `database` 塞进 sys.modules 之前把 skill_registry 拉起来：
# 它模块顶层就是 `import database as db`，第一次导入发生在冒牌库还在位的时候，
# 那个 SingleNamespace 就会被永久绑进注册表（sys.modules 里的 skill_registry 只此
# 一份）。builtin_skills 同步到至少一条技能时会 `from skill_registry import ...`，
# 于是后面任何按真库断言的用例（test_ai_loop_core_regression 的禁用清单两条）都在跟一个
# 陈旧冒牌库比，报"'types.SimpleNamespace' object has no attribute
# 'list_disabled_skills_for_mode'"。这里先导入，让它绑定到真库。
import skill_registry  # noqa: F401

import builtin_skills


@pytest.fixture(autouse=True)
def _restore_managed_slugs():
    """`_managed_slugs` 是模块级集合，用例就地 clear/update 会把它留给后面的用例。

    routes/skills.py 读它当孤儿清理的豁免名单，泄漏出去的那一份会让后续用例
    看到"豁免了 s1"这种根本没发生过的事。
    """
    saved = set(builtin_skills._managed_slugs)
    yield
    builtin_skills._managed_slugs.clear()
    builtin_skills._managed_slugs.update(saved)


@pytest.fixture
def state_file(tmp_workspace, monkeypatch):
    from pathlib import Path

    path = Path(tmp_workspace) / "builtin_skills_state.json"
    monkeypatch.setattr(builtin_skills, "_STATE_FILE", path)
    return path


@pytest.fixture
def fake_db(monkeypatch):
    """冒牌 database：本文件的用例只关心清理判据与回写时机，不许碰任何真库。

    `_db_path` 平时是空串，`sqlite3.connect("")` 给的是一个匿名临时库，写进去的
    东西看不见也删不掉 —— 一旦同批有别的用例先 init_db 过，这里就会把测试技能
    落到真正的用户库里。槽位一律显式打桩，不靠运气。
    """
    deleted: list[str] = []
    db = types.SimpleNamespace(
        upsert_builtin_skill=lambda **_kw: None,
        list_builtin_skill_slugs=lambda: ["gone-in-new-version"],
        delete_builtin_skill_by_slug=lambda slug: deleted.append(slug),
    )
    monkeypatch.setitem(sys.modules, "database", db)
    return deleted


@pytest.fixture
def one_packaged_skill(monkeypatch):
    """只测清理分支：技能集合用一条桩数据，避免依赖真实目录扫出来的条数。"""
    monkeypatch.setattr(
        builtin_skills, "scan_builtin_skills",
        lambda: [{"name": "n", "slug": "s1", "system_prompt": "x" * 300,
                  "description": "", "category": "", "trigger_words": []}],
    )


def test_missing_state_is_first_run_not_untrusted(state_file):
    """文件不存在 = 首次运行，空集合是正确答案，不该禁用后续清理。"""
    assert builtin_skills._previous_managed_slugs() == set()


def test_corrupt_state_returns_none_and_keeps_evidence(state_file):
    state_file.write_text('{"slugs": ["a",', encoding="utf-8")  # 典型半写截断

    assert builtin_skills._previous_managed_slugs() is None

    baks = [p for p in state_file.parent.glob("*.corrupt-*.bak")]
    assert len(baks) == 1, "损坏原文必须留证，否则下次无从核对基准"
    assert '"a",' in baks[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("payload", ['["不是对象"]', '{"slugs": "不是列表"}', 'null'])
def test_unexpected_structure_is_untrusted(state_file, payload):
    state_file.write_text(payload, encoding="utf-8")
    assert builtin_skills._previous_managed_slugs() is None


def test_unreadable_file_is_untrusted_not_empty(tmp_workspace, monkeypatch):
    """读失败（权限/IO）也不许当成"此前没管过"。

    用一个目录占住路径来制造非 ENOENT 的 OSError：比给 pathlib.read_text 打桩轻，
    不会顺带影响同一轮里其他读文件的测试代码。
    """
    from pathlib import Path

    blocker = Path(tmp_workspace) / "blocked-state.json"
    blocker.mkdir(exist_ok=True)
    monkeypatch.setattr(builtin_skills, "_STATE_FILE", blocker)

    assert builtin_skills._previous_managed_slugs() is None


def test_persist_is_atomic_and_roundtrips(state_file):
    builtin_skills._managed_slugs.clear()
    builtin_skills._managed_slugs.update({"linux-ops-triage", "nginx-troubleshoot"})

    builtin_skills._persist_managed_slugs()

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["slugs"] == ["linux-ops-triage", "nginx-troubleshoot"]
    assert data["version"] == builtin_skills._STATE_VERSION
    assert not list(state_file.parent.glob("*.tmp")), "临时文件不得残留"

    # 覆盖写（os.replace 语义）：第二次不留旧内容碎片
    builtin_skills._managed_slugs.discard("nginx-troubleshoot")
    builtin_skills._persist_managed_slugs()
    assert builtin_skills._previous_managed_slugs() == {"linux-ops-triage"}


def test_cleanup_skipped_when_base_untrusted(state_file, fake_db, one_packaged_skill):
    """状态不可信时不得删任何东西 —— 清理的判据是"此前管过"，不知道就不动。"""
    state_file.write_text("{", encoding="utf-8")

    builtin_skills.sync_packaged_skills()

    assert fake_db == [], "基准未知时任何删除都是拿猜测当事实"


def test_untrusted_baseline_is_not_clobbered(state_file, fake_db, one_packaged_skill, monkeypatch):
    """基准未知时连回写都不许发生。

    上一条只钉住"不删"，但同一次同步里无条件 `_persist_managed_slugs()` 会把
    "当前还管着的这一批"当新基准写下去，正好抹掉唯一能说明"此前还管过别的"的记录：
    那些已从包内删掉的技能就此永远不会再被认成孤儿，而且下一次读盘只会"确认"覆写
    后的子集。这条把覆写本身钉住 —— 判据是"这次到底写没写盘"。
    """
    state_file.write_text('{"version": 1, "slugs": ["still-managed", "removed-in-new"]}',
                          encoding="utf-8")
    monkeypatch.setattr(builtin_skills, "_previous_managed_slugs", lambda: None)

    persisted: list[bool] = []
    monkeypatch.setattr(
        builtin_skills, "_persist_managed_slugs", lambda: persisted.append(True))

    builtin_skills.sync_packaged_skills()

    assert persisted == [], "基准未知却回写了状态文件，真实基准就此被子集覆写"
    assert '"removed-in-new"' in state_file.read_text(encoding="utf-8"), \
        "原基准必须原样留在盘上，等它被读出来那天仍能收敛"
