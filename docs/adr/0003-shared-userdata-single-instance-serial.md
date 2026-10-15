# dev 与已安装版共用同一个 userData，靠单实例锁串行

`package.json` 的 `name` 与 electron-builder 的 `productName` 同为 `cKunAgentSSH`，且主进程不调
`app.setName` / `setPath('userData')`，因此 `npm run dev` 起来的实例与安装版指向同一个
`%APPDATA%\cKunAgentSSH`——同一份 db、同一份 JSON store、同一把单实例锁。

决定：**保持共享**，谁先起来谁服务，后起的实例拿不到锁就退出（这是单实例的正确语义）。

## 为什么不隔离

给 dev 一个独立 userData（后缀目录或 `--user-data-dir` 指向 node_modules）能让两者并行，但会重新
打开一扇门：`BackendManager.killOrphanBackends` 的兜底判据只要命令行含 `\backend\main.py` +
`--host 127.0.0.1` + 端口落在 8010–8100 就认定"自己的孤儿后端"并 `taskkill /T`，而安装版的路径
`…\resources\backend\main.py` 完全命中。两个实例同时活着时，dev 启动会杀掉正式版正在用的后端。
要么收紧该判据（它会失去"安装目录被移动后仍能清掉孤儿"的既有用途），要么每次启动跳过孤儿清理
（异常退出残留的 python 会重新堆到 5 个占满 8010–8014）。两个代价都比"开发前先退正式版"大。

## 后果

单实例锁拿不到时的两种成因必须分开判（`main.ts` 的 `userDataWritable()` 探针）：真有实例 → 退出；
userData 不可写导致锁文件创建失败（Windows ERROR_ACCESS_DENIED）→ 降级继续并显式承认
"单实例保证失效"。这条判据与 `whenReady` 的 `mayStart` 守卫必须同源，否则僵尸分支会回来。
