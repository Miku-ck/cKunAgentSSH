<script setup lang="ts">
/**
 * ApprovalDock — 审批决策面（控制面），挂在输入区上方
 *
 * 审批是运行期的瞬时决策，不是执行记录：请求不写入 agentTrace / agent_runs，
 * run 收尾四条出口（done/error/abort/_reset）都会清空。所以它不能跟着消息流
 * 滚动 —— 用户往上翻历史时，待决的那条仍要停在输入框上方。
 *
 * 执行过程本身归运维轨迹（DB 权威记录），那里按 run_id 回放；本组件只负责
 * 「现在要你拍板」这一件事，逐条决策仍复用 ApprovalCard，批量只结算权限请求。
 */
import { computed, ref } from 'vue'
import { useAgentStore } from '@/stores/agent'
import { useApprovalActions } from '@/composables/useApprovalActions'
import { onceGrantBlocked } from '@/utils/approval-scopes'
import ApprovalCard from './ApprovalCard.vue'

const agent = useAgentStore()
const { settleAll } = useApprovalActions()

const pending = computed(() => agent.pendingPermissions)
const batching = ref(false)
const onceBlocked = computed(() => onceGrantBlocked(agent.isRunning))

async function batch(kind: 'approve' | 'deny') {
  if (batching.value) return
  batching.value = true
  try {
    await settleAll(kind)
  } finally {
    batching.value = false
  }
}
</script>

<template>
  <section v-if="pending.length" class="approval-dock" aria-label="待放行的操作">
    <div class="ad-head">
      <span class="ad-dot" aria-hidden="true"></span>
      <span class="ad-title">{{ pending.length }} 条操作待你放行</span>
      <span class="ad-sp"></span>
      <template v-if="pending.length > 1">
        <button type="button" class="ad-b" :disabled="batching" @click="batch('deny')">
          全部拒绝
        </button>
        <button
          type="button"
          class="ad-b"
          :disabled="batching || onceBlocked"
          :title="onceBlocked ? '本轮运行已结束，once 授权无处生效' : ''"
          @click="batch('approve')"
        >{{ batching ? '处理中…' : '全部批准本次' }}</button>
      </template>
    </div>
    <div class="ad-body">
      <ApprovalCard v-for="req in pending" :key="req.id" :req="req" />
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

/* 多张卡时只让卡区滚动：头部计数与批量按钮必须一直看得见 */
.ad-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: min(38vh, 340px);
  overflow-y: auto;
  min-height: 0;
}
</style>
