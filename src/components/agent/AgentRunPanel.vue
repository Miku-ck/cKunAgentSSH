<script setup lang="ts">
/**
 * AgentRunPanel — Agent 运行过程通知面板（异常/需要用户决策时显示）
 *
 * 实际包含的区块（均为条件渲染，无内容时整块隐藏）:
 * - 澄清向导（ClarifyingWizard：自带 visible 门控 + Teleport，不受下方整块开关影响）
 * - 任务规划（意图分类：级别/任务类型/思考预算）
 * - 警告（warnings）
 * - 人工介入求助卡片（AI 请求帮助，可忽略）
 * - 工具授权请求（允许本次/本会话允许/始终允许/忽略，命令工具绑定命令哈希；
 *   session 档只对非 dangerous 卡开放，口径见 utils/approval-scopes.ts）
 * - 可见终端执行：待确认命令（确认/拒绝）
 * - 错误（errorMessage）
 *
 * 整块的显隐由 agentStore.hasRunNotice 决定（数据侧单一判定，逐区块对齐）——
 * 宿主组件不要再抄一份条件：漏掉一项就等于那块内容永远不挂载。
 *
 * 注：常规运行状态（thinking/acting、迭代计数、工具调用流）不在本面板展示 —
 * 工具调用明细见消息气泡内联轨迹（AgentTraceInline）与运行交付面板。
 */
