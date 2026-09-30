<script setup lang="ts">
/**
 * 首页「待你放行」队列（spec 2026-09-29-home-command-panel · 用户故事 10–14）
 *
 * 读 agentStore.pendingPermissions —— 与侧栏审批托盘是同一份状态，不是抄一份。
 * 结算是首页/托盘共用 store 里的 approvePermission / denyPermission，所以在这
 * 里批准与在侧栏里批准效果严格一致（含失败留卡可重试）。
 */
import { computed } from 'vue'
import { useAgentStore, type PermissionRequest } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { useApprovalActions } from '@/composables/useApprovalActions'
import { grantScopesForRisk } from '@/utils/approval-scopes'
import { approvalCommand, approvalRiskLabel, approvalTargetLabel } from '@/utils/approval-display'

const agent = useAgentStore()
const ssh = useSSHStore()
const { approve, deny } = useApprovalActions()

/** 导航状态归宿主（SshView → useSshNavigation）。这里绝不能自己再调一次
 *  useSshNavigation()：那个 composable 每次调用都新建一份 showRun，点下去改的是
 *  一份没人读的状态 —— 亮的空开关。 */
const emit = defineEmits<{ 'open-run': [] }>()

const pending = computed(() => agent.pendingPermissions)
const target = (req: PermissionRequest) => approvalTargetLabel(req, ssh.sessions, ssh.connections)
const canAlways = (req: PermissionRequest) => grantScopesForRisk(req.risk).includes('always')
</script>

<template>
  <section v-if="pending.length" class="haq">
    <header class="haq-head">
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
        :title="`trace ${agent.lastRunId}`"
        @click="emit('open-run')"
      >看过程 · №{{ agent.lastRunId.slice(0, 8) }}</button>
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
  color: #fff;
  font-weight: 600;
}
.b.pri:hover:not(:disabled) { filter: brightness(1.15); background: var(--accent-primary) }
</style>
