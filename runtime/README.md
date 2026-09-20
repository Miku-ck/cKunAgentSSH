# runtime/ — 内嵌 Python 运行时（可选）

把 **Windows embeddable / portable Python** 解压到本目录，`npm run package`
就会把它随包分发到 `resources/python-runtime/`，打包后的 app 不再依赖目标机
的系统 Python（解析逻辑：`electron/backend-manager.ts` → `getPythonCommand()`）。

本目录只有 README.md 时视为「未准备运行时」：打包脚本会打警告并回退系统
python/python3，构建不失败。

## 准备步骤（Windows）

1. 下载 https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
2. 全部解压到本目录（`runtime/python.exe` 直接可见，不要多一层子目录）
3. 启用 pip：编辑 `python311._pth`，把 `#import site` 改为 `import site`，
   然后按 https://bootstrap.pypa.io/get-pip.py 装 pip
4. 安装后端依赖：`.\python.exe -m pip install -r ..\backend\requirements.txt`
5. `npm run package` —— 构建日志出现
   `[package] runtime/ 已就绪 → 打包内嵌 Python 运行时` 即生效

## 验证

装好后启动打包版，若后端正常拉起且日志无 `spawn python ENOENT`，即内嵌成功。
排障时可用环境变量 `CKUN_PYTHON=<python.exe 绝对路径>` 强制指定解释器。

> 签名（代码签署证书）不在本仓库范围：win 配置未设 `certificateFile`，
> electron-builder 会跳过签名并打警告，属预期行为。
