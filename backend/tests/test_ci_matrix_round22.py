"""CI 形状契约（round 22）—— 交付目标只有 Windows，CI 却只在 ubuntu 上验。

本仓 100% 的缺陷与修复都发生在 Windows（named pipe 桥、DPAPI、winpty ConPTY、
electron-builder nsis/portable、node-pty/ssh2 原生编译）。ubuntu runner 上
`npm ci` 的 postinstall 走的是 Linux 工具链，原生模块编译失败被 `|| true` 吞掉，
于是「装不上/编不过/打不出包」这类问题只在用户机器上第一次暴露。

这里不加行为测试，而是把 workflow 的形状钉住：交付平台必须在 CI 里真实跑过
typecheck / vitest / 后端 pytest / 构建。yml 用正则抽（CI 配置文件没有 yaml 依赖，
为一个断言引入 pyyaml 不值得），所以形状一变（矩阵改名、job 拆并）本测试会红，
这正是想要的：改 ci.yml 的人必须顺手改这里，等于强制 review。

运行: python -m pytest backend/tests/test_ci_matrix_round22.py -v
"""
import re
from pathlib import Path

import pytest

CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _text() -> str:
    return CI_YML.read_text(encoding="utf-8")


def _job_blocks() -> dict[str, str]:
    """按 `  <job_id>:` 切块（GitHub Actions 的 jobs 固定缩进 2 空格）。"""
    text = _text()
    starts = [(m.group(1), m.start()) for m in re.finditer(r"^  ([\w][\w-]*):\s*$", text, re.M)]
    blocks = {}
    for i, (name, pos) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        blocks[name] = text[pos:end]
    return blocks


def test_ci_file_exists():
    assert CI_YML.exists(), f"找不到 {CI_YML}"


def test_ci_covers_windows_runner():
    """交付目标是 Windows：ubuntu 单跑等于从没验过真实交付物。"""
    assert re.search(r"runs-on:\s*(\$\{\{\s*matrix\.os\s*\}\}|windows-latest)", _text()), (
        "ci.yml 没有任何 Windows runner：原生编译（node-pty/ssh2）、"
        "named pipe 桥、nsis 打包链路全都只在开发机上首次暴露")


def test_ci_still_covers_linux():
    """ubuntu 不能因为加 Windows 而被删掉——Gitee Go/本地等价检查与后端依赖都在它上面。"""
    assert "ubuntu-latest" in _text()


def _win_job_body() -> str:
    """Windows 侧的 job 文本（独立 job，或矩阵里带 windows 的那些 step）。

    注释行会被剥掉：yml 的说明文字里出现 `|| true` 不算这条 job 真的吞失败。
    """
    blocks = _job_blocks()
    standalone = [b for b in blocks.values() if "windows-latest" in b]
    if standalone:
        return _strip_comments("\n".join(standalone))
    for block in blocks.values():
        m = re.search(r"os:\s*\[([^\]]*)\]", block)
        if m and "windows" in m.group(1):
            steps = block[block.index("steps:"):]
            picked = "\n".join(s for s in re.split(r"\n(?=^\s*- )", steps, flags=re.M)
                               if "windows" in s)
            return _strip_comments(picked or steps)
    raise AssertionError("ci.yml 里没有 Windows job")


def _strip_comments(text: str) -> str:
    """丢掉整行注释：yml 里「不加 || true」这类说明文字会被形状断言误读成命令。"""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#"))
    raise AssertionError("ci.yml 里没有 Windows job")


def test_two_single_os_jobs_not_a_matrix():
    """两条单 OS job，而不是 runs-on 矩阵：矩阵逼着每个 step 挂 if: matrix.os，
    三处平台差异（python 装法、pip 缓存、pytest 只在 Linux 跑）会把 yml 淹在守卫里。"""
    text = _text()
    assert "${{ matrix.os }}" not in text, "改用矩阵后，per-step if 守卫会让闸门漏跑看不出来"
    blocks = _job_blocks()
    runners = [re.search(r"runs-on:\s*([\w-]+)", b) for b in blocks.values()]
    oses = [m.group(1) for m in runners if m]
    assert "ubuntu-latest" in oses and "windows-latest" in oses, f"两条单 OS job 不齐：{oses}"


def test_windows_job_runs_frontend_gates_and_build():
    """Windows job 必须跑前端三条闸门（typecheck / vitest / build）。

    只跑构建的 Windows job 是装饰性的：本仓的缺陷都在类型检查与单测里。
    后端 pytest 有意只在 ubuntu 跑（见 ci.yml 注释），所以这里不断言它。
    """
    body = _win_job_body()
    assert _missing_gates(body) == [], f"Windows job 漏掉了闸门：{_missing_gates(body)}"


def test_backend_pytest_stays_on_linux():
    """两条 job 各跑一遍 1481 项后端用例不会多抓到缺陷，只会把 Windows 特有的
    socketpair 抖动变成无人处理的永久红。"""
    blocks = _job_blocks()
    win = _win_job_body()
    assert "pytest" not in win, "后端 pytest 不该在 Windows job 里跑（见本文件 docstring）"
    ubuntu = "\n".join(b for b in blocks.values() if "ubuntu-latest" in b)
    assert re.search(r"pytest", ubuntu), "ubuntu job 必须跑后端 pytest"


def test_native_compile_failure_is_visible_on_windows():
    """`|| true` 是 ubuntu 侧的历史妥协（原生模块编不过就跳过）。Windows 上恰恰
    要让它红：winpty/ConPTY 依赖 node-pty 真编出来，静默跳过等于把问题推给用户。"""
    body = _win_job_body()
    assert not re.search(r"\|\|\s*true", body), (
        "Windows 上把失败用 `|| true` 吞掉，等于这条 job 什么都没验")


GATES = {
    "typecheck": r"npm run typecheck",
    "vitest": r"vitest run",
    "build": r"npm run build",
    "package": r"npm run package",
}


def _missing_gates(body: str) -> list[str]:
    return [name for name, pat in GATES.items() if not re.search(pat, body)]


# ── 自检：证明这套抽取真的分得清两条 job、删步骤真的会红 ─────────────────────

def test_extraction_picks_the_windows_block_not_ubuntu():
    """抽取逻辑若抽错块（永远拿到 ubuntu 那段），下面的闸门断言会一路假绿。"""
    body = _win_job_body()
    assert "windows-latest" in body and "ubuntu-latest" not in body


@pytest.mark.parametrize("gate", sorted(GATES))
def test_guard_actually_catches_a_dropped_windows_step(gate):
    body = _win_job_body()
    assert _missing_gates(body) == [], f"前提不成立：现有 ci.yml 的 Windows job 缺 {gate}"
    mutated = re.sub(GATES[gate], "echo skipped", body, count=1)
    assert _missing_gates(mutated) == [gate], (
        f"删掉 {gate} 那一步后断言没有变红 —— 这套形状契约是摆设")


def test_guard_actually_catches_swallowed_install_failure():
    body = _win_job_body()
    mutated = re.sub(r"(run: npm ci)", r"\1 || true", body, count=1)
    assert mutated != body, "npm ci 那一步的形状变了，请同步更新本自检"
    assert re.search(r"\|\|\s*true", mutated), "安装失败被吞掉的断言抓不到退化"
