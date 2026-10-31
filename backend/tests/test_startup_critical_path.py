"""后端启动关键路径守卫。

这些用例锁的不是某个函数行为，而是三条「启动成本别悄悄涨回去」的不变量：

1. `import main` 不得拉起 Agent 执行栈（harness/driver/reflection/evolution/tools）
   与 httpx —— 它们只在真正发起 run、或用到 webhook 时才需要（实测曾占启动
   初始化 130ms+，并让 routes.agent 的惰性导入白做）。同一条不变量的第三方侧：
   openai / botpy / cryptography 也不许在导入期出现（实测 openai 477ms、
   botpy 271ms，两个就超过后端从起来到就绪的全部 710ms）。
2. `agent.harness` 的公开导出保持惰性（PEP 562），但 `from agent.harness import
   Harness` 这种写法必须仍然可用 —— 否则惰性改造就变成了破坏封装。
3. `init_db` 的结构版本短路必须与 DDL/迁移同步：改了建表或迁移却不升
   `_SCHEMA_VERSION`，老库会永远停在旧结构且不再迁移（比不优化更糟）。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# 允许在 import 期加载的执行栈模块（出现即回归）
_FORBIDDEN_AT_IMPORT = (
    "agent.harness.harness",
    "agent.harness.driver",
    "agent.reflection",
    "agent.evolution",
    "agent.tools",
    "httpx",
)

# 重型第三方库：本机实测 `import openai` 477ms、`import botpy` 271ms、
# `import cryptography` 15ms —— 前两者相加（748ms）已经超过后端从进程起来到
# 健康就绪的全部耗时（710ms，其中导入段 543ms）。它们今天全靠惰性取用躲在这条
# 路径之外：openai 走 llm_adapter 的 `_openai()` 惰性导入，botpy 只在真的恢复
# QQ 渠道时才要（且那一步在就绪之后）。任何人把它们写回模块级 import，冷启动
# 就地 +70%，而单元测试与 typecheck 都不会因此变红 —— 所以只能在这里钉住。
_FORBIDDEN_AT_IMPORT_THIRDPARTY = (
    "openai",
    "botpy",
    "cryptography",
)

# init_db / 迁移函数体的指纹。改动这些函数的任何一行，都要在
# 「升级 _SCHEMA_VERSION」与「更新本常量」之间做一次显式决定。
_SCHEMA_SOURCE_FINGERPRINT = "78a959dd0e14e3b1"


def _run_probe(code: str) -> dict:
    """在独立解释器里跑探针：导入图只能在没有 pytest 自身污染时断言。"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, f"探针进程退出码 {proc.returncode}\nstderr:\n{proc.stderr[-3000:]}"
    # 后端日志走 stderr；stdout 只留探针自己的 JSON
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_import_main_keeps_execution_stack_off_critical_path():
    """`import main`（= 后端启动的全部导入）不加载执行栈与 httpx。"""
    result = _run_probe(
        f"""
import json, sys
import main  # noqa: F401
import agent.harness.reins.gate  # noqa: F401  （lifespan 的审批策略回填会拉到这一层）
forbidden = {list(_FORBIDDEN_AT_IMPORT)!r}
print(json.dumps({{"loaded": sorted(m for m in forbidden if m in sys.modules)}}))
"""
    )
    assert result["loaded"] == [], (
        "后端启动又把这些模块拉回来了（启动成本会因此上涨）："
        f"{result['loaded']}"
    )


def test_import_main_keeps_heavy_third_party_off_critical_path():
    """`import main` 不得拉起 openai / botpy / cryptography。

    与上一条同一条不变量的第三方侧：这几个包的导入成本量级和整个后端启动相当
    （见 _FORBIDDEN_AT_IMPORT_THIRDPARTY 上的实测数），一旦被写回模块级 import，
    冷启动会成倍变贵，而现有单测与 typecheck 全都察觉不到。
    """
    result = _run_probe(
        f"""
import json, sys
import main  # noqa: F401
import agent.harness.reins.gate  # noqa: F401  （lifespan 的审批策略回填会拉到这一层）
forbidden = {list(_FORBIDDEN_AT_IMPORT_THIRDPARTY)!r}
print(json.dumps({{"loaded": sorted(m for m in forbidden if m in sys.modules)}}))
"""
    )
    assert result["loaded"] == [], (
        "后端启动导入期拉起了重型第三方库，冷启动成本会显著上涨："
        f"{result['loaded']}（把它们改回惰性取用，或在提交说明里写清为什么必须提前）"
    )


