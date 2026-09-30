<script setup lang="ts">
/**
 * AgentRunPanel — Agent 运行过程通知面板（异常/需要用户决策时显示）
 *
 * 实际包含的区块（均为条件渲染，无内容时整块隐藏）:
 * - 澄清向导（ClarifyingWizard：自带 visible 门控 + Teleport，不受下方整块开关影响）
 * - 任务规划（意图分类：级别/任务类型/思考预算）
 * - 警告（warnings）
 * - 人工介入求助卡片（AI 请求帮助，可忽略）
 * - 可见终端执行：待确认命令（确认/拒绝）
 * - 错误（errorMessage）
 *
 * 工具授权请求原先也在这里（审批托盘：单一焦点卡 + 批量 + 追溯），R1 起整块撤走 ——
 * 同一份 permissionRequests 现在只有两处画法，且分工不重叠：过程区的内联卡是
 * 唯一的逐条决策位，首页那条计数窄条只管汇总、批量与追溯。结算仍走 store 的
 * approvePermission / denyPermission。`hasRunNotice` 里那一项
 * 条件随之撤掉（留着会空挂一块面板）。
 *
 * 整块的显隐由 agentStore.hasRunNotice 决定（数据侧单一判定，逐区块对齐）——
 * 宿主组件不要再抄一份条件：漏掉一项就等于那块内容永远不挂载。
 *
 * 注：常规运行状态（thinking/acting、迭代计数、工具调用流）不在本面板展示 —
 * 工具调用明细见消息气泡内联轨迹（AgentTraceInline）与运行交付面板。
 */
import { computed } from 'vue'
import { useAgentStore } from '@/stores/agent'
import ClarifyingWizard from './ClarifyingWizard.vue'

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
  <ClarifyingWizard />
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

    <!-- 人工介入提示（Task 3：降级为顶部细条通知，非阻塞、不占审批焦点） -->
    <div class="ap-help-msgbar" v-if="latestHelp">
      <svg class="ap-ic ap-help-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M14.8 9.2a3 3 0 1 0-5.2 2c.9.9 1.4 1.3 1.4 2.3"/><line x1="12" y1="17.2" x2="12.01" y2="17.2"/></svg>
      <span class="ap-help-msg" :title="latestHelp.reason">
        AI 请求人工介入<template v-if="agentStore.humanHelpRequests.length > 1">（{{ agentStore.humanHelpRequests.length }} 次）</template>：{{ latestHelp.reason }}
      </span>
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

    <!-- 审批托盘已随侧栏退役移除（spec 2026-09-30-retire-ai-side-panel R1）。
         同一份 agentStore.permissionRequests 现在只有两处画法，逐条决策位只剩一个：
         过程区里的内联卡；首页那条窄条只报数、批量、追溯。批量/追溯不是丢了，是搬到
         那边 —— 侧栏不再是结算面，结算仍走 store 的 approve/deny。 -->

    <!-- 可见终端执行：待确认命令 -->
    <div class="delivery-section" v-if="agentStore.pendingDeliveries.length > 0">
      <div v-for="d in agentStore.pendingDeliveries" :key="d.tool_id" class="ap-card ap-deliver">
        <div class="ap-head">
          <svg class="ap-ic ap-deliver-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
          <span class="ap-ttl">命令待确认</span>
          <span class="ap-risk-pill">将在当前终端执行</span>
        </div>
        <div class="ap-body">
          <pre class="ap-deliver-code">{{ d.command }}</pre>
          <div class="ap-actions">
            <button class="ap-btn ap-btn-grant" @click="agentStore.confirmDeliver(d.tool_id)">确认执行</button>
            <button class="ap-btn ap-btn-deny" @click="agentStore.rejectDeliver(d.tool_id)">拒绝执行</button>
          </div>
        </div>
      </div>
    </div>

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
.ap-intention { background: var(--bg-elevated, var(--bg-base)); border-color: var(--status-info-border); }
.ap-level {
  font-size: 11px;
  padding: 2px 9px;
  border-radius: 999px;
  font-weight: 600;
}
.ap-level.level-trivial,
.ap-level.level-simple { background: var(--status-success-bg); color: var(--status-success-text); }
.ap-level.level-moderate { background: var(--status-warn-bg); color: var(--status-warn-text); }
.ap-level.level-complex { background: var(--status-error-bg); color: var(--accent-orange, #f59e0b); }
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
  border-radius: 8px;
}
.ap-warning .ap-ic { margin-top: 2px; color: var(--status-warn-text); }

/* ── 人工介入提示（Task 3：降级为顶部细条通知，非阻塞） ── */
.ap-help-msgbar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--status-warn-text, #f59e0b);
  background: var(--status-warn-bg, var(--bg-elevated, var(--bg-base)));
  border: 1px solid var(--card-warn-border, rgba(245, 158, 11, 0.35));
  border-radius: 8px;
  padding: 6px 10px;
}
.ap-help-msgbar .ap-ic { flex: none; color: var(--status-warn-text, #f59e0b); }
.ap-help-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ap-help-accent { color: var(--status-warn-text, #f59e0b); }

/* 审批托盘 / 折叠队列 / 追溯区的样式随托盘一起删（R1）：它们的唯一消费方在模板里
   已经没有了。批量与追溯不是丢了，是搬进首页「待你放行」队列。 */

/* ── 命令待确认 ── */
.ap-deliver { background: var(--bg-elevated, var(--bg-base)); border-color: var(--card-warn-border); }
.ap-deliver-accent { color: var(--status-warn-text, #f59e0b); }
.ap-deliver-code {
  margin: 0 0 11px;
  background: var(--bg-input, var(--bg-base));
  border: 1px solid var(--border-default);
  border-radius: 6px;
  padding: 9px 11px;
  font-size: 11.5px;
  color: var(--accent-orange, #f59e0b);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: inherit;
}

/* ── 错误 ── */
.ap-error {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--status-error-text);
  background: var(--status-error-bg);
  padding: 9px 12px;
  border-radius: 8px;
}
.ap-error .ap-ic { color: currentColor; }
</style>

