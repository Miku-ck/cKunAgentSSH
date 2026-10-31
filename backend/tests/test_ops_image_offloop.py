"""本地图片读取必须跑在事件循环之外（形状守卫）。

同族的 ops_file 已经把每条本地腿都包进 asyncio.to_thread，并在 _local_read 的
docstring 里写明理由：这个循环同时载着所有在途 SSE 流与终端转发，一次冷盘/网络盘
上的 stat+read 能把它们整片卡住。ops_image 的本地分支是那次整改漏掉的一条腿
（ruff 的 ASYNC230 也单独点过它）。

行为面（能读出 data URI、超限拒绝、缺文件报错）已由 tests/test_ops_file_job_img.py
的 test_read_image_local_ok / _too_large / _bad_extension 覆盖，这里只钉「在哪个线程
上跑」这一件测不到又最容易回退的事 —— 要跑真行为就得测「别的协程有没有被卡住」，
那在 CI 上是计时噪声。最后一条是变异自检：证明这条断言真会红。
"""
import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "agent" / "tools" / "builtin" / "ops_image.py"

#: execute 主体的缩进层（类方法 = 8 空格）。用它区分「主体里的裸同步调用」和
#: 「交给线程的那个函数里的调用」——两者文本上都有 open()，只看字符串分不开。
_BODY_INDENT = 8


def _top_level(body: str) -> str:
    """只保留 execute 主体这一层的行（嵌套函数体在 12 格，不进这里）。"""
    return "\n".join(
        l for l in body.splitlines()
        if len(l) - len(l.lstrip()) == _BODY_INDENT
    )


def _execute_body() -> str:
    lines = SRC.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if re.search(r"\basync def execute\(", l))
    indent = len(lines[start]) - len(lines[start].lstrip())
    # execute 是文件最后一个方法时后面没有倒退行，段尾就是文件尾
    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i].strip() and (len(lines[i]) - len(lines[i].lstrip())) <= indent),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_asyncio_is_imported_at_module_level():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    names = {
        alias.name
        for n in tree.body
        if isinstance(n, ast.Import)
        for alias in n.names
    }
    assert "asyncio" in names


def test_local_leg_runs_through_to_thread_and_keeps_the_same_branches():
    body = _execute_body()
    assert "await asyncio.to_thread(" in body, "本地这条腿又回到事件循环上了"
    # 挪进线程的那一段必须还是原来那段同步实现：尺寸上限与错误分支被削弱或复制，
    # 这条守卫就只是把一段改坏了的实现换了个位置
    helper = SRC.read_text(encoding="utf-8")
    assert helper.count("os.path.getsize(path) > MAX_IMAGE_BYTES") == 1, "10MB 上限被复制成两处或删除"
    for required in ("PermissionError", "IsADirectoryError"):
        assert required in helper, f"本地读的 {required} 分支丢了"
    # 同步 stat/open/read/encode 必须整个待在被交给线程的那个函数里：execute 主体
    # 这一层再出现任何一次直接调用，就是回到事件循环上了
    top = _top_level(body[body.index("# 本地"):])
    assert "asyncio.to_thread(_read_local)" in top, "交给线程的不是那个同步函数本体"
    for banned in ("open(", "b64encode", "os.path.getsize", "os.path.exists"):
        assert banned not in top.replace("asyncio.to_thread(_read_local)", ""), \
            f"execute 主体这一层还剩裸的 {banned}"


def test_mutant_is_caught():
    """变异自检：把 to_thread 拆回直接调用，上面的判据确实会红。"""
    body = _execute_body()
    mutant = body.replace(
        "err, b64 = await asyncio.to_thread(_read_local)",
        "err, b64 = _read_local()",
    )
    assert mutant != body, "被替换的那个调用点根本不存在 = 这条自检是空的"
    assert "await asyncio.to_thread(" not in mutant
    top = _top_level(mutant[mutant.index("# 本地"):])
    assert "asyncio.to_thread(_read_local)" not in top
