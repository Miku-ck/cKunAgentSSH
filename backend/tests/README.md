# backend/tests — 测试目录说明

## 运行

```bash
npm run test:backend          # = cd backend && python -m pytest tests/ -q
python -m pytest tests/test_xxx.py -v   # 单文件
```

`pytest.ini` 固定 `testpaths = tests`；`conftest.py` 把 `backend/` 根插入
`sys.path`（测试以 `import database`、`from agent...` 的方式引用被测模块），
并提供 `base_db` / `tmp_workspace` 等夹具。`api_source.py` 不是测试，是跨端
源码读取助手（`frontend_api_source` / `SOURCE_LABEL`），供契约类测试比对
前端 `src/` 与后端模型。

## 命名规范（2026-10-24 起）

`test_<域>_<守卫什么>.py` —— **文件名要说清守的是什么，不写轮次/工单号**。
出处历史（第几轮审查、Sx-xx 工单）保留在各文件自己的 docstring 里，
git log 也可追溯；文件名不再承载。

反例（已全部改名）：`test_ai_loop_round13c.py`、`test_schedule_op_grant_s706.py`
—— 看文件名不知道守什么，只能靠 docstring。

## 历史改名映射（旧名 → 新名）

2026-10-24 一次性重命名 58 个带 `_roundN` / `_sNxx` 后缀的文件。**只看新名；
此表用于把历史提交、评论、文档里的旧名对回当前位置。**

