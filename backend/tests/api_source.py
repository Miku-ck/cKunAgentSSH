"""跨语言守卫的前端读源单一入口。

这些守卫要拿前端的 TS 声明与后端模型比对，原先都直接读 `src/api/client.ts` 单份
文本再正则抽取。ARCH-008 把那个文件拆成 base/core/audit/sse/agent 五个模块、
client.ts 退化成 barrel 之后，单文件读法抽不到任何接口 —— 而"抽不到"在这类断言里
的两种表现都坏：要么直接抛「找不到接口」把重构当成事故报，要么抽出空集让断言
因看不见而通过（round 22 已记过后一种）。

读法跟着 barrel 走：barrel 声明了哪几个模块，就按声明顺序合并哪几份。以后再加
或再拆文件，守卫不用跟着改；barrel 写了 `./x` 而 `x.ts` 不存在，在这里炸掉，
不留给下游用空集兜底。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "src" / "api"
BARREL = API_DIR / "client.ts"

# 只认 barrel 那种写法：`export * from './name'`（引号两种都行）
_REEXPORT = re.compile(r"^export \* from ['\"]\./([^'\"]+)['\"]", re.M)

# 报错时指代读源用这个名字，别再写死单个文件名
SOURCE_LABEL = "src/api（client.ts barrel 及其 re-export 的模块）"


def frontend_api_source() -> str:
    """barrel 本文 + 它 re-export 的每个模块，按声明顺序拼成一份文本。"""
    barrel = BARREL.read_text(encoding="utf-8")
    specs = _REEXPORT.findall(barrel)
    if not specs:
        raise AssertionError(
            f"{BARREL} 里没有任何 `export * from './x'` —— 本守卫的读法前提已失效")
    parts = [barrel]
    for spec in specs:
        path = (API_DIR / f"{spec}.ts").resolve()
        if API_DIR.resolve() not in path.parents:
            raise AssertionError(f"barrel 里的 './{spec}' 指向 src/api 之外：{path}")
        if not path.is_file():
            raise AssertionError(f"barrel 声明了 './{spec}' 但 {path} 不存在")
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)
