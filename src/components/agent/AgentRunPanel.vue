<script setup lang="ts">
/**
 * AgentRunPanel — Agent 运行过程通知面板（异常/需要用户决策时显示）
 *
 * 实际包含的区块（均为条件渲染，无内容时整块隐藏）:
 * - 任务规划（意图分类：级别/任务类型/思考预算）
 * - 警告（warnings）
 * - 人工介入求助卡片（AI 请求帮助，可忽略）
 * - 工具授权请求（允许本次/始终允许/忽略，命令工具绑定命令哈希）
 * - 可见终端执行：待确认命令（确认/拒绝）与在途交付状态徽章
 * - 错误（errorMessage）
 *
 * 注：常规运行状态（thinking/acting、迭代计数、工具调用流）不在本面板展示 —
 * 工具调用明细见消息气泡内联轨迹（AgentTraceInline / ToolOperationList）。
 */
import { useAgentStore } from '@/stores/agent'
import { agentApi } from '@/api/client'
import { useToastStore } from '@/stores/toast'
import ClarifyingWizard from './ClarifyingWizard.vue'

const agentStore = useAgentStore()
const toast = useToastStore()

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

/** 可见终端交付状态的人读文案 */
function deliveryStatusLabel(status: string): string {
  const map: Record<string, string> = {
    capturing: '执行中',
    done: '已完成',
    rejected: '已拒绝',
    aborted: '已中止',
    writing: '写入中',
  }
  return map[status] || status
}

/** 授权（审查 M-3：提供"允许本次"(once) 与"始终允许"(always) 两档；
 *  命令工具始终允许时绑定该条具体命令，不会覆盖其他 dangerous 命令） */
async function grantPermission(reqId: string, tool: string, scope: 'once' | 'always') {
  const req = agentStore.permissionRequests.find(r => r.id === reqId)
  if (!req || req.granting || req.resolved) return
  const command = typeof req.args?.command === 'string' ? (req.args.command as string) : undefined
  req.granting = true
  try {
    // 回传发起审批的 run_id / 会话 id：once 按 run 键控、session 按会话键控
    await agentApi.grantPermission(tool, scope, command, req.runId, req.conversationId)
    agentStore.resolvePermission(reqId, true)
  } catch (e) {
    req.granting = false
    console.error('授权失败:', e)
    toast.error('授权失败，请重试')
  }
}

</script>

