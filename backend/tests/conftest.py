import shutil
import sys
from pathlib import Path

import pytest

# 让测试可以直接 import backend/ 下的模块（safety.py 等）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def _sweep_tmp_workspace():
    """每轮测试前后把 _tmp/ 整个清干净（沙箱禁止系统 tmp，测试产物只能落在 _tmp/）。

    只靠 tmp_workspace 的逐目录 rmtree 会留孤儿：database 的连接池是
    keep-alive（close() = rollback 后保留底层句柄），进程退出前 _tmp 里的
    .db/.db-shm/.db-wal 一直开着，Windows 下 rmtree(ignore_errors=True)
    静默失败，目录就永久残留。这里每轮测试前后整目录清扫：上一轮进程
    退出后句柄已释放，可全部删掉；仍被占用的（如挂死进程持锁）由
    ignore_errors 兜底跳过，不阻塞测试。
    """
    base = Path(__file__).resolve().parent / "_tmp"

    def sweep() -> None:
        if not base.is_dir():
            return
        for child in base.iterdir():
            shutil.rmtree(child, ignore_errors=True)

    sweep()
    yield
    sweep()


@pytest.fixture
def tmp_workspace():
    """工作区内的临时目录（沙箱禁止系统 tmp，pytest 默认 tmp_path 不可用）

    注意：不能用 tempfile.mkdtemp — Windows 下它给目录打限制性 ACL，
    沙箱进程随后读写该目录会被拒绝。普通 os.mkdir 继承默认 ACL，可用。
    """
    import uuid
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    d = base / f"wstest-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield str(d)
    shutil.rmtree(d, ignore_errors=True)
