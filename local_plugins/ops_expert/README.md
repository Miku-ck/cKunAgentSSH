# ops-expert —— 运维专家 MCP 插件

只读运维诊断工具供给插件（stdio MCP server），供内置「运维专家（general_ops）」挂载。

## 工具清单（6 个，全部声明 read_only_hint=True）

| 工具（`mcp__ops_expert__<name>`） | 用途 |
|---|---|
| `system_overview` | 主机名/OS/CPU/内存水位/各分区磁盘使用率 |
| `disk_usage` | 指定路径所在分区的容量详情 |
| `top_processes` | 按内存排序的 Top N 进程（psutil 可用时含 CPU%） |
| `tcp_probe` | TCP 端口连通性 + 延迟（不发应用层数据） |
| `http_probe` | HTTP 健康检查（GET，状态码/延迟/Server 头） |
| `log_tail` | 日志尾部安全读取（2MB 窗口，防巨文件） |

设计边界：只读诊断，不做变更类操作（改配置/重启/杀进程走 Agent 命令通道，经人工审批与审计）。仅依赖标准库 + `mcp` SDK。

## 安装/启用步骤（同意流，任何一步不可跳过）

1. **安装**：已完成（DB `plugins` 表，slug=`ops-expert`，保存即停用未确认）；
2. **验证并加载工具**：插件面板点击「验证」→ 拉起进程发现工具清单（已预执行过一次，UI 重新点一次即可）；
3. **确认启用**：看过工具清单后点击确认（写 `consented_at`）；
4. **逐项只读确认**：6 个工具全部勾选只读；
5. **启用开关**：打开 `enabled`。

## 运行时依赖

- 插件子进程解释器：`D:\ProgramData\miniconda3\python.exe`（已装 `mcp>=2.0,<3.0`）；
- **后端进程**自身也需可 `import mcp`（`requirements.txt` 第 21-23 行已声明，未装时插件功能不可用但主程序可启动）；
- 打包发布时 runtime Python 需同样安装 `mcp`，或将插件 command 换成 runtime 解释器。

## 挂载关系

「运维专家（general_ops）」的 `plugin_ids_json = None`（继承语义）→ 插件启用后自动获得全部 6 个工具，无需改专家配置。其他专家若显式设了 `plugin_ids_json="[]"`（如研究/文案）则不会拿到。