<template>
  <ClarifyingWizard />
  <div class="agent-run-panel">
    <!-- 规划/意图信息 -->
    <div class="intention-section" v-if="agentStore.intentionInfo">
      <div class="intention-header">
        <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
        <span class="intention-title">任务规划</span>
        <span class="intention-level" :class="`level-${agentStore.intentionInfo.level}`">
          {{ levelLabel(agentStore.intentionInfo.level) }}
        </span>
      </div>
      <div class="intention-details">
        <span class="intention-tag">
          {{ taskTypeLabel(agentStore.intentionInfo.task_type) }}
        </span>
        <span class="intention-tag" v-if="agentStore.intentionInfo.allow_planning_phase">
          含规划阶段
        </span>
        <span class="intention-tag" v-if="agentStore.intentionInfo.reasoning_budget">
          思考预算 {{ agentStore.intentionInfo.reasoning_budget }} tokens
        </span>
      </div>
    </div>

    <!-- 警告 -->
    <div class="warnings-section" v-if="agentStore.warnings.length > 0">
      <div v-for="(w, index) in agentStore.warnings" :key="`warn-${index}`" class="warning-item">
        <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        {{ w }}
      </div>
    </div>

    <!-- 人工介入求助卡片 -->
    <div
      class="human-help-section"
      v-if="agentStore.hasHumanHelpRequest"
    >
      <div
        v-for="req in agentStore.humanHelpRequests"
        :key="req.id"
        class="human-help-item"
      >
        <div class="human-help-header">
          <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>
          <span class="human-help-title">AI 请求人工介入</span>
          <button
            v-if="!agentStore.isRunning"
            class="human-help-dismiss"
            title="忽略"
            aria-label="忽略该请求"
            @click="agentStore.dismissHelp(req.id)"
          >×</button>
        </div>
        <div class="human-help-body">
          <div class="human-help-reason">{{ req.reason }}</div>
          <div v-if="req.tool" class="human-help-tool">
            <span class="tool-label">工具:</span>
            <code>{{ req.tool }}</code>
            <details v-if="req.args" class="tool-args">
              <summary>参数</summary>
              <pre>{{ JSON.stringify(req.args, null, 2) }}</pre>
            </details>
          </div>
          <div class="human-help-hint">
            AI 已被引导换一种方法；如需介入请终止并修改任务描述后重试。
          </div>
        </div>
      </div>
    </div>

    <!-- 权限请求 -->
    <div
      class="permission-section"
      v-if="agentStore.permissionRequests.length > 0"
    >
      <div
        v-for="req in agentStore.permissionRequests"
        :key="req.id"
        class="permission-item"
        :class="{ 'permission-resolved': req.resolved }"
      >
        <div class="permission-header">
          <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          <span class="permission-title">工具授权请求</span>
          <span class="permission-risk" :class="`risk-${req.risk}`">{{ req.risk }}</span>
          <button
            v-if="!agentStore.isRunning && !req.resolved"
            class="permission-dismiss"
            title="忽略"
            aria-label="忽略该请求"
            @click="agentStore.dismissPermission(req.id)"
          >×</button>
        </div>
        <div class="permission-body">
          <div class="permission-message">{{ req.message }}</div>
          <div class="permission-tool">
            <span class="tool-label">工具:</span>
            <code>{{ req.tool }}</code>
            <span class="tool-label" style="margin-left: 12px;">可逆:</span>
            <span>{{ req.reversible ? '是' : '否' }}</span>
            <!-- 问题 2 改进：显示归一化命令，让用户理解授权实际绑定的命令形态 -->
            <span v-if="req.isCommandTool && req.normalizedCommand" class="tool-label" style="margin-left: 12px;">授权命令:</span>
            <code v-if="req.isCommandTool && req.normalizedCommand" class="normalized-cmd">{{ req.normalizedCommand }}</code>
            <details v-if="req.args && Object.keys(req.args).length > 0" class="tool-args">
              <summary>参数</summary>
              <pre>{{ JSON.stringify(req.args, null, 2) }}</pre>
            </details>
          </div>
        </div>
        <div v-if="req.resolved" class="permission-actions">
          <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
          <span class="permission-resolved-text">已授权，重新发起请求后生效</span>
        </div>
        <div v-else class="permission-actions">
          <button
            class="permission-btn permission-btn-grant"
            :disabled="req.granting"
            @click="grantPermission(req.id, req.tool, 'once')"
          >
            {{ req.granting ? '授权中...' : '允许本次' }}
          </button>
          <button
            class="permission-btn permission-btn-grant"
            :disabled="req.granting"
            @click="grantPermission(req.id, req.tool, 'always')"
          >
            {{ req.isCommandTool ? '始终允许此命令' : '始终允许' }}
          </button>
          <button
            class="permission-btn permission-btn-dismiss"
            @click="agentStore.dismissPermission(req.id)"
          >忽略</button>
        </div>
        <div class="permission-hint">
          <template v-if="req.isCommandTool">
            授权仅绑定此条命令，其他 dangerous 命令仍会单独请求审批；
          </template>
          授权后 AI 重新发起该请求即生效。
        </div>
      </div>
    </div>

    <!-- 可见终端执行：待确认命令 -->
    <div class="delivery-section" v-if="agentStore.pendingDeliveries.length > 0">
      <div
        v-for="d in agentStore.pendingDeliveries"
        :key="d.tool_id"
        class="delivery-item"
      >
        <div class="delivery-header">
          <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
          <span class="delivery-title">命令待确认</span>
          <span class="delivery-risk">将在当前终端执行</span>
        </div>
        <pre class="delivery-command">{{ d.command }}</pre>
        <div class="delivery-actions">
          <button
            class="delivery-btn delivery-btn-confirm"
            @click="agentStore.confirmDeliver(d.tool_id)"
          >确认执行</button>
          <button
            class="delivery-btn delivery-btn-reject"
            @click="agentStore.rejectDeliver(d.tool_id)"
          >拒绝执行</button>
        </div>
      </div>
    </div>

    <!-- 可见终端执行：在途交付状态 -->
    <div class="delivery-section" v-if="agentStore.deliveries.some(d => d.status !== 'pending_confirm')">
      <div
        v-for="d in agentStore.deliveries.filter(d => d.status !== 'pending_confirm')"
        :key="d.tool_id"
        class="delivery-badge-row"
      >
        <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
        <code class="delivery-badge-cmd">{{ d.command }}</code>
        <span
          class="delivery-badge"
          :class="`badge-${d.status}`"
        >{{ deliveryStatusLabel(d.status) }}</span>
      </div>
    </div>

    <!-- 错误 -->
    <div class="error-section" v-if="agentStore.errorMessage">
      <div class="error-item">
        <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        {{ agentStore.errorMessage }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.agent-run-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 规划/意图信息卡片 */
.intention-section {
  padding: 0 4px;
}

.intention-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--status-info-text);
  background: var(--status-info-bg);
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--status-info-border);
}

