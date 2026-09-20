"""
Agentic AI 框架 — Agent 编排核心（Harness 架构）

模块组成：
- harness/    驾驭工程框架（Driver/Bridle/Reins/Saddle/Stirrups/Telemetry）
- tools/      工具注册中心 + 内置工具
- memory      情景/语义记忆 + 检索（embedding 有 fallback 词法相似度）
- reflection  启发式运行评分（效率/鲁棒/安全/完成/成本加权，非 LLM 反思）
- evolution   规则式参数调整（依评分调 max_iterations/temperature 等）
- consciousness_tick  后台维护任务（记忆固化/修剪、失败反思调度）
- cost_tracker  成本追踪
"""
