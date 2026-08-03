<script setup lang="ts">
/**
 * AgentRunPanel — Agent 运行过程可视化面板
 *
 * 仅显示 Agent 运行过程中的实时信息:
 * - 状态指示器（thinking / acting / observing）
 * - 迭代计数 + 终止按钮
 * - 工具调用卡片
 * - 警告 / 人工介入 / 权限请求 / 错误
 *
 * 运行结束后的成本统计与完整轨迹通过消息气泡上的"轨迹"按钮访问 TraceViewer。
 */
import { useAgentStore } from '@/stores/agent'
import { agentApi } from '@/api/client'

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

const props = defineProps<{
  /** 当审查面板已展示人工介入状态时，隐藏此处的人工介入卡片 */
  hideHumanHelp?: boolean
}>()

/** 永久授权工具 */
async function grantAlways(reqId: string, tool: string) {
  const req = agentStore.permissionRequests.find(r => r.id === reqId)
  if (!req || req.granting || req.resolved) return
  req.granting = true
  try {
    await agentApi.grantPermission(tool, 'always')
    agentStore.resolvePermission(reqId, true)
  } catch (e) {
    req.granting = false
    console.error('授权失败:', e)
  }
}

</script>

<template>
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

    <!-- 人工介入求助（运行中/非 IDE 模式显示完整卡片；IDE 模式由 CodeChangeReviewPanel 统一展示，此处仅保留关闭入口） -->
    <div
      class="human-help-section"
      v-if="agentStore.hasHumanHelpRequest && !props.hideHumanHelp"
    >
      <div
        v-for="req in agentStore.humanHelpRequests"
        :key="req.id"
        class="human-help-item"
      >
        <div class="human-help-header">
          <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>
          <span class="human-help-title">Agent 请求人工介入</span>
          <button
            v-if="!agentStore.isRunning"
            class="human-help-dismiss"
            title="忽略"
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
            Agent 已被引导换一种方法；如需介入请终止并修改任务描述后重试。
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
            @click="grantAlways(req.id, req.tool)"
          >
            {{ req.granting ? '授权中...' : '永久允许' }}
          </button>
          <button
            class="permission-btn permission-btn-dismiss"
            @click="agentStore.dismissPermission(req.id)"
          >忽略</button>
        </div>
        <div class="permission-hint">
          授权后下次运行生效；Agent 已被引导换用更安全的工具。
        </div>
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
  color: #569cd6;
  background: #1a2d3d;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid #2d4a5a;
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
  background: #2d3d2d;
  color: #6abf6a;
}

.intention-level.level-simple {
  background: #2d3d2d;
  color: #6abf6a;
}

.intention-level.level-moderate {
  background: #3d3d2d;
  color: #dcdcaa;
}

.intention-level.level-complex {
  background: #3d2d2d;
  color: #ce9178;
}

.intention-level.level-debug {
  background: #2d2d3d;
  color: #9cdcfe;
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
  background: rgba(86, 156, 214, 0.1);
  color: #8b949e;
  border: 1px solid rgba(86, 156, 214, 0.2);
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
  color: #dcdcaa;
  background: #2d2d1a;
  padding: 6px 12px;
  border-radius: 6px;
  margin-bottom: 4px;
}

.error-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #bf6a6a;
  background: #2d1a1a;
  padding: 6px 12px;
  border-radius: 6px;
}

.human-help-item {
  font-size: 13px;
  color: #dcdcaa;
  background: #2d2a1a;
  border: 1px solid #4a4533;
  padding: 8px 12px;
  border-radius: 6px;
  margin-bottom: 6px;
}

.human-help-item.compact {
  padding: 6px 10px;
  margin-bottom: 4px;
}

.human-help-item.compact .human-help-header {
  margin-bottom: 0;
}

.human-help-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.human-help-title {
  font-weight: 600;
  color: #dcdcaa;
  flex: 1;
}

.human-help-dismiss {
  background: transparent;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  padding: 0 4px;
}

.human-help-dismiss:hover {
  color: #dcdcaa;
}

.human-help-reason {
  color: #ccc;
  margin-bottom: 6px;
  line-height: 1.5;
}

.human-help-tool {
  font-size: 12px;
  color: #999;
  margin-bottom: 4px;
}

.human-help-tool .tool-label {
  color: #888;
  margin-right: 4px;
}

.human-help-tool code {
  background: var(--bg-base);
  padding: 1px 6px;
  border-radius: 3px;
  color: #ce9178;
  font-size: 12px;
}

.tool-args {
  margin-top: 4px;
}

.tool-args summary {
  cursor: pointer;
  color: #888;
  font-size: 11px;
}

.tool-args pre {
  margin-top: 4px;
  background: var(--bg-base);
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 11px;
  color: #aaa;
  overflow-x: auto;
  white-space: pre-wrap;
}

.human-help-hint {
  font-size: 11px;
  color: #777;
  margin-top: 6px;
  font-style: italic;
}

/* ── 权限请求卡片 ── */
.permission-item {
  font-size: 13px;
  color: #e6c07b;
  background: #2d221a;
  border: 1px solid #4a3a23;
  padding: 8px 12px;
  border-radius: 6px;
  margin-bottom: 6px;
}

.permission-item.permission-resolved {
  opacity: 0.6;
  border-color: #4a8a4a;
}

.permission-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.permission-title {
  font-weight: 600;
  color: #e6c07b;
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
  background: #4a3a23;
  color: #e6c07b;
}

.permission-risk.risk-dangerous {
  background: #4a1a1a;
  color: #ff6b6b;
}

.permission-dismiss {
  background: transparent;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  padding: 0 4px;
}

.permission-dismiss:hover {
  color: #e6c07b;
}

.permission-body {
  margin-bottom: 8px;
}

.permission-message {
  color: #ccc;
  margin-bottom: 4px;
  line-height: 1.5;
}

.permission-tool {
  font-size: 12px;
  color: #999;
}

.permission-tool .tool-label {
  color: #888;
  margin-right: 4px;
}

.permission-tool code {
  background: var(--bg-base);
  padding: 1px 6px;
  border-radius: 3px;
  color: #ce9178;
  font-size: 12px;
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
  background: #3a5a3a;
  border-color: #4a8a4a;
  color: #b0e0b0;
}

.permission-btn-grant:hover:not(:disabled) {
  background: #4a6a4a;
}

.permission-btn-grant:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.permission-btn-dismiss {
  background: transparent;
  border-color: #555;
  color: #999;
}

.permission-btn-dismiss:hover {
  border-color: #777;
  color: #ccc;
}

.permission-resolved-text {
  font-size: 12px;
  color: #6abf6a;
  font-style: italic;
}

.permission-hint {
  font-size: 11px;
  color: #777;
  margin-top: 4px;
  font-style: italic;
}
</style>
