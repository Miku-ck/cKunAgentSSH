# 发布物内嵌 Python 运行时，且缺运行时不允许出包

打包版要跑 AI 侧必须有一个装了 fastapi/uvicorn/openai/httpx/pydantic 的 Python。此前发布链对此
没有任何保证：`runtime/` 只有 README，`scripts/package.js` 探测不就绪时只打一行 warning 就照常出包，
CI 的 Windows job 也不准备 runtime —— 结果是"能出一个装到别人机器上 AI 全灭的包"。

决定：**CI 在打包前准备内嵌 Python**（python-build-standalone 解压到 `runtime/`，再 `pip install -r
backend/requirements.txt`），由 package.js 既有的就绪探测自动带进 `resources/python-runtime/`；
同时把 package.js 的就绪探测从 warning 升级为**构建期硬失败**，`npm run package` 在没有 runtime 时
直接 exit 1，留 `--allow-missing-runtime` 显式放行（排障/试流水线用）。

## 备选方案（已否决）

- **只加闸门不内嵌**：发布链不再说谎，但普通用户仍装不上 AI 侧。
- **不内嵌、做首启引导**：把不可用变成可见可自救，产物仍不自足。
- **内嵌但不加闸门**：CI 有、本地随手打包没有，静默半残包这条路还开着。

## 后果

包体 +40~60 MB（相对 electron 运行时本身可接受）；POSIX 侧内嵌路径口径已统一为
`runtime/bin/python3` ↔ `resources/python-runtime/bin/python3`（见 runtime/README.md 的表），
Windows 是 `runtime/python.exe` ↔ `resources/python-runtime/python.exe`。CI 需处理 embeddable
发行版的 `._pth` 站点限制，选 python-build-standalone 正是为了绕开这一步。
