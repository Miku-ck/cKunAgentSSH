<template>
  <div v-if="gp.hasContent" class="gp-panel" data-testid="goal-plan-progress">
    <!-- 目标卡：陈述 + 逐条标准勾选态（裁决语义由徽标呈现，只读） -->
    <div v-if="gp.snapshot.goal" class="gp-goal">
      <div class="gp-goal-head">
        <span class="gp-badge" :class="goalBadgeClass">{{ goalStatusLabel }}</span>
        <h3 class="gp-goal-title" :title="gp.snapshot.goal.statement">
          {{ gp.snapshot.goal.title || gp.snapshot.goal.statement }}
        </h3>
        <span class="gp-ver">v{{ gp.snapshot.goal.version }}</span>
      </div>
      <p class="gp-statement">{{ gp.snapshot.goal.statement }}</p>
      <ul v-if="gp.snapshot.goal.criteria.length" class="gp-criteria">
        <li
          v-for="c in gp.snapshot.goal.criteria"
          :key="c.id"
          class="gp-criterion"
          :class="{ met: c.met }"
          :title="c.evidence || c.text"
        >
          <span class="gp-tick">{{ c.met ? '✓' : '○' }}</span>
          <span class="gp-criterion-text">{{ c.text }}</span>
        </li>
      </ul>
    </div>

    <!-- 计划卡：状态徽标 + 进度条 + 步骤列表 + 用户侧动作 -->
    <div v-if="gp.snapshot.plan" class="gp-plan">
      <div class="gp-plan-head">
        <span class="gp-badge" :class="planBadgeClass">{{ planStatusLabel }}</span>
        <span class="gp-plan-title">计划 v{{ gp.snapshot.plan.version }}</span>
        <span class="gp-progress-num">{{ gp.snapshot.progress.completed }}/{{ gp.snapshot.progress.total }}</span>
        <div class="gp-actions">
          <button
            v-if="gp.awaitingApproval"
            class="gp-btn primary"
            :disabled="gp.busy"
            @click="onApprove"
          >批准计划</button>
          <template v-if="gp.planActionable && !gp.awaitingApproval">
            <button class="gp-btn" :disabled="gp.busy" @click="onReplan">重置</button>
            <button class="gp-btn danger" :disabled="gp.busy" @click="onAbandon">放弃</button>
          </template>
        </div>
      </div>
      <div class="gp-bar" role="progressbar" :aria-valuenow="gp.snapshot.progress.completed"
           aria-valuemin="0" :aria-valuemax="gp.snapshot.progress.total">
        <div class="gp-bar-fill" :style="{ width: progressPct + '%' }" />
      </div>
      <ol class="gp-steps">
        <li
          v-for="s in gp.snapshot.steps"
          :key="s.step_id"
          class="gp-step"
          :class="s.status"
        >
          <span class="gp-step-mark">{{ statusMark(s.status) }}</span>
          <span class="gp-step-title">{{ s.ordinal }}. {{ s.title }}</span>
          <span v-if="s.evidence" class="gp-evidence" :title="s.evidence">{{ s.evidence }}</span>
          <span v-if="s.blocked_reason" class="gp-blocked" :title="s.blocked_reason">
            受阻：{{ s.blocked_reason }}
          </span>
        </li>
      </ol>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useConfirm } from '@/composables/useConfirm'
import { useGoalPlanStore } from '@/stores/goal-plan'
import type { GoalStatus, PlanStatus, StepStatus } from '@/utils/goal-plan-core'

const gp = useGoalPlanStore()
const { confirm } = useConfirm()

const GOAL_LABELS: Record<GoalStatus, string> = {
  draft: '草案', active: '进行中', achieved: '已达成', revised: '已修订', abandoned: '已放弃',
}
const PLAN_LABELS: Record<PlanStatus, string> = {
  draft: '草案', awaiting_approval: '等待批准', approved: '已批准', active: '执行中',
  superseded: '已重置', completed: '已完成', abandoned: '已放弃',
}

const goalStatusLabel = computed(() =>
  gp.snapshot.goal ? GOAL_LABELS[gp.snapshot.goal.status] ?? gp.snapshot.goal.status : '')
const planStatusLabel = computed(() =>
  gp.snapshot.plan ? PLAN_LABELS[gp.snapshot.plan.status] ?? gp.snapshot.plan.status : '')

const goalBadgeClass = computed(() => ({
  achieved: 'ok', active: 'run', abandoned: 'off', revised: 'off', draft: 'wait',
}[gp.snapshot.goal?.status ?? 'draft'] ?? 'wait'))
const planBadgeClass = computed(() => ({
  awaiting_approval: 'wait', approved: 'ok', active: 'run',
  completed: 'ok', superseded: 'off', abandoned: 'off', draft: 'wait',
}[gp.snapshot.plan?.status ?? 'draft'] ?? 'wait'))

const progressPct = computed(() => {
  const total = gp.snapshot.progress.total
  return total > 0 ? Math.round((gp.snapshot.progress.completed / total) * 100) : 0
})

