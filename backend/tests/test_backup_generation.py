# -*- coding: utf-8 -*-
"""C-review M13 回归：同 run 同文件二次写不得覆盖第一次磁盘备份。

修复前：_backup_file 用 copy2 写同名目标 — 二次写把第一次的备份（run 前原始）
覆盖为中间态，undo 恢复到中间态而非原始内容。
修复后：备份名带代次序号（g1/g2/...），g1 = 第一次写前内容 = run 前原始；
undo 路由 _scan_run_backups 取最小代次；file_changes 合并保留首次 operation。

运行: python -m pytest backend/tests/test_backup_generation.py -v
"""
import os
from pathlib import Path

import pytest

from agent.harness import saddle


@pytest.fixture()
def backup_dir(tmp_workspace, monkeypatch):
    d = Path(tmp_workspace) / "bk"
    d.mkdir()
    monkeypatch.setenv("CKUN_AGENT_BACKUP_DIR", str(d))
    return d


def test_two_writes_keep_first_generation(backup_dir):
    """第一次写备份原始（g1），第二次写备份中间态（g2），g1 不被覆盖"""
    f = backup_dir / "target.txt"
    f.write_text("ORIGINAL", encoding="utf-8")
    b1 = saddle._backup_file(str(f), "run1")
    assert b1, "第一次写应生成备份"
    assert "_g1" in os.path.basename(b1)

    f.write_text("INTERMEDIATE", encoding="utf-8")
    b2 = saddle._backup_file(str(f), "run1")
    assert b2, "第二次写应生成备份"
    assert b2 != b1
    assert "_g2" in os.path.basename(b2)

    with open(b1, "r", encoding="utf-8") as fh:
        assert fh.read() == "ORIGINAL", "g1 必须是 run 前原始内容"
    with open(b2, "r", encoding="utf-8") as fh:
        assert fh.read() == "INTERMEDIATE"


def test_scan_run_backups_picks_first_generation(backup_dir):
    """undo 路由的 _scan_run_backups 必须取第一代（run 前原始）"""
    from routes import agent as agent_routes
    f = backup_dir / "cfg.conf"
    f.write_text("V0", encoding="utf-8")
    saddle._backup_file(str(f), "run2")
    f.write_text("V1", encoding="utf-8")
    saddle._backup_file(str(f), "run2")
    f.write_text("V2", encoding="utf-8")
    saddle._backup_file(str(f), "run2")

    mapping = agent_routes._scan_run_backups("run2")
    assert str(f) in mapping, "应扫到该路径的备份"
    with open(mapping[str(f)], "r", encoding="utf-8") as fh:
        assert fh.read() == "V0", "undo 应恢复到 run 前原始，而非中间态"


def test_create_then_modify_keeps_create_operation(tmp_workspace):
    """create→modify 合并保留 operation=create（undo 才会正确删除文件）"""
    from agent import file_changes
    run_id = "m13-create-modify"
    ws = Path(tmp_workspace)
    try:
        f = ws / "new.txt"
        file_changes.record_file_write(run_id, str(f), str(ws), "hello")
        f.write_text("hello", encoding="utf-8")  # 模拟工具实际写入
        file_changes.record_file_write(run_id, str(f), str(ws), "hello v2")
        f.write_text("hello v2", encoding="utf-8")

        changes = file_changes.get_file_changes(run_id)
        assert len(changes) == 1, "同 run 同文件应合并为一条记录"
        assert changes[0]["operation"] == "create", (
            f"create→modify 合并后 operation 应保持 create，实际: {changes[0]['operation']}"
        )
    finally:
        file_changes.clear_file_changes(run_id)