| 旧名 | 新名 | 守什么 |
| --- | --- | --- |
| test_admin_api_round4 | test_admin_permission_and_webkeys | 管理端权限授予（工具级/命令级/空工具拒绝）+ web 工具密钥保存/脱敏/清空/长度上限 |
| test_ai_loop_round13c | test_ai_loop_core_regression | 意图分级/推理预算/批量读检测/成本定价/技能触发与禁用缓存/审计结论三处同源/演化输入/LLM 客户端 LRU 与在途计数 |
| test_audit_channel_limits_s407 | test_audit_channel_limits | 审计字段上限跨端对账（渲染层裁剪值 = 后端 Field 上限） |
| test_audit_read_honesty_s1101 | test_audit_read_honesty | 审计读侧诚实性 + 四个端点首批路由级测试 |
| test_audit_run_id_on_approvals_s204 | test_audit_run_id_on_approvals | 审批决策落审计必须带 run_id |
| test_bridge_error_suggestion_s714 | test_bridge_error_suggestion | BridgeError 文案单一来源（同错误不出现四条文案） |
| test_ci_matrix_round22 | test_ci_matrix_contract | CI 形状契约（ubuntu gates + windows gates/package 两条 job 的形状由本测试钉住） |
| test_cron_error_text_s717 | test_cron_error_text | cron 报错文案不许承诺校验器不认的值 |
| test_evolution_round13 | test_evolution | 演化链路读数（低分反思入演化、DB 异常不吞输入） |
| test_file_grep_coverage_honesty_s1404 | test_file_grep_coverage_honesty | grep 不把「没读到」说成「确认没有」，不悄悄截断 |
| test_harness_round2 | test_harness_feedback_loop | Reins 反馈消费链/聚合保留/循环检测与澄清等待期豁免 |
| test_host_profile_block_prune_s1406 | test_host_profile_block_prune | host-profile 块不随连接数无限增长 |
| test_http_env_precedence_round22 | test_http_env_precedence | 出站 HTTP 策略 env 兜底优先级 |
| test_http_log_diagnostics_round19 | test_http_log_diagnostics | 出站 HTTP 策略/日志级别/诊断快照 |
| test_image_timeout_single_source_s1409 | test_image_timeout_single_source | read_image 超时唯一来源 `meta.timeout` |
| test_interrupted_run_persistence_s101 | test_interrupted_run_persistence | 中止/断连的 run 必须留下承载轨迹的 assistant 痕迹 |
| test_job_id_reuse_round10 | test_job_id_reuse | 后台任务 id 重启后撞号 + 整型设置脏行读取 |
| test_llm_client_stream_round7 | test_llm_client_stream | 流式中途取消关流/推理 token 计入估算完成量 |
| test_memory_max_items_round11 | test_memory_max_items | `memory.max_items` 读取区间（越界读数会把修剪变成清空） |
| test_minor_round3 | test_review_minor_fixes | 审查 MINOR 项回归合辑（LRU 驱逐/上传上限/web_fetch 截断/run 槽冲突与接管/默认标题/分页 tiebreak） |
| test_model_default_round22 | test_model_default_resolution | 模型主键解析 + 全局默认模型可设 |
| test_model_diag_honesty_s1019_s1021 | test_model_diag_honesty | 超时报错说准哪一相到点；服务端故障不冒充输入错误 |
| test_model_lookup_single_source_s1022 | test_model_lookup_single_source | 两种键的解析规则只存在一处 |
| test_model_seed_probe_s1003 | test_model_seed_probe | 首启模型种子探测的鉴权/超时预算/凭据脱敏 |
| test_models_panel_payload_round23 | test_models_panel_payload | 面板实际提交的键 ⇄ `src/api` 声明的 payload 类型 |
| test_numeric_args_round10 | test_numeric_args | 数字参数统一解析 + 判定视图与执行命令逐字节一致 |
| test_ops_exact_channel_round15 | test_ops_exact_channel | 字节协议通道不得被「给人看的输出」截断损坏 |
| test_ops_service_round13d | test_ops_service_failure_classification | service_manage 失败原因分类（unit 不存在 ≠ 机器没有 systemd） |
| test_permission_policy_round18 | test_permission_policy_ttl | 永久授权 TTL（gate 到期判定 + sidecar 兜底 + 落库推 gate） |
| test_permission_policy_route_loop_s203 | test_permission_policy_route_async | 改进程内审批运行时状态的路由必须 async def |
| test_post_process_round13b | test_post_process_failure_semantics | 后处理失败语义与配置同源（技能数保护/NULL 标题/常驻单例快照） |
| test_proxy_clear_persistence_s001 | test_proxy_clear_persistence | 代理清除动作的持久化 |
| test_proxy_env_precedence_s001_disputed | test_proxy_env_precedence_disputed | 代理 env 优先级的争议场景（ disputed 用例组） |
| test_reasoning_zero_budget_s1006 | test_reasoning_zero_budget | `reasoning_tokens=0` = 跟随输出预算，不是不设上限 |
| test_remote_parts_last_sentinel_s1402 | test_remote_parts_last_sentinel | `remote_parts` 按拼装顺序拆，判据取最后一次出现 |
| test_remote_protocol_round21 | test_remote_protocol_single_source | 哨兵协议单一实现源 |
| test_remote_write_verification_s1401 | test_remote_write_verification | 写类工具不看 `ok`，看管道退出码（空输入 base64 仍 0 退出） |
| test_round8_fixes | test_sse_permission_and_run_lifecycle | SSE permission_needed 透传 normalized_command/release 结论不改写/run 与会话轮转清理/prod 危险工具预授权 |
| test_sandbox_command_entry_parity_s104 | test_sandbox_command_entry_parity | Sandbox 不可禁用底线看的命令入口与全仓唯一清单一致 |
| test_schedule_create_requires_task_s718 | test_schedule_create_requires_task | `schedule_op create` 不许把漏参换成内置默认巡检词 |
| test_schedule_engine_round6 | test_schedule_engine_stability | trigger 去重标记时序（轮询 tick 与手动执行竞态） |
| test_schedule_entry_bounds_s711 | test_schedule_entry_bounds | PATCH 不许绕开 POST 的入库上限 |
| test_schedule_op_grant_s706 | test_schedule_op_grant | schedule_op 授权键粒度收到 action 级 |
| test_schedule_prompt_guard_s702 | test_schedule_prompt_guard | 定时任务系统提示词的守卫段不被上下文段失败吞掉 |
| test_schedule_target_host_s709 | test_schedule_target_host | AI 排的班必须能带上目标主机 |
| test_settings_contract_round22 | test_settings_contract | 全仓只留一种 `PUT /settings/*` 写入语义 |
| test_settings_fields_round20 | test_settings_fields | 六组此前只有前端消费的设置补齐后端用例 |
| test_settings_key_contract_round22 | test_settings_key_contract | 前端 payload 键集合 ⇄ 后端 pydantic 请求模型字段 |
| test_settings_round23 | test_settings_boot_and_runtime | 启动回填/定时任务取模型/保留条数边界/越界参数回弹/脏行可见 |
| test_skill_registry_reload_s804 | test_skill_registry_reload | 技能写侧显式刷新绕过节流，失败不被路由吞掉 |
| test_skills_chat_round5 | test_skills_chat_api | 技能增删改与 mode 配置联动（删除剪配置/冲突回真 id/404/同步/无扩展名上传） |
| test_todo_write_round9 | test_todo_write | todo_write 两处越权写入回归 |
| test_tool_call_pending_round12 | test_tool_call_pending | 半截工具调用不许在轨迹回放里显示成「成功」 |
| test_tool_result_binding_s103 | test_tool_result_binding | `/agent/tool-result` 的 run 归属与 requires_confirm 回查 |
| test_trace_projection_keys_s106 | test_trace_projection_keys | TraceEvent 的 SSE 投影不许读生产者从未写的键 |
| test_web_fetch_deadline_s608 | test_web_fetch_deadline | 慢滴响应命中总时限，而不是永远跑下去 |
| test_web_search_chain_round9 | test_web_search_chain | 降级链：「刮不到结果」不能被当成「搜不到结果」 |
| test_web_search_response_caps_s628 | test_web_search_response_caps | 三条搜索腿必须有响应体上限，Tavily 列表本地截断 |
