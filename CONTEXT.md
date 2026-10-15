# CONTEXT — 术语表

只放"这个词在这个项目里到底指什么"。实现细节看代码，取舍理由看 `docs/adr/`。

| 术语 | 定义 | 为什么要钉住 |
| --- | --- | --- |
| **端口已分配**（`getPort()`） | spawn 后端**之前**从 8010–8100 里选定的端口号。非零只代表"选过端口"，不代表"有人在那个端口监听" | 把它当就绪用就是假 ready 病的病灶：后端彻底没起来时首屏仍显示"已就绪" |
| **真就绪**（`whenReady()` / `ready`） | 后端已通过 `/health` 健康检查、可以接请求的状态 | 渲染层的状态机只能由真就绪驱动；端口号至多当 URL 提示用 |
| **降级模式**（degraded single-instance） | 拿不到单实例锁、但已证实是 userData 不可写（而非真有第二个实例）时继续启动，此时**单实例保证失效** | 与"已有实例→退出"是两种语义；两者的判据必须同源，否则会退回"打印继续启动却什么都不做"的僵尸分支 |
| **内嵌运行时**（bundled runtime） | 构建期把 Python 解释器随包放进 `resources/python-runtime/`；Windows 认 `runtime/python.exe`，POSIX 认 `runtime/bin/python3` | 两侧路径口径不一致过 —— 探到却读不到，mac/linux 等于从来没有内嵌运行时 |
| **孤儿后端**（orphan backend） | 上次应用异常退出后残留的 Python 后端进程，持有过期的 bridge 端口/token | 它的清理判据有宽/严之争，取舍记在 ADR 0003 |
| **半写损坏**（torn write） | 进程被杀在写文件中间，留下半个 JSON | 项目内所有落盘必须 tmp+rename/os.replace 原子写，就是为了不产生这个词 |
| **隔离**（quarantine，`.corrupt-<ts>.bak`） | 读不出可信内容的文件被改名留证，而不是被静默当空集用 | "静默当空集"会让下一次写盘用子集覆盖真实基准；读不出必须诚实上报"不知道" |
| **审计结论口径**（`conclusion ∈ {…}`） | 后端枚举、前端筛选下拉、契约文档三处必须同一份结论取值 | 三处同源的守卫；载体搬迁的理由见 `docs/adr/0004-audit-conclusion-contract-carrier.md` |
