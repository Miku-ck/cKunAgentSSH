"""S14-09：read_image 的超时只有一个来源（`meta.timeout`）。

原先 `meta` 里声明 `timeout=20.0`，调用点又抄一个裸 `20`。两处数字今天恰好相等，
但调档位时只会改到一处 —— 改 `meta` 的人以为限住了，实际发出去的还是那个 20。
同族写法是 `ops_file` 各工具一律传 `self.meta.timeout`。

用源码守卫而不是行为断言：这条要钉的是"只有一个来源"这件事本身，而不是 20 这个数字。
"""
import inspect

from agent.tools.builtin import ops_image


def test_read_image_passes_the_declared_meta_timeout():
    src = inspect.getsource(ops_image)
    assert "self.meta.timeout" in src
    # 裸数字的调用形状必须消失（含跨行换行的写法）
    assert "image_read_cmd(path), 20," not in src.replace("\n", " ").replace("  ", " ")
    assert "run_headless(context, image_read_cmd(path), 20" not in src


def test_timeout_is_declared_exactly_once():
    src = inspect.getsource(ops_image)
    assert src.count("timeout=20.0") == 1, "档位声明重复一份就会漂"
