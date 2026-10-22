# runtime/ — 内嵌 Python 运行时（发布包必需）

把可重定位的 Python 解压到本目录，`npm run package` 会把它随包分发到
`resources/python-runtime/`，打包后的 app 不再依赖目标机的系统 Python
（解析逻辑：`electron/backend-manager.ts` → `resolvePythonCommand()`）。

**本目录只有 README.md 时，`npm run package` 直接失败退出**（exit 1），不再"打一行警告照常出包"：
没有内嵌运行时的包装到用户机器上，AI 侧必挂而产物表面看不出异常。取舍理由记在 git history 的
ADR-0002。确实要出无运行时的包（排障、试流水线）
加 `--allow-missing-runtime`。

CI 的 Windows job 里「Prepare embedded Python runtime」一步会自动准备它，本地打包才需要手动做。

## 准备步骤（Windows，推荐与 CI 同源）

用 [python-build-standalone](https://github.com/astral-sh/python-build-standalone) 的
`cpython-3.11.*-x86_64-pc-windows-msvc-install_only_stripped.tar.gz`：自带可重定位的
`python.exe` + `Lib/`，不必手改 `._pth`、不必另装 pip。

1. `curl -sSL -o py.tar.gz <上面那个资产的 browser_download_url>`
2. `tar -xzf py.tar.gz -C runtime --strip-components=1`（结果：`runtime/python.exe` 直接可见，别多一层子目录）
3. `runtime\python.exe -m ensurepip --upgrade`
4. `runtime\python.exe -m pip install -r backend\requirements.txt`
5. `npm run package` —— 日志出现 `[package] runtime/ 已就绪 → 打包内嵌 Python 运行时` 即生效

> 也可以用 python.org 的 Windows embeddable 包，但要额外做两件事：编辑 `python311._pth`
> 把 `#import site` 改成 `import site`，再按 https://bootstrap.pypa.io/get-pip.py 装 pip；
> 漏任一步，第 4 步的 pip 安装会静默失败，包里的后端依旧起不来。

## 验证

装好后启动打包版，若后端正常拉起且日志无 `spawn python ENOENT`，即内嵌成功。
排障时可用环境变量 `CKUN_PYTHON=<python.exe 绝对路径>` 强制指定解释器。

## 准备步骤（macOS / Linux）

内嵌运行时在包内的位置由 `electron/backend-manager.ts` 决定，构建期探测由
`scripts/package.js` 决定，两处必须同一口径：

| 平台 | 仓库里放这里 | 打进包后 | 运行时读取 |
| --- | --- | --- | --- |
| Windows | `runtime/python.exe` | `resources/python-runtime/python.exe` | 同左 |
| POSIX | `runtime/bin/python3` | `resources/python-runtime/bin/python3` | 同左 |

即 POSIX 要把 portable Python 解压成 `runtime/bin/python3`（多一层 `bin/`），
依赖同样装进这个解释器：`runtime/bin/python3 -m pip install -r ../backend/requirements.txt`。
放成 `runtime/python3` 不会被探测到 —— 探测与解析都只认上表这两条路径。

> 签名（代码签署证书）不在本仓库范围：win 配置未设 `certificateFile`，
> electron-builder 会跳过签名并打警告，属预期行为。
