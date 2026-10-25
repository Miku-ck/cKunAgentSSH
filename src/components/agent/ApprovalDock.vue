<script setup lang="ts">
/**
 * ApprovalDock — 审批决策面（控制面），挂在输入区上方
 *
 * 审批是运行期的瞬时决策，不是执行记录：请求不写入 agentTrace / agent_runs，
 * run 收尾四条出口（done/error/abort/_reset）都会清空。所以它不能跟着消息流
 * 滚动 —— 用户往上翻历史时，待决的那条仍要停在输入框上方。
 *
 * 执行过程本身归运维轨迹（DB 权威记录），那里按 run_id 回放；本组件只负责
 * 「现在要你拍板」这一件事：权限审批逐条复用 ApprovalCard，终端交付逐条复用
 * DeliveryCard，批量只结算权限请求。
 */
import { computed, ref } from 'vue'
import { useAgentStore } from '@/stores/agent'
import { useApprovalActions } from '@/composables/useApprovalActions'
import { batchApproveBlocked, onceGrantBlocked } from '@/utils/approval-scopes'
import ApprovalCard from './ApprovalCard.vue'
import DeliveryCard from './DeliveryCard.vue'
import ClarifyingWizard from './ClarifyingWizard.vue'

const agent = useAgentStore()
const { settleAll } = useApprovalActions()

const pending = computed(() => agent.pendingPermissions)
const deliveries = computed(() => agent.pendingDeliveries)
const wizard = computed(() => agent.clarificationWizard)
// Q-F6 恢复入口：结果回传自动重试耗尽（postFailed）的交付已是终态，不在
// pendingDeliveries 里 —— 不在这里给它们一席之地，用户界面上就没有任何
// 可操作的恢复入口（后端挂起的循环只能干等 TTL）。卡内由 DeliveryCard
// 渲染「重发结果」按钮，调 useAgentDeliver.retryPostResult。
const failedPostDeliveries = computed(() =>
  agent.deliveries.filter(d => d.postFailed === true),
)
const batching = ref(false)
const onceBlocked = computed(() => onceGrantBlocked(agent.isRunning))
/** 这批里有一条不能一键放行，整批都不给「全部批准」 */
const batchBlocked = computed(() => batchApproveBlocked(pending.value))
const batchBlockedReason = computed(() => {
  if (onceBlocked.value) return '本轮运行已结束，once 授权无处生效'
  if (batchBlocked.value) return '队列里有高危、生产环境或风险未知的操作，只能逐条确认'
  return ''
})

async function batch(kind: 'approve' | 'deny') {
  if (batching.value) return
  // 按钮 disabled 挡的是点击，这里挡的是状态在渲染后又变脏（新卡到达/环境被改写）
  if (kind === 'approve' && (batchBlocked.value || onceBlocked.value)) return
  batching.value = true
  try {
    await settleAll(kind)
  } finally {
    batching.value = false
  }
}
</script>

<template>
  <section v-if="pending.length || deliveries.length || failedPostDeliveries.length || wizard" class="approval-dock" aria-label="待放行的操作">
    <!-- 意图澄清：run 阻塞在澄清上，先答它才有后续审批，故排在权限队列之前 -->
    <ClarifyingWizard v-if="wizard" />
    <template v-if="pending.length">
      <div class="ad-head">
        <span class="ad-dot" aria-hidden="true"></span>
        <!-- 审批面是弹出式打断：卡片出现在输入区上方，读屏用户不会正好盯着那里。
             role=status + polite 让计数自己播报一次，polite 不打断正在读的内容 -->
        <span class="ad-title" role="status" aria-live="polite" aria-atomic="true">
          {{ pending.length }} 条操作待你放行
        </span>
        <span class="ad-sp"></span>
        <template v-if="pending.length > 1">
          <button type="button" class="ad-b" :disabled="batching" @click="batch('deny')">
            全部拒绝
          </button>
          <button
            type="button"
            class="ad-b"
            :disabled="batching || onceBlocked || batchBlocked"
            :title="batchBlockedReason"
            @click="batch('approve')"
          >{{ batching ? '处理中…' : '全部批准本次' }}</button>
        </template>
      </div>
      <div class="ad-body">
        <ApprovalCard v-for="req in pending" :key="req.id" :req="req" />
      </div>
    </template>
    <!-- 交付确认没有「批量」：每条命令写进终端都会立刻执行，不能合并成一次放行。
         postFailed 的交付（回传耗尽重试）与待确认集合互斥，key 不冲突 -->
    <div v-if="deliveries.length || failedPostDeliveries.length" class="ad-body ad-deliveries">
      <DeliveryCard v-for="d in deliveries" :key="d.tool_id" :delivery="d" />
      <DeliveryCard v-for="d in failedPostDeliveries" :key="d.tool_id" :delivery="d" />
    </div>
  </section>
</template>

<style scoped>
/* 宽度与 HomeComposer 的输入卡同幅（max-width 820），在 composer-wrap 里居中 */
.approval-dock {
  width: 100%;
  max-width: 820px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.ad-head {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 2px;
  font-size: 11.5px;
}

.ad-dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--status-warn-text);
}

.ad-title {
  color: var(--status-warn-text);
  font-weight: 600;
  white-space: nowrap;
}

.ad-sp { flex: 1 }

.ad-b {
  flex: none;
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-primary);
  font-size: 11.5px;
  white-space: nowrap;
  cursor: pointer;
  transition: background var(--transition-fast);
}
.ad-b:hover:not(:disabled) { background: var(--bg-hover) }
.ad-b:disabled { opacity: 0.5; cursor: default }

/* 多张卡时只让卡区滚动：头部计数与批量按钮必须一直看得见。
   每张卡自身也限同高并内滚正文（见 ApprovalCard / DeliveryCard 的 scoped 样式），
   所以外层限高只影响卡队列，不会再裁掉某张卡底部的确认/拒绝。 */
.ad-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: min(38vh, 340px);
  overflow-y: auto;
  min-height: 0;
}
</style>
