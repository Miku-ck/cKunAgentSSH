# 勿重议清单（Historical adjudications — consult BEFORE raising any finding）

来源：`C:\Users\ckun\.qoder-cn\projects\D--workspace-cKunAgentSSH\memory\` 下 2026-09 各轮审查记录。
命中本清单的发现一律记入 **Rejected** 列并标注 `already adjudicated (round X)`，正文不重复争论；
若掌握了当年没有的新证据，写 `CONTRADICTION — needs re-adjudication` 并并列旧理由与新证据，**不自行翻案**。

## A. 全树扫描已定论 → 结构性不重跑

| 扫描 | 结论 | 出处 |
|---|---|---|
| Ruff `F,B,E9` 全树 | 56 命中逐类核实后仅 1 处生产代码可清（已清）；F401×18 在有意再导出的 `reins/__init__.py` barrel；F821×7 是 `TYPE_CHECKING` 前向注解；B904×13 已显式传 `original=`；B007×6 在 tests。**后端无未覆盖的真实缺陷/死码信号** | 09-23 后端 Ruff 定论 (908bb62) |
| `zip()` strict(B905) | 仅 `audit.py:_build_filters` 加 `strict=True`；`dict(zip(cols,row))`/gather/mstore._cosine **勿加**（会把降级算变热路径 500） | a8cb56c |
| 前端 unused-export | 一次性 `vue-tsc --noUnusedLocals --noUnusedParameters` 扫过两 config，现零残留 | 7997e1d/3bea866 |
| 死导出扫描 | 跨全库引用计数 = 0 个「仅定义无引用」死导出；「65 个仅本文件使用 export」是 local-only 非死码 | e82e08a 轮 |
| `.sort(` 反应性 | 全树 15 处核实无 mutation 泄漏（均排拷贝或有意就地淘汰） | 09-23 定论 |
| import 可达性 | 从 `backend/main.py` 起：**后端零孤儿模块**；78 路由 + 前端 api 77 方法全有活调用点 | 第六轮 |
| IPC 三腿对称 | preload 79 key / 68 handleTrusted / renderer 调用集合双向对称无死链 | 第六轮 |
| 启动/性能路径 | Monaco shim+runtime、SshView defineAsyncComponent、openai/httpx 惰性、ssh2/node-pty 懒载均已落地定论；剩余大头是 fastapi/uvicorn 框架本体与 xterm 刚需，**不可移** | 1095297/17c7126/c20f5d9/933313f |

## B. 已判「非 bug / 刻意设计」——具体条目

1. **server-monitor `inflight` 未在 `stop()` 重置**：非 bug。`sample()` 的 `finally` 必在 ≤12s 释放，且有 `activeSshId!==session.id` 跨会话守卫；**在 stop() 里 reset 是错的**（旧会话 finally 会覆盖新会话的 true，破坏 single-flight）。
2. **`LocalTerminal` onUnmounted 主动 `ptyDestroy`**：刻意行为，本地 PTY 卸载即销毁是防孤儿 pty 的正确默认；与 RemoteTerminal（SSH 连接持久化）类比不成立。仅补过 `.catch(()=>{})`。
3. **`rm -rf /` 型 over-block**（`grep 'rm -rf /' 日志` 误拦）：已登记取舍，有回归锁 `test_known_over_block_is_registered_tradeoff`；位置化会让 `find -exec` 降级。
4. **`retriever.py:74-76` 余弦下限「误杀 fts 强命中」**：vec 路存在时按向量分卡下限是 RRF 融合的设计意图，属语义加载判断非缺陷。
5. **`agent.py:1759 count=len(rows)`**：前端 AgentMemoryView 固定 limit=100、无 offset/分页、从不读 `count`，字段无人消费（Low）。
6. **`MarkdownStream.vue:106` 复制按钮 1.5s setTimeout 未纳管**：卸载后仅对 detached 节点写 textContent，once 且无害；正解需 Set 跟踪，不成比例。
7. **`backend-manager` stdout 空 `'data'` 监听器**：**必须保留**（不挂则子进程因管道缓冲写满而阻塞）。
8. **`modeToString` 的 `0o170000`**：S_IFMT 掩码本身永不可达，已删。
9. **`AgentPanel` 三个 `computed(()=>settings.agent)`（budget/runtimeCfg/opsCfg）刻意不合并**：「budget」一词与 `open.budget`/`dynamicBudget` 多处重叠，重命名易错且损分区可读性。
10. **`command_explain.py:353` 与 `auto_approve.py:111` 的 4 键 `model_cfg` dict 字面量重复**：仅 2 处各 5 行 trivial dict，抽 helper 需选 home 反增导入。
11. **`post_processor` model 解析三连重复**：三处仅 config 字段/log tag/返回前额外动作有别，合并需参数化回调，收益低于风险。
12. **`mstore.render_line` vs `retriever._render` 格式串重复**：输入形态不同（dict/Hit），合并需 adapter，边际收益低。
13. **`reconcile._apply` 四处 adopt/add_memory kwargs 样板重复**：合并须保住 content(merged) vs cand["content"] 的刻意区分，稍有不慎重蹈向量/正文错位 bug。
14. **`schedule_engine:451 _local_tz()` 用固定 UTC 偏移非 zoneinfo**：DST 主机 weekly/monthly 跨界错 1h；用户环境 UTC+8 无 DST → 本机零影响，留待有需求再说。
15. **`ops_obs.log_read` 319-324「未匹配到日志行」死分支**：确为死码但诚实修复需 `set -o pipefail`（bash-only，远端 shell 未证）+ 解析标记，脆弱低价值 → 备案未修，勿当简单 bug 顺手改。
16. **`safety.py:1404` 同名 `command_from_args` 内联刻意未收敛**：属冻结区边缘，且让 `harness.safety` 反向 import `harness.reins.core` 不划算。
17. **`saddle.py:399` 的 `getattr(...,"test")`**：无 `or`、口径不同（允许 `""`），刻意不动。
18. **`flow.py:362 // file_mutation_interval` 零除**：`dynamic_budget_interval` 来自 contracts 默认 3，全后端无任何写路径灌 0，当前不可达；加 `max(1,..)` 属为不存在的场景兜底。
19. **`driver` 完成路径双 `cost_tracker.snapshot()` / guards `is_denied` 双调用**：热循环收尾 + await 边界后状态理论可变，纯去重收益低。
20. **前端明确延后（是 feature 非 cleanup，勿当简单 bug 补）**：① 本地终端 `getCwd` 恒返回启动目录（真修需 shell-integration）；② `useTerminalContext.positionAskAiBtn` 选择 x 疑似差一列（需 DOM mount 验证）；③ `main.ts findProgramsInPathByPrefix` 非 win32 分支产 bogus token（唯一 caller 是 win32-only，永不可达）。
21. **`reins/__init__.py` barrel 的 F401 再导出**：有意的测试用再导出面，勿当死码删、勿批量塞 `__all__`。
22. **`mstore.reset_flags`**：仅测试消费者，但是真消费者（刻意测试钩子），非死码勿删。

### 设置模块 round 23（2026-09-29 审查）登记未修

23. **`models_config.supports_thinking / supports_image / supports_tool_calling` 三列保留**：全仓零消费者（思考走 `enable_thinking`＋演化、工具下发走 `enable_tools`、图片从不按模型判定）。本轮按裁决**删掉面板勾选**并停止写入，列与读侧回传留着供 seed/旧数据显示徽标。真上限：徽标是历史事实不是约束；将来若要接线，得同时交代「已勾 tool_calling=0 的存量模型突然收不到工具」的迁移。
24. **`post_processor._call_llm_with_timeout` 仍不传 `top_p`**：起标题/记忆抽取/技能提取固定走 0.9。本轮只把交互式（`routes/agent.py`）与定时任务（`schedule_engine`）两条主链接到模型行；该 helper 被 5 处后处理子任务共用，加形参要把模型行一路传进去，而短输出任务对采样几乎不敏感。
25. **`PATCH /settings/models/{config_id}` 允许把两档默认都清掉**：`update_model_config` 只在置 1 时扫同档（`if kwargs.get(key)`），显式传 false 会留下 0 默认。不改成拒绝，是因为「换默认」的正常两步（先撤旧的再勾新的）会被 400 卡死，与 `delete_model_config` 的守卫场景不同。真上限：并发/手改可致 0 默认，运行时有 全局档 → `list_models_config()[0]` 两级兜底。
26. **`AppearancePanel`「跟随强调色/跟随主题底色」取消勾选不改变量**：存储值 `''` 就是「跟随」的唯一表示，要表达「取消跟随但不选新色」需引入第三态。当前未勾选时色板显示的是**解析后的实际色**，不是错值——与当年 `#58a6ff`/`#0078d4` 那种「显示与生效不符」不同族，勿当同一 bug 再提。
27. **`stores/settings.ts flushOnPageHide` 无条件 PUT 记忆与后处理**：不加脏判定，因为关窗时点没有可靠基线可比（`lastSynced*` 只记成功同步）。后端两个 PUT 都是整包同值写入，幂等。真上限：每次关窗两条 SQLite 事务。
28. **`http.retry_count` 只对 LLM 生效**：`netconfig.new_client()`/`proxy_http_client()` 造的是裸 httpx 客户端，无重试传输层。本轮把面板与 `HttpSettingsRequest` 的文案改成如实描述（「仅 LLM 调用」）而不是实现重试——给全部出站请求加重试要引 tenacity 或自定义 transport，且会改变 embedding/网页抓取的失败时序。
29. **定时任务不读 `max_output_tokens`**：`schedule_engine` 按上下文窗口一半推导输出上限，而交互式走 `safe_max_tokens`。两条通道对「输出预算」的定义本就不同（定时任务无流式、无 UI 反馈），统一到一套要重做 schedule 的钳制逻辑。
30. **聊天代码块不随「消息字号」缩放**：`--chat-font-size` 只驱动正文（`.markdown-body`/`.message-bubble`/`.message-content`），代码块保留自己的 11–12px。代码密度与正文密度是两个排版关注点，历史上代码块随正文放大导致横向滚动加剧。

## C. 口径提醒

- 代码内 `P0-x / C-review / roundNN` 注释是**历史修复标记**，勿据此判定现存缺陷。
- 用户约束：**别再加带隐喻的新组件**。
- 目标「优化/精简/清冗余/修 bug」是开放式无终态，不以此自评为完成。