.intention-title {
  font-weight: 500;
  flex: 1;
}

.intention-level {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
}

.intention-level.level-trivial {
  background: var(--status-success-bg);
  color: var(--status-success-text);
}

.intention-level.level-simple {
  background: var(--status-success-bg);
  color: var(--status-success-text);
}

.intention-level.level-moderate {
  background: var(--status-warn-bg);
  color: var(--status-warn-text);
}

.intention-level.level-complex {
  background: var(--status-error-bg);
  color: var(--accent-orange);
}

.intention-level.level-debug {
  background: var(--status-debug-bg);
  color: var(--status-debug-text);
}

.intention-details {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  padding: 0 4px;
}

.intention-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--status-tag-bg);
  color: var(--status-tag-text);
  border: 1px solid var(--status-tag-border);
}

.warnings-section,
.error-section,
.human-help-section,
.permission-section {
  padding: 0 4px;
}

.status-icon {
  flex-shrink: 0;
  vertical-align: -2px;
}

.warning-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--status-warn-text);
  background: var(--status-warn-bg);
  padding: 6px 12px;
  border-radius: 6px;
  margin-bottom: 4px;
}

.error-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--status-error-text);
  background: var(--status-error-bg);
  padding: 6px 12px;
  border-radius: 6px;
}

.human-help-item {
  font-size: 13px;
  color: var(--status-warn-text);
  background: var(--card-warn-bg);
  border: 1px solid var(--card-warn-border);
  padding: 8px 12px;
  border-radius: 6px;
  margin-bottom: 6px;
}



.human-help-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.human-help-title {
  font-weight: 600;
  color: var(--status-warn-text);
  flex: 1;
}

.human-help-dismiss {
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  padding: 0 4px;
}

.human-help-dismiss:hover {
  color: var(--status-warn-text);
}

.human-help-reason {
  color: var(--text-primary);
  margin-bottom: 6px;
  line-height: 1.5;
}

.human-help-tool {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.human-help-tool .tool-label {
  color: var(--text-tertiary);
  margin-right: 4px;
}

.human-help-tool code {
  background: var(--bg-base);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--accent-orange);
  font-size: 12px;
}

.tool-args {
  margin-top: 4px;
}

.tool-args summary {
  cursor: pointer;
  color: var(--text-tertiary);
  font-size: 11px;
}

.tool-args pre {
  margin-top: 4px;
  background: var(--bg-base);
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 11px;
  color: var(--text-secondary);
  overflow-x: auto;
  white-space: pre-wrap;
}

.human-help-hint {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 6px;
  font-style: italic;
}

/* ── 权限请求卡片 ── */
.permission-item {
  font-size: 13px;
  color: var(--card-perm-text);
  background: var(--card-perm-bg);
  border: 1px solid var(--card-perm-border);
  padding: 8px 12px;
  border-radius: 6px;
  margin-bottom: 6px;
}

.permission-item.permission-resolved {
  opacity: 0.6;
  border-color: var(--card-perm-resolved);
}

