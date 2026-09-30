<script setup lang="ts">
/**
 * 首页「待你放行」队列（spec 2026-09-29-home-command-panel · 用户故事 10–14）
 *
 * 读 agentStore.pendingPermissions —— 与侧栏审批托盘是同一份状态，不是抄一份。
 * 结算是首页/托盘共用 store 里的 approvePermission / denyPermission，所以在这
 * 里批准与在侧栏里批准效果严格一致（含失败留卡可重试）。
 */
import { computed, ref } from 'vue'
import { useAgentStore, type PermissionRequest } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { useApprovalActions } from '@/composables/useApprovalActions'
import { grantScopesForRisk } from '@/utils/approval-scopes'
import { approvalCommand, approvalRiskLabel, approvalTargetLabel } from '@/utils/approval-display'

const agent = useAgentStore()
const ssh = useSSHStore()
const { approve, deny, settleAll } = useApprovalActions()

/** 导航状态归宿主（SshView → useSshNavigation）。这里绝不能自己再调一次
 *  useSshNavigation()：那个 composable 每次调用都新建一份 showRun，点下去改的是
 *  一份没人读的状态 —— 亮的空开关。 */
const emit = defineEmits<{ 'open-run': [] }>()

const pending = computed(() => agent.pendingPermissions)
/** 已处理的卡不删数据（与侧栏托盘原先同一口径）；超时卡按裁决 A 留在可操作队列，
 *  不进这里。队列空了但这里还有货时，整节仍要挂出来 —— 否则"批过什么"就查不到了。 */
const resolved = computed(() => agent.permissionRequests.filter(r => r.resolved))
/** 批量按钮的锁在组件侧：settleAll 里逐条 await，双击会让两个循环并跑 */
const batching = ref(false)
const target = (req: PermissionRequest) => approvalTargetLabel(req, ssh.sessions, ssh.connections)
const canAlways = (req: PermissionRequest) => grantScopesForRisk(req.risk).includes('always')

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
      <span class="hint">批准即执行 · 拒绝则本条命令不再询问</span>
      <!-- 故事 14 的后半句"点进那个 run 看上下文"。归属只在队头说一次：单 run 模型下
           队列里所有卡都来自这条 run（_reset 连着清 permissionRequests 与 lastRunId，
           agent.test.ts 有断言钉着），每张卡印一遍就是把同一个号重复 N 次。 -->
      <button
        v-if="agent.lastRunId"
        type="button"
        class="haq-goto"
        :title="`进入这条 run · trace ${agent.lastRunId}`"
        @click="emit('open-run')"
      >看过程 · №{{ agent.lastRunId }}</button>
    </header>

    <article v-for="req in pending" :key="req.id" class="haq-card" :class="`r-${req.risk}`">
      <div class="haq-body">
        <p v-if="req.task" class="haq-task">{{ req.task }}</p>
        <code class="haq-cmd" :title="approvalCommand(req) || req.tool">{{ approvalCommand(req) || req.tool }}</code>
        <div class="haq-meta">
          <span class="risk" :class="`r-${req.risk}`">{{ approvalRiskLabel(req.risk) }}</span>
          <span v-if="target(req)" class="m">{{ target(req) }}</span>
          <span class="m">{{ req.reversible ? '可逆' : '不可逆' }}</span>
          <span v-if="req.expired" class="m exp">已超时</span>
        </div>
        <p v-if="req.explain" class="haq-explain">{{ req.explain }}</p>
      </div>
      <div class="haq-acts">
        <button type="button" class="b no" :disabled="req.settling" @click="deny(req.id)">拒绝</button>
        <button v-if="canAlways(req)" type="button" class="b alt" :disabled="req.settling" @click="approve(req.id, 'always')">始终允许</button>
        <button type="button" class="b pri" :disabled="req.settling" @click="approve(req.id, 'once')">
          {{ req.settling ? '处理中...' : '批准' }}
        </button>
      </div>
    </article>

    <!-- 批量结算是侧栏托盘退役后搬过来的能力（spec 2026-09-30-retire-ai-side-panel R1）。
         两个细节照旧：按钮上说清作用范围是「本次」（once）—— 批量点「始终允许」等于
         一次性给一批命令签长期授权，那是权限越界不是效率；循环里的快照语义在
         useApprovalActions.settleAll 里。 -->
    <div v-if="pending.length > 1" class="haq-batch">
      <button type="button" class="b" :disabled="batching" @click="batch('deny')">
        全部拒绝（{{ pending.length }}）
      </button>
      <button type="button" class="b" :disabled="batching" @click="batch('approve')">
        {{ batching ? '处理中…' : `全部批准本次（${pending.length}）` }}
      </button>
    </div>

    <!-- 追溯区：批过/拒过的命令留在原地，不删数据 -->
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
  /* 计数的手写体感归主题（稿子里 .gate-h .n 用的就是展示衬线）；字号不动。
   * --font-display 目前只有纸纹那套未提交的令牌表里有定义，回落必须写：
   * 缺了它，HEAD 里这一行就是无效声明，字号照旧而字体悄悄换掉，肉眼扫不出来。 */
  font-family: var(--font-display, var(--font-sans));
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
.haq-card {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 11px 13px;
  margin-bottom: 8px;
  background: var(--bg-elevated);
  border: 1px solid var(--card-warn-border);
  border-left: 3px solid var(--accent-orange);
  border-radius: var(--radius-md);
}
.haq-card.r-dangerous { border-left-color: var(--danger-text); border-color: var(--danger-border) }
.haq-card.r-safe { border-left-color: var(--status-success-border); border-color: var(--border-subtle) }
.haq-body { flex: 1; min-width: 240px }
.haq-task {
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.5;
  margin-bottom: 5px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.haq-cmd {
  display: block;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent-cyan);
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 5px 8px;
  overflow-x: auto;
  white-space: pre;
}
.haq-meta { display: flex; align-items: center; gap: 9px; margin-top: 6px; flex-wrap: wrap }
.haq-meta .m { font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono) }
.haq-meta .m.exp { color: var(--accent-orange) }
.haq-explain { margin-top: 5px; font-size: 12px; color: var(--text-secondary); line-height: 1.55 }
.risk {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
}
.risk.r-dangerous { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-text) }
.risk.r-moderate { background: var(--status-warn-bg); color: var(--status-warn-text) }
.risk.r-safe { background: var(--status-success-bg); border-color: var(--status-success-border); color: var(--status-success-text) }
.haq-acts { display: flex; gap: 7px }
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
.b.no { border-color: var(--border-subtle); color: var(--text-tertiary) }
.b.no:hover:not(:disabled) { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-text) }
.b.alt { border-style: dashed; color: var(--text-secondary) }
.b.pri {
  background: var(--accent-primary);
  border-color: var(--accent-primary);
  /* 同 .hcc-send：写死 #fff 时强调色选「白色」这枚「批准」按钮白底白字 */
  color: var(--accent-on, #fff);
  font-weight: 600;
}
.b.pri:hover:not(:disabled) { filter: brightness(1.15); background: var(--accent-primary) }
/* 批量行：只有两条以上待决才出现 —— 一条的时候它就是把同一件事问两遍。
   刻意不用强调色：批量是效率控件，不该比逐条「批准」更显眼，否则视觉上在劝人多点。 */
.haq-batch { display: flex; gap: 7px; justify-content: flex-end; margin: -2px 0 10px }
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
