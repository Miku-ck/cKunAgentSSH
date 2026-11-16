"""后端导入耗时基准：区分「字节码命中 / 未命中」并列出最重的导入模块。

用法（在仓库根目录）：
    python scripts/bench-imports.py            # 用当前解释器
    python scripts/bench-imports.py --python D:/ProgramData/miniconda3/python.exe
    python scripts/bench-imports.py --python runtime/python.exe

输出三段：
  1. 解释器与字节码缓存现状（cpython-<tag>.pyc 数量）
  2. 连续 3 次 `import main` 的墙钟耗时（新进程，模拟每次冷启动的导入段）
  3. `-X importtime` 里 self 耗时最大的 15 个模块

注意：本脚本只读源码、不写后端目录（compileall 不跑），可安全在发布前执行。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def timed_run(python: str, args: list[str], cwd: Path | None = None) -> tuple[float, subprocess.CompletedProcess]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    t0 = time.perf_counter()
    proc = subprocess.run(
        [python, *args],
        cwd=str(cwd or BACKEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return (time.perf_counter() - t0) * 1000, proc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="要测量的 Python 解释器")
    ap.add_argument("--runs", type=int, default=3, help="重复导入次数")
    args = ap.parse_args()

    python = args.python
    ver = subprocess.run(
        [python, "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip() or "?"
    print(f"解释器      : {python}")
    print(f"Python 版本 : {ver}")

    parts = ver.split(".")
    tag = f"cpython-{parts[0]}{parts[1]}" if len(parts) >= 2 else "cpython-?"
    pyc = list(BACKEND.rglob(f"*{tag}.pyc"))
    other = [p for p in BACKEND.rglob("*.pyc") if tag not in p.name]
    print(f"字节码缓存  : {len(pyc)} 个 {tag}.pyc；另有 {len(other)} 个其它版本 .pyc")
    if not pyc:
        print("  [SLOW] 该解释器没有任何字节码缓存 —— 每次启动都会从源码全量编译。")
        print("         参考 scripts/package.js 注释：import main 无 .pyc 5240ms / 有 .pyc 430ms。")

    print()
    print("import main 墙钟耗时（每次都是新进程）")
    walls: list[float] = []
    for i in range(args.runs):
        ms, proc = timed_run(python, ["-c", "import main"])
        if proc.returncode != 0:
            print(f"  run{i + 1}: FAILED exit={proc.returncode}")
            print((proc.stderr or "")[-1200:])
            return 1
        walls.append(ms)
        print(f"  run{i + 1}: {ms:8.0f} ms")
    if walls:
        print(f"  最小 / 中位 : {min(walls):.0f} ms / {sorted(walls)[len(walls) // 2]:.0f} ms")
        if min(walls) > 3000:
            print("  [SLOW] 导入 > 3s，与「无字节码命中 / 冷盘 + 实时扫描」特征一致。")

    # 冷态对照：把 backend 源码复制到一个不含 __pycache__ 的临时目录，在副本里
    # import main —— site-packages 字节码仍命中，只有项目自身从源码编译，精确对应
    # 「打包脚本清空过 __pycache__ / 解包到只读目录」的场景。不改 backend/ 任何文件。
    print()
    print("冷态对照：backend 无 .pyc（临时副本）")
    import shutil
    cold_root = Path(os.environ.get("TEMP", "/tmp")) / f"ckun-pyc-cold-{os.getpid()}"
    cold_backend = cold_root / "backend"
    try:
        shutil.copytree(
            BACKEND, cold_backend,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.pyo", "tests", "_tmp",
                "*.db", "*.db-wal", "*.db-shm", "*.log",
            ),
        )
        cold_ms, cold_proc = timed_run(python, ["-c", "import main"], cwd=cold_backend)
        if cold_proc.returncode != 0:
            print(f"  FAILED exit={cold_proc.returncode}")
            print((cold_proc.stderr or "")[-600:])
        else:
            print(f"  冷态 import main : {cold_ms:8.0f} ms")
            if walls:
                print(f"  热态最小         : {min(walls):8.0f} ms")
                print(f"  字节码缓存收益   : {cold_ms - min(walls):8.0f} ms  ({cold_ms / min(walls):.1f}x)")
    finally:
        shutil.rmtree(cold_root, ignore_errors=True)

    print()
    print("importtime self 耗时 Top 15（µs）")
    _, p = timed_run(python, ["-X", "importtime", "-c", "import main"])
    rows: list[tuple[int, int, str]] = []
    for line in (p.stderr or "").splitlines():
        if not line.startswith("import time:"):
            continue
        payload = line.split(":", 1)[1].strip()
        fields = payload.split("|")
        if len(fields) < 3:
            continue
        try:
            self_us = int(fields[0].strip())
            cum_us = int(fields[1].strip())
        except ValueError:
            continue
        rows.append((self_us, cum_us, fields[2].strip()))
    for self_us, cum_us, name in sorted(rows, reverse=True)[:15]:
        print(f"  {self_us:9d}  cum={cum_us:9d}  {name}")
    print()
    print("判读：self 大而 cumulative≈self 的模块通常是编译/执行本身重；")
    print("      self 小但 cumulative 大的是它拉起了一整棵依赖树。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