function statusMark(status: StepStatus): string {
  return { pending: '○', in_progress: '▶', completed: '✓', blocked: '!', skipped: '−' }[status]
}

async function onApprove(): Promise<void> {
  const planId = gp.snapshot.plan?.plan_id
  if (!planId) return
  const ok = await confirm({
    title: '批准计划',
    message: '批准后模型将按步骤执行（写操作解除只读强制）。确认批准？',
    confirmText: '批准',
  })
  if (ok) void gp.approvePlan(planId)
}

async function onReplan(): Promise<void> {
  const planId = gp.snapshot.plan?.plan_id
  if (!planId) return
  const ok = await confirm({
    title: '重置计划',
    message: '重置后当前计划归档，已完成步骤与证据保留，模型将重新提出计划。确认重置？',
    confirmText: '重置',
  })
  if (ok) void gp.replanPlan(planId)
}

async function onAbandon(): Promise<void> {
  const planId = gp.snapshot.plan?.plan_id
  if (!planId) return
  const ok = await confirm({
    title: '放弃计划',
    message: '放弃后本计划终止且不可恢复。确认放弃？',
    confirmText: '放弃',
    danger: true,
  })
  if (ok) void gp.abandonPlan(planId)
}
</script>

<style scoped>
/* 输入区上方的会话级进度面板：与 ApprovalDock 同层级、同为「不属于消息流」
   的常驻卡。颜色全走令牌（DESIGN.md），强调用 --accent-primary（主题感知强调色）。 */
.gp-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-8);
  margin-bottom: var(--spacing-8);
  padding: var(--spacing-10) var(--spacing-12);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}

.gp-goal-head,
.gp-plan-head {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
}

.gp-goal-title {
  margin: 0;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.9231rem;
  font-weight: 600;
  color: var(--text-primary);
}

.gp-plan-title {
  flex: 1;
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-weight: 600;
}

.gp-ver {
  color: var(--text-tertiary);
  font-size: 0.8462rem;
}

.gp-badge {
  flex-shrink: 0;
  padding: 1px var(--spacing-8);
  border-radius: var(--radius-pill);
  font-size: 0.7692rem;
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}

.gp-badge.wait { border-color: var(--status-warn); color: var(--status-warn); }
.gp-badge.ok { border-color: var(--status-success); color: var(--status-success); }
.gp-badge.run { border-color: var(--accent-primary); color: var(--accent-text); }
.gp-badge.off { color: var(--text-tertiary); }

.gp-statement {
  margin: var(--spacing-6) 0 0;
  color: var(--text-secondary);
  font-size: 0.8462rem;
  line-height: var(--line-height-normal);
}

.gp-criteria {
  list-style: none;
  margin: var(--spacing-6) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
}

.gp-criterion {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-6);
  color: var(--text-secondary);
  font-size: 0.8462rem;
}

.gp-criterion.met { color: var(--text-primary); }
.gp-criterion.met .gp-tick { color: var(--status-success); }

.gp-tick { flex-shrink: 0; }

.gp-criterion-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gp-progress-num {
  flex-shrink: 0;
  color: var(--text-tertiary);
  font-size: 0.8462rem;
  font-variant-numeric: tabular-nums;
}

.gp-actions {
  display: flex;
  gap: var(--spacing-6);
  flex-shrink: 0;
}

.gp-btn {
  padding: 2px var(--spacing-10);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.8462rem;
  cursor: pointer;
}

.gp-btn:hover:not(:disabled) { border-color: var(--accent-primary); color: var(--text-primary); }
.gp-btn:disabled { opacity: 0.5; cursor: default; }

.gp-btn.primary {
  border-color: var(--accent-primary);
  background: var(--accent-primary);
  color: var(--accent-on);
}

.gp-btn.danger:hover:not(:disabled) {
  border-color: var(--danger-border);
  color: var(--danger-text);
}

.gp-bar {
  height: 4px;
  margin-top: var(--spacing-8);
  border-radius: var(--radius-pill);
  background: var(--bg-input);
  overflow: hidden;
}

.gp-bar-fill {
  height: 100%;
  border-radius: var(--radius-pill);
  background: var(--accent-primary);
  transition: width var(--transition-fast);
}

.gp-steps {
  list-style: none;
  margin: var(--spacing-8) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
}

.gp-step {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-6);
  font-size: 0.8462rem;
  color: var(--text-secondary);
}

.gp-step.completed { color: var(--text-tertiary); }
.gp-step.completed .gp-step-mark { color: var(--status-success); }
.gp-step.in_progress { color: var(--text-primary); }
.gp-step.in_progress .gp-step-mark { color: var(--accent-text); }
.gp-step.blocked { color: var(--status-warn); }
.gp-step.skipped .gp-step-mark { color: var(--text-tertiary); }

.gp-step-title { flex-shrink: 0; }

.gp-evidence,
.gp-blocked {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gp-evidence { color: var(--text-tertiary); }
.gp-blocked { color: var(--status-warn); }
</style>