.permission-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.permission-title {
  font-weight: 600;
  color: var(--card-perm-text);
  flex: 1;
}

.permission-risk {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 600;
  text-transform: uppercase;
}

.permission-risk.risk-moderate {
  background: var(--card-perm-border);
  color: var(--card-perm-text);
}

.permission-risk.risk-dangerous {
  background: var(--danger-bg);
  color: var(--danger-text);
}

.permission-dismiss {
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  padding: 0 4px;
}

.permission-dismiss:hover {
  color: var(--card-perm-text);
}

.permission-body {
  margin-bottom: 8px;
}

.permission-message {
  color: var(--text-primary);
  margin-bottom: 4px;
  line-height: 1.5;
}

.permission-tool {
  font-size: 12px;
  color: var(--text-secondary);
}

.permission-tool .tool-label {
  color: var(--text-tertiary);
  margin-right: 4px;
}

.permission-tool code {
  background: var(--bg-base);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--accent-orange);
  font-size: 12px;
}

.permission-tool code.normalized-cmd {
  color: var(--accent-cyan, #7aa2d0);
  font-style: italic;
}

.permission-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}

.permission-btn {
  font-size: 12px;
  padding: 4px 12px;
  border-radius: 4px;
  border: 1px solid;
  cursor: pointer;
  transition: all 0.15s;
}

.permission-btn-grant {
  background: var(--card-perm-btn-bg);
  border-color: var(--card-perm-btn-border);
  color: var(--card-perm-btn-text);
}

.permission-btn-grant:hover:not(:disabled) {
  background: var(--card-perm-btn-hover);
}

.permission-btn-grant:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.permission-btn-dismiss {
  background: transparent;
  border-color: var(--border-default);
  color: var(--text-secondary);
}

.permission-btn-dismiss:hover {
  border-color: var(--text-tertiary);
  color: var(--text-primary);
}

.permission-resolved-text {
  font-size: 12px;
  color: var(--status-success-text);
  font-style: italic;
}

.permission-hint {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 4px;
  font-style: italic;
}

/* ── 可见终端执行 · 待确认命令 ── */
.delivery-section {
  padding: 0 4px;
}

.delivery-item {
  font-size: 13px;
  color: var(--card-perm-text);
  background: var(--card-warn-bg);
  border: 1px solid var(--card-warn-border);
  padding: 8px 12px;
  border-radius: 6px;
  margin-bottom: 6px;
}

.delivery-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.delivery-title {
  font-weight: 600;
  color: var(--status-warn-text);
  flex: 1;
}

.delivery-risk {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--danger-bg);
  color: var(--danger-text);
}

.delivery-command {
  margin: 0 0 8px;
  background: var(--bg-base);
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12px;
  color: var(--accent-orange);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.delivery-actions {
  display: flex;
  gap: 8px;
}

.delivery-btn {
  font-size: 12px;
  padding: 4px 14px;
  border-radius: 4px;
  border: 1px solid;
  cursor: pointer;
  transition: all 0.15s;
}

.delivery-btn-confirm {
  background: var(--status-success-bg);
  border-color: var(--status-success-border, var(--border-default));
  color: var(--status-success-text);
}

.delivery-btn-confirm:hover {
  filter: brightness(1.05);
}

.delivery-btn-reject {
  background: transparent;
  border-color: var(--border-default);
  color: var(--text-secondary);
}

.delivery-btn-reject:hover {
  border-color: var(--danger-text);
  color: var(--danger-text);
}

.delivery-badge-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--card-warn-bg);
  border: 1px solid var(--card-warn-border);
  padding: 4px 10px;
  border-radius: 6px;
  margin-bottom: 4px;
}

.delivery-badge-cmd {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--accent-orange);
  font-size: 12px;
}

.delivery-badge {
  flex-shrink: 0;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  font-weight: 600;
}

.badge-capturing,
.badge-writing {
  background: var(--status-warn-bg);
  color: var(--status-warn-text);
}

.badge-done {
  background: var(--status-success-bg);
  color: var(--status-success-text);
}

.badge-rejected,
.badge-aborted {
  background: var(--status-error-bg);
  color: var(--status-error-text);
}
</style>
