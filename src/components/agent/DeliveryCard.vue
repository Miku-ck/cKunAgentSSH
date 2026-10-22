<script setup lang="ts">
/**
 * DeliveryCard — 可见终端交付的逐条确认卡，消费方是输入区上方的 ApprovalDock
 *
 * 与 ApprovalCard 的分工：权限卡回答「能不能执行」，本卡回答「现在是否把它写进
 * 可见终端」。两者都是运行期的瞬时决策，所以都只活在输入区上方，不跟消息流滚动。
 *
 * 命令块只做换行、不做 shell 语法拆行：拆错引号/heredoc 会把「一条链」画成
 * 「多条独立命令」，用户据此拍板就失真了。原样展示，靠空格与分隔符自然折行。
 */
import { computed } from 'vue'
import type { ToolDelivery } from '@/composables/useAgentDeliver'
import { useAgentStore } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { approvalTargetLabel } from '@/utils/approval-display'

const props = defineProps<{
  delivery: ToolDelivery
}>()

const agentStore = useAgentStore()
const sshStore = useSSHStore()

/** 说明来源标签：LLM 生成的解释必须标注「仅供参考」，静态后果表不必 */
const explainIsLlm = computed(() => props.delivery.explain_source === 'llm')

const targetLabel = computed(() =>
  approvalTargetLabel(
    { sshSessionId: props.delivery.session_id },
    sshStore.sessions,
    sshStore.connections,
  ),
)

/** prod 徽章与权限卡同源：从会话反查连接；查不到就不冒充测试环境 */
const isProd = computed(() => {
  const session = sshStore.sessions.find(s => s.id === props.delivery.session_id)
  const conn = session?.connectionId
    ? sshStore.connections.find(c => c.id === session.connectionId)
    : undefined
  return conn?.environment === 'prod'
})
</script>

<template>
  <div class="ap-card delivery-card">
    <div class="ap-head">
      <svg class="ap-ic delivery-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
      <span class="ap-ttl">命令待确认</span>
      <span class="delivery-target" :title="targetLabel || '当前终端'">{{ targetLabel || '当前终端' }}</span>
      <span v-if="isProd" class="ap-env env-prod">生产环境</span>
      <span class="ap-sp"></span>
    </div>
    <div class="ap-body">
      <!-- 阅读顺序与审批卡一致：这条命令会做什么 → 命令原文。解释由后端解释层
           随 tool_deliver 下发（纯展示，不参与任何放行判定），失败/未携带时不渲染 -->
      <div v-if="delivery.explain" class="ap-keyval">
        <span class="ap-k">这条命令会做什么</span>
        <span class="ap-v">{{ delivery.explain }}</span>
        <span v-if="explainIsLlm" class="ap-tag-inline">AI 生成，仅供参考</span>
      </div>
      <pre class="delivery-code">{{ delivery.command }}</pre>
    </div>
    <div class="ap-actions">
      <button class="ap-btn ap-btn-grant" @click="agentStore.confirmDeliver(delivery.tool_id)">确认执行</button>
      <button class="ap-btn ap-btn-deny" @click="agentStore.rejectDeliver(delivery.tool_id)">拒绝执行</button>
    </div>
  </div>
</template>

<style scoped>
.delivery-card { border-color: var(--card-warn-border); }
.delivery-accent { color: var(--status-warn-text); }

/* 多会话时这是卡头唯一的位置信息：写进哪台机器。过长时截断，完整值留在 title。 */
.delivery-target {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 8px;
  border: 1px solid var(--border-default);
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-secondary);
}

/* break-all 会把路径从中间截断；改成只在空格/分隔符处折行，超长无空格
   token 才硬断。限高保证极端长命令不会把输入区顶出视口。 */
.delivery-code {
  margin: 0;
  max-height: 180px;
  overflow-y: auto;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  padding: 9px 11px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-primary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