def test_harness_package_public_api_still_resolves():
    """惰性导出不改变公开用法：`from agent.harness import Harness` 仍然可用。"""
    result = _run_probe(
        """
import json, sys
import agent.harness.reins.gate  # noqa: F401  先只导入子模块
lazy_before = "agent.harness.harness" in sys.modules
from agent.harness import Harness, HarnessConfig  # 公开 API 写法必须保持不变
print(json.dumps({
    "lazy_before": lazy_before,
    "names": [Harness.__name__, HarnessConfig.__name__],
    "loaded_after": "agent.harness.harness" in sys.modules,
}))
"""
    )
    assert result["lazy_before"] is False, "只导入 reins 子模块不应拉起 harness 本体"
    assert result["names"] == ["Harness", "HarnessConfig"]
    assert result["loaded_after"] is True, "公开导出必须真的能取到 Harness"


def test_uvicorn_config_receives_app_object():
    """入口必须传 app 对象：传 "main:app" 字符串会让 uvicorn 再 import 一次本模块。"""
    text = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert re.search(r"uvicorn\.Config\(\s*(?:#[^\n]*\n\s*)*app,", text), (
        "uvicorn.Config 的第一参数不是 app 对象 —— 字符串入口会双重执行 main.py"
    )
    for lineno, line in enumerate(text.splitlines(), 1):
        if "main:app" in line and not line.lstrip().startswith("#"):
            raise AssertionError(f"main.py:{lineno} 仍以字符串形式指定入口: {line.strip()}")


def test_init_db_marks_schema_version(base_db):
    """全量初始化后必须把结构版本落到库里，否则下次启动仍会重跑 DDL。"""
    import database as db

    conn = db._get_conn()
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    assert version == db._SCHEMA_VERSION
    for table in db._SCHEMA_CORE_TABLES:
        hit = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert hit is not None, f"核心表缺失: {table}"


def test_init_db_skips_ddl_when_schema_is_current(base_db, monkeypatch, caplog):
    """结构已是最新时只跳过建表脚本；迁移与数据修复必须照常执行。

    这是本优化的安全边界：重跑建表脚本是固定成本（~110ms），而迁移修的是
    user_version 表达不了的缺损（表在但列被删、老数据写反）—— 一并短路会让
    这类库失去自愈能力（test_migrate_survives_db_without_name_column 覆盖）。
    """
    import logging

    import database as db

    migrated: list[int] = []
    monkeypatch.setattr(db, "_migrate_db", lambda conn: migrated.append(1))
    with caplog.at_level(logging.INFO, logger="cKunAgentic.database"):
        db.init_db(base_db)
    assert "跳过建表脚本" in caplog.text, "结构版本已命中却仍执行了建表脚本"
    assert migrated == [1], "迁移被一并短路了：表在但列被删的库将无法自愈"


def test_init_db_rebuilds_when_version_is_stale(tmp_workspace):
    """版本号被清零（老库/外部工具）时必须回退全量建表，不能永久短路。"""
    import os

    import database as db

    db_path = os.path.join(tmp_workspace, "stale.db")
    db._close_all_connections()
    db.init_db(db_path)
    try:
        conn = db._get_conn()
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        assert db._schema_is_current(conn) is False

        db.init_db(db_path)  # 回退全量建表/迁移
        assert db._schema_is_current(db._get_conn()) is True
    finally:
        db._close_all_connections()


def test_schema_version_bumped_with_ddl_changes():
    """改了 init_db 的 DDL / 迁移就必须升 _SCHEMA_VERSION（否则老库永不迁移）。"""
    import hashlib
    import inspect

    import database as db

    parts = [
        inspect.getsource(f)
        for f in (
            db.init_db,
            db._migrate_db,
            db._migrate_bot_channels,
            db._migrate_memory_v1_to_v2,
        )
    ]
    # 建表脚本只存在于 init_db 源码里，上面已覆盖；这里额外把 DDL 文本本身
    # 纳进来（executescript 的字符串字面量），避免「只改 SQL 不改函数结构」漏网。
    src = (BACKEND / "database.py").read_text(encoding="utf-8")
    start = src.index('_maybe_ddl("""')
    end = src.index('""")', start)
    parts.append(src[start:end])
    blob = re.sub(r"\s+", " ", "\n".join(parts))
    fingerprint = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    assert fingerprint == _SCHEMA_SOURCE_FINGERPRINT, (
        "init_db/迁移代码变了。若这是结构变更：先把 database._SCHEMA_VERSION += 1，"
        f"再更新本文件的 _SCHEMA_SOURCE_FINGERPRINT = {fingerprint!r}；"
        "若只是无害重构（不动 SQL 语义）：同样更新该常量即可。"
    )
