<script setup lang="ts">
/**
 * AgentRunPanel — Agent 运行过程通知区（异常/需要用户决策时显示）
 *
 * 宿主是首页任务台：挂在消息流末尾（HomeWorkspace），自身按 agentStore.hasRunNotice
 * 决定内容块挂不挂载。宿主不要再抄一份条件：曾经抄过，那份漏了澄清向导/任务规划，
 * 结果是需要用户决策时面板根本没挂载，用户只看到一条 toast，
 * 运行等到后端 TTL 超时。
 *
 * 实际包含的区块（均为条件渲染，无内容时整块隐藏）:
 * - （意图澄清已并入输入区上方 ApprovalDock 的澄清卡，不在此面板）
 * - 任务规划（意图分类：级别/任务类型/思考预算）
 * - 警告（warnings）
 * - 人工介入求助卡片（AI 请求帮助，可忽略）
 * - 错误（errorMessage）
 *
 * 工具授权与终端交付都不在这里：唯一逐条决策位是输入区上方的 ApprovalDock
 * （ApprovalCard / DeliveryCard），批量结算只作用于权限请求。
 *
 * 注：常规运行状态（thinking/acting、迭代计数、工具调用流）不在本面板展示 —
 * 执行过程与工具调用明细归运维轨迹页，按 run_id 回放。
 */
import { computed } from 'vue'
import { useAgentStore } from '@/stores/agent'

const agentStore = useAgentStore()

function levelLabel(level: string): string {
  const map: Record<string, string> = {
    trivial: '简单',
    simple: '单步',
    moderate: '多步',
    complex: '复杂',
    debug: '调试',
  }
  return map[level] || level
}

function taskTypeLabel(type: string): string {
  const map: Record<string, string> = {
    query: '查询',
    implement: '实现',
    modify: '修改',
    refactor: '重构',
    debug: '调试',
    research: '调研',
    conversation: '对话',
  }
  return map[type] || type
}

// ── 人工介入提示（Task 3：降级为顶部细条通知，只显示最近一条） ──
const latestHelp = computed(() =>
  agentStore.humanHelpRequests[agentStore.humanHelpRequests.length - 1] ?? null,
)

</script>

<template>
  <div v-if="agentStore.hasRunNotice" class="agent-run-panel">
    <!-- 规划/意图信息 -->
    <div class="intention-section" v-if="agentStore.intentionInfo">
      <div class="ap-card ap-intention">
        <div class="ap-head">
          <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          <span class="ap-ttl">任务规划</span>
          <span class="ap-level" :class="`level-${agentStore.intentionInfo.level}`">
            {{ levelLabel(agentStore.intentionInfo.level) }}
          </span>
        </div>
        <div class="ap-intention-details">
          <span class="ap-tag">{{ taskTypeLabel(agentStore.intentionInfo.task_type) }}</span>
          <span class="ap-tag" v-if="agentStore.intentionInfo.allow_planning_phase">含规划阶段</span>
          <span class="ap-tag" v-if="agentStore.intentionInfo.reasoning_budget">思考预算 {{ agentStore.intentionInfo.reasoning_budget }} tokens</span>
        </div>
      </div>
    </div>

    <!-- 警告 -->
    <div class="warnings-section" v-if="agentStore.warnings.length > 0">
      <div v-for="(w, index) in agentStore.warnings" :key="`warn-${index}`" class="ap-warning">
        <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        {{ w }}
      </div>
    </div>

    <!-- 人工介入提示（Task 3：降级为顶部细条通知，非阻塞、不占审批焦点）
         2026-10-23：reason 携带失败模式诊断（卡在哪），suggestion 为可执行的
         恢复建议（怎么办）——两者都来自后端 ConfidenceMonitor，无则不渲染 -->
    <div class="ap-help-msgbar" v-if="latestHelp">
      <svg class="ap-ic ap-help-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M14.8 9.2a3 3 0 1 0-5.2 2c.9.9 1.4 1.3 1.4 2.3"/><line x1="12" y1="17.2" x2="12.01" y2="17.2"/></svg>
      <div class="ap-help-body">
        <span class="ap-help-msg" :title="latestHelp.reason">
          AI 请求人工介入<template v-if="agentStore.humanHelpRequests.length > 1">（{{ agentStore.humanHelpRequests.length }} 次）</template>：{{ latestHelp.reason }}
        </span>
        <span v-if="latestHelp.suggestion" class="ap-help-suggestion" :title="latestHelp.suggestion">
          建议：{{ latestHelp.suggestion }}
        </span>
      </div>
      <span class="ap-sp"></span>
      <button
        v-if="!agentStore.isRunning"
        class="ap-icon-btn"
        title="忽略"
        aria-label="忽略该请求"
        @click="agentStore.dismissHelp(latestHelp.id)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>

    <!-- 权限审批与终端交付都不在这里渲染：唯一决策位是输入区上方的 ApprovalDock。 -->

    <!-- 错误 -->
    <div class="error-section" v-if="agentStore.errorMessage">
      <div class="ap-error">
        <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        {{ agentStore.errorMessage }}
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ═══════════ AgentRunPanel 卡片化重构（ui-ux-pro-max：Minimal + JetBrains Mono / OLED 深色） ═══════════
 * 视觉沿用项目的 CSS 变量令牌（status-* / card-* / accent-* / text-*），
 * 语义色与统计口径和既有模型保持一致，仅调整结构、层级与间距。 */
