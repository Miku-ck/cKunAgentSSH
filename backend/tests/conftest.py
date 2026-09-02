import sys
import shutil
from pathlib import Path

import pytest

# 让测试可以直接 import backend/ 下的模块（safety.py 等）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