import { computed, ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agent'
// 展示口径与首页待放行队列共用一份（别名保持模板调用点不变）
import {
  approvalCommand as cardCommand,
  approvalRiskLabel as riskLabel,
} from '@/utils/approval-display'
import { useApprovalActions } from '@/composables/useApprovalActions'
import ApprovalCard from './ApprovalCard.vue'
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

/** 结算的用户可见层与首页待放行队列共用一份（别名保持模板调用点不变） */
const { approve: grantPermission, deny: denyRequest } = useApprovalActions()

// ── 单一焦点审批队列（spec refactor-approval-center） ──
const pendingPerms = computed(() => agentStore.pendingPermissions)
/** 已解决（已授权/已拒绝）项（追溯区）；expired 卡按裁决A留在可操作队列，不进追溯 */
const resolvedPerms = computed(() => agentStore.permissionRequests.filter(r => r.resolved))

// 焦点在待审批队列内的索引；队列收缩/触发时钳制到合法范围
const focusIdx = ref(0)
watch(() => pendingPerms.value.length, (n) => {
  if (focusIdx.value >= n) focusIdx.value = Math.max(0, n - 1)
})
/** 焦点卡 = 待审批队列里最旧一条（队首），随结算自动向前 */
const focusPerm = computed(() => pendingPerms.value[focusIdx.value] ?? pendingPerms.value[0] ?? null)
/** 折叠的排队数（除焦点外其余待处理） */
const queueCount = computed(() => Math.max(0, pendingPerms.value.length - 1))
/** 待处理队列折叠列表是否展开 */
const queueOpen = ref(false)

function focusNext() {
  const n = pendingPerms.value.length
  if (n <= 1) return
  focusIdx.value = (focusIdx.value + 1) % n
}
function focusPrev() {
  const n = pendingPerms.value.length
  if (n <= 1) return
  focusIdx.value = (focusIdx.value - 1 + n) % n
}
function focusAt(i: number) {
  if (i >= 0 && i < pendingPerms.value.length) focusIdx.value = i
}

// 批量审批：快照待审批 id 再逐条按正常授权/拒绝流程结算（grantPermission /
// denyRequest 自身幂等：resolved/settling 会自动跳过）
const batchBusy = ref(false)
async function grantAll() {
  if (batchBusy.value || pendingPerms.value.length === 0) return
  batchBusy.value = true
  for (const req of [...pendingPerms.value]) {
    await grantPermission(req.id, 'once')
  }
  batchBusy.value = false
}
async function denyAll() {
  if (batchBusy.value || pendingPerms.value.length === 0) return
  batchBusy.value = true
  for (const req of [...pendingPerms.value]) {
    await denyRequest(req.id)
  }
  batchBusy.value = false
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

    <!-- 权限请求：审批托盘之一。"单一焦点"这句已经不适用 —— 同一份
         agentStore.permissionRequests 现在有三个入口（这里 / 首页待放行队列 /
         运行视图内联卡），三处渲染的是同一对象、结算走同一个 approve/deny，
         所以不矛盾；要不要在 run 标签活动时段落隐去这层是待裁决 C2。 -->
    <div class="permission-section" v-if="agentStore.permissionRequests.length > 0">
      <!-- 托盘头：汇总计数 + 人工介入角标 + 批量操作 -->
      <div class="ap-perm-tray">
        <svg class="ap-ic ap-perm-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        <span class="ap-ttl ap-perm-ttl">审批队列</span>
        <span class="ap-tray-count">{{ pendingPerms.length }} 待处理</span>
        <span
          v-if="agentStore.humanHelpRequests.length > 0"
          class="ap-badge ap-badge-warn"
          role="status"
        >{{ agentStore.humanHelpRequests.length }} 次人工介入</span>
        <span class="ap-sp"></span>
        <button class="ap-btn ap-btn-grant" :disabled="batchBusy || pendingPerms.length === 0" @click="grantAll">
          {{ batchBusy ? '处理中...' : `全部允许本次（${pendingPerms.length}）` }}
        </button>
        <button class="ap-btn ap-btn-deny" :disabled="batchBusy || pendingPerms.length === 0" @click="denyAll">
          {{ batchBusy ? '处理中...' : `全部拒绝（${pendingPerms.length}）` }}
        </button>
      </div>

      <!-- 单一焦点卡：最旧一条未处理（卡体与运行视图内联审批共用一个组件） -->
      <ApprovalCard
        v-if="focusPerm"
        :req="focusPerm"
        :nav-count="pendingPerms.length"
        @prev="focusPrev"
        @next="focusNext"
      />

      <!-- 折叠的排队项（+N 条待处理） -->
      <div v-if="queueCount > 0" class="ap-queue">
        <button class="ap-queue-toggle" :aria-expanded="queueOpen" @click="queueOpen = !queueOpen">
          <svg v-if="queueOpen" class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 15 12 9 18 15"/></svg>
          <svg v-else class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          <span v-if="queueOpen">收起待处理</span>
          <span v-else>+{{ queueCount }} 条待处理</span>
        </button>
        <div v-if="queueOpen" class="ap-queue-list">
          <button
            v-for="(req, i) in pendingPerms"
            :key="req.id"
            class="ap-queue-item"
            :class="{ 'is-focus': i === focusIdx }"
            @click="focusAt(i)"
          >
            <span class="ap-queue-ic" :class="`risk-${req.risk}`">!</span>
            <code class="ap-queue-cmd" :title="cardCommand(req) || req.tool">{{ cardCommand(req) || req.tool }}</code>
            <span class="ap-risk ap-risk-mini" :class="`risk-${req.risk}`">{{ riskLabel(req.risk) }}</span>
          </button>
        </div>
      </div>

      <!-- 追溯区：已解决（不删数据）；超时卡仍在可操作队列 -->
      <details v-if="resolvedPerms.length > 0" class="ap-trace">
        <summary>
          <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          历史记录（{{ resolvedPerms.length }} 已处理）
        </summary>
        <div v-for="req in resolvedPerms" :key="req.id" class="ap-trace-row">
          <code class="ap-trace-cmd" :title="cardCommand(req) || req.tool">{{ cardCommand(req) || req.tool }}</code>
          <span v-if="req.granted" class="ap-trace-status" :class="`env-${req.environment || 'test'}`">
            <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            已授权
          </span>
          <span v-else class="ap-trace-status ap-trace-denied">
            <svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            已拒绝
          </span>
        </div>
      </details>
    </div>

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

/* ── 审批托盘头（Task 2：批量操作 + 汇总计数 + 人工介入角标） ── */
.ap-perm-tray {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 13px;
  background: var(--bg-elevated, var(--bg-input, var(--bg-base)));
  border: 1px solid var(--border-default);
  border-radius: 10px;
}
.ap-perm-tray .ap-ic { flex: none; }
.ap-tray-count {
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--card-perm-border, var(--border-default));
  padding: 2px 9px;
  border-radius: 999px;
}
.ap-perm-tray .ap-btn { padding: 4px 11px; }

/* ── 折叠的排队项（+N 条待处理） ── */
.ap-queue { display: flex; flex-direction: column; gap: 4px; }
.ap-queue-toggle {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--bg-input, var(--bg-base));
  border: 1px solid var(--border-default);
  border-radius: 7px;
  padding: 5px 11px;
  cursor: pointer;
  transition: background 0.18s, color 0.18s;
}
.ap-queue-toggle:hover { background: var(--bg-hover, var(--bg-input)); color: var(--text-primary); }
.ap-queue-toggle .ap-ic { width: 12px; height: 12px; }
.ap-queue-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 5px;
  background: var(--bg-elevated, var(--bg-base));
}
.ap-queue-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: inherit;
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 6px 9px;
  cursor: pointer;
  transition: background 0.18s, border-color 0.18s;
}
.ap-queue-item:hover { background: var(--bg-hover, var(--bg-input)); }
.ap-queue-item.is-focus {
  background: var(--bg-input, var(--card-perm-bg));
  border-color: var(--card-perm-border, var(--border-default));
}
.ap-queue-ic {
  flex: none;
  width: 16px;
  height: 16px;
  display: grid;
  place-items: center;
  font-weight: 800;
  font-size: 10px;
  border-radius: 50%;
}
.ap-queue-ic.risk-dangerous { background: var(--danger-bg); color: var(--danger-text); }
.ap-queue-ic.risk-moderate { background: var(--status-warn-bg); color: var(--status-warn-text); }
.ap-queue-ic.risk-safe { background: var(--status-success-bg); color: var(--status-success-text); }
.ap-queue-cmd {
  flex: 1;
  min-width: 0;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ap-risk-mini { font-size: 9.5px; padding: 1px 7px; }

/* ── 追溯区：超时保留 + 已解决 ── */
.ap-trace {
  border: 1px solid var(--border-default);
  border-radius: 8px;
  background: var(--bg-input, var(--bg-base));
}
.ap-trace summary {
  cursor: pointer;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 11px;
  color: var(--text-tertiary);
  font-size: 11px;
}
.ap-trace summary::-webkit-details-marker { display: none; }
.ap-trace summary .ap-ic { width: 13px; height: 13px; }
.ap-trace-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 11px;
  border-top: 1px solid var(--border-default);
}
.ap-trace-cmd {
  flex: 1;
  min-width: 0;
  font-family: inherit;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ap-trace-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
}
.ap-trace-status .ap-ic { width: 12px; height: 12px; }
.ap-trace-denied { color: var(--danger-text); }

/* ── 折叠的排队项摘要（卡体样式见 ApprovalCard / approval-card.css） ── */

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

/* ── 徽章：圆点前缀 + 文字 + 配色三重传达（不依赖单一颜色） ── */
.ap-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.4px;
  padding: 2px 9px;
  border-radius: 999px;
  line-height: 1;
}
.ap-badge::before {
  content: "";
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.ap-badge-warn { color: var(--status-warn-text); background: var(--status-warn-bg); }

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