.agent-run-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Mono", Menlo, Consolas, monospace;
}

/* ── 通用卡片骨架 / key-value 行 / 徽章 / 按钮：见 @/styles/approval-card.css ──
 * 那一份现在由 ApprovalCard 与本面板共同消费，抄回 scoped 就是两处漂移的开始。
 * 这里只留下面板自己的布局与覆盖规则。 */

/* ── 规划/意图卡片 ── */
.ap-intention { background: var(--bg-elevated); border-color: var(--status-info-border); }
.ap-level {
  font-size: 11px;
  padding: 2px 9px;
  border-radius: 999px;
  font-weight: 600;
}
.ap-level.level-trivial,
.ap-level.level-simple { background: var(--status-success-bg); color: var(--status-success-text); }
.ap-level.level-moderate { background: var(--status-warn-bg); color: var(--status-warn-text); }
.ap-level.level-complex { background: var(--status-error-bg); color: var(--accent-orange); }
.ap-level.level-debug { background: var(--status-debug-bg); color: var(--status-debug-text); }
.ap-intention-details {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 13px 10px;
}
.ap-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 5px;
  background: var(--status-tag-bg);
  color: var(--status-tag-text);
  border: 1px solid var(--status-tag-border);
}

/* ── 警告 ── */
.ap-warning {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  font-size: 12.5px;
  color: var(--status-warn-text);
  background: var(--status-warn-bg);
  padding: 8px 12px;
  border-radius: var(--radius-lg);
}
.ap-warning .ap-ic { margin-top: 2px; color: var(--status-warn-text); }

/* ── 人工介入提示（Task 3：降级为顶部细条通知，非阻塞） ── */
.ap-help-msgbar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--status-warn-text);
  background: var(--status-warn-bg);
  border: 1px solid var(--card-warn-border);
  border-radius: var(--radius-lg);
  padding: 6px 10px;
}
.ap-help-msgbar .ap-ic { flex: none; color: var(--status-warn-text); }
/* 主体两行：主行 reason + 可选建议行（后端诊断产出，无则不渲染） */
.ap-help-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ap-help-msg {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ap-help-suggestion {
  font-size: 11px;
  opacity: 0.85;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ap-help-accent { color: var(--status-warn-text); }

/* 审批托盘 / 折叠队列 / 追溯区的样式随托盘一起删（R1）：它们的唯一消费方在模板里
   已经没有了。批量与追溯不是丢了，是搬进首页「待你放行」队列。 */

/* ── 错误 ── */
.ap-error {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--status-error-text);
  background: var(--status-error-bg);
  padding: 9px 12px;
  border-radius: var(--radius-lg);
}
.ap-error .ap-ic { color: currentColor; }
</style>

