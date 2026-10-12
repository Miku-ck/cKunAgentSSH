<script setup lang="ts">
/**
 * 首页「待你放行」计数条（spec 2026-09-29-home-command-panel · 故事 10–14，C2 裁决后形态）
 *
 * 读 agentStore.pendingPermissions —— 与过程区的内联卡是同一份状态、同一个对象，
 * 不是抄一份。结算是 store 里的 approvePermission / denyPermission，所以在卡片上
 * 批准与在这儿批量批准效果严格一致（含失败留卡可重试）。
 *
 * 这里**不再逐条画卡**：C1 把过程搬进首页之后，队列与内联卡同屏，同一条待决请求
 * 出现两次、两个决策位。择一的时候选了卡，理由不是顺手而是能力差：
 * `ApprovalCard` 开四档（once / session / always / deny）并带 prod 警示，
 * 队列原先只开三档（没有 session 档）。把逐条决策留在这条窄条上，等于让用户
 * 每次都能看见一张更弱的卡。队列剩下的独有能力是批量与追溯，都留着。
 */
import { computed, ref } from 'vue'
import { useAgentStore } from '@/stores/agent'
import { useApprovalActions } from '@/composables/useApprovalActions'
import { approvalCommand } from '@/utils/approval-display'
import { onceGrantBlocked } from '@/utils/approval-scopes'

const agent = useAgentStore()
const { settleAll } = useApprovalActions()

/** 导航状态归宿主（SshView → useSshNavigation），这里只 emit。绝不能自己再调一次
 *  useSshNavigation()：那个 composable 每次调用新建一份状态，子组件点下去改的是
 *  一份没人读的状态 —— 亮的空开关。 */
const emit = defineEmits<{ 'open-run': [] }>()

const pending = computed(() => agent.pendingPermissions)
// P2 修复（2026-10-11）：批量「批准本次」全是 once 授权，run 终态后无处消费
// （后端 409）—— 与内联卡的「允许本次」同一道 run 状态门。
const onceBlocked = computed(() => onceGrantBlocked(agent.isRunning))
/** 已处理的卡不删数据（与内联卡原先同一口径）；超时卡按裁决 A 留在可操作队列，
 *  不进这里。队列空了但这里还有货时，整节仍要挂出来 —— 否则"批过什么"就查不到了。 */
const resolved = computed(() => agent.permissionRequests.filter(r => r.resolved))
/** 批量按钮的锁在组件侧：settleAll 里逐条 await，双击会让两个循环并跑 */
const batching = ref(false)

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
  <section v-if="pending.length || resolved.length" class="haq">
    <header v-if="pending.length" class="haq-head">
      <span class="t">待你放行</span>
      <span class="n">{{ pending.length }}</span>
      <span class="hint">卡片在下面过程里 · 批准即执行，拒绝则本条命令不再询问</span>
      <!-- 故事 14 的后半句"点进那个 run 看上下文"。计数条不再就地决策，所以这枚
           按钮从"顺路看一眼"变成了通往决策位的唯一一条路。 -->
      <button
        v-if="agent.lastRunId"
        type="button"
        class="haq-goto"
        title="滚到那条审批卡"
        @click="emit('open-run')"
      >看过程 · №{{ agent.lastRunId }}</button>
    </header>

    <!-- 批量结算是侧栏托盘退役后搬过来的能力（spec 2026-09-30-retire-ai-side-panel R1），
         也是这条窄条在内联卡之外唯一还剩下的决策动作 —— 它作用于「这一批」，
         与逐条卡不是同一件事，所以不算两个决策位。
         两个细节照旧：按钮上说清作用范围是「本次」（once）—— 批量点「始终允许」等于
         一次性给一批命令签长期授权，那是权限越界不是效率；循环里的快照语义在
         useApprovalActions.settleAll 里。 -->
    <div v-if="pending.length > 1" class="haq-batch">
      <button type="button" class="b" :disabled="batching" @click="batch('deny')">
        全部拒绝（{{ pending.length }}）
      </button>
      <!-- 批量批准都是 once 授权，共用内联卡同一道 run 状态门（onceBlocked）。 -->
      <button
        type="button"
        class="b"
        :disabled="batching || onceBlocked"
        :title="onceBlocked ? '本轮运行已结束，once 授权无处生效' : ''"
        @click="batch('approve')"
      >
        {{ batching ? '处理中…' : `全部批准本次（${pending.length}）` }}
      </button>
    </div>

    <!-- 追溯区：批过/拒过的命令留在原地，不删数据。内联卡只活到 resolved 那一刻，
         所以"这一轮批过哪些"只有这里查得到。 -->
    <details v-if="resolved.length" class="haq-trace">
      <summary>历史记录（{{ resolved.length }} 已处理）</summary>
      <div v-for="req in resolved" :key="req.id" class="haq-trace-row">
        <code :title="approvalCommand(req) || req.tool">{{ approvalCommand(req) || req.tool }}</code>
        <span :class="req.granted ? 'ok' : 'no'">{{ req.granted ? '已授权' : '已拒绝' }}</span>
      </div>
    </details>
  </section>
</template>

<style scoped>
/* 只用现有主题令牌，不写死颜色 —— 主题层是第四片的事 */
.haq {
  width: 100%;
  max-width: 720px;
  margin-top: 16px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 12px;
}
.haq-head {
  display: flex;
  align-items: baseline;
  gap: 9px;
  margin-bottom: 9px;
}
.haq-head .t {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .5px;
  text-transform: uppercase;
  color: var(--text-secondary);
}
.haq-head .n {
  /* 计数的手写体感归主题（稿子里 .gate-h .n 用的就是展示衬线）；字号不动。 */
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--accent-orange);
}
.haq-head .hint { margin-left: auto; font-size: 11px; color: var(--text-tertiary) }
.haq-goto {
  height: 22px;
  padding: 0 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: nowrap;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.haq-goto:hover { background: var(--bg-hover); color: var(--text-primary) }
.b {
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
  white-space: nowrap;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}
.b:hover:not(:disabled) { background: var(--bg-hover) }
.b:disabled { opacity: .5; cursor: default }
/* 批量行：只有两条以上待决才出现 —— 一条的时候它就是把同一件事问两遍。
   刻意不用强调色：批量是效率控件，不该比逐条「批准」更显眼，否则视觉上在劝人多点。
   逐条的「批准」已经在下方内联卡上了，这里没有第二枚强调色按钮可争。 */
.haq-batch { display: flex; gap: 7px; justify-content: flex-end; margin-bottom: 8px }
.haq-trace { margin-bottom: 4px }
.haq-trace summary {
  font-size: 11px;
  color: var(--text-tertiary);
  cursor: pointer;
  padding: 3px 0;
}
.haq-trace-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 2px;
  font-size: 11px;
  border-bottom: 1px solid var(--border-subtle);
}
.haq-trace-row code {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.haq-trace-row .ok { color: var(--status-success-text) }
.haq-trace-row .no { color: var(--danger-text) }
</style>
