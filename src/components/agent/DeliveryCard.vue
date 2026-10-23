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

/** 卡头风险徽章：聚合级别由后端拆解层给出（展示用，权威判定在授权链）。
 *  旧后端不带 risk 字段时不渲染 —— 宁缺毋滥，别让徽章说没根据的话。 */
const riskBadge = computed(() => {
  const level = props.delivery.risk
  if (level === 'safe') {
    // P1b：safe 只代表免审批；证明只读时才敢说「不改动任何文件」
    return props.delivery.readonly
      ? { text: '只读 · 不改动任何文件', cls: 'dc-risk-safe' }
      : { text: '免审批 · 可能写入磁盘', cls: 'dc-risk-warn' }
  }
  if (level === 'dangerous') return { text: '含非只读操作 · 需谨慎核对', cls: 'dc-risk-bad' }
  if (level === 'hardline') return { text: '高危操作', cls: 'dc-risk-bad' }
  return null
})

/** 复合命令逐段拆解（后端拆解层，单段命令不带 segments） */
const segments = computed(() =>
  (props.delivery.segments ?? []).length > 1 ? props.delivery.segments! : [])
</script>

<template>
  <div class="ap-card delivery-card">
    <div class="ap-head">
      <svg class="ap-ic delivery-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
      <span class="ap-ttl">命令待确认</span>
      <span class="delivery-target" :title="targetLabel || '当前终端'">{{ targetLabel || '当前终端' }}</span>
      <span v-if="isProd" class="ap-env env-prod">生产环境</span>
      <span v-if="riskBadge" class="dc-risk" :class="riskBadge.cls">{{ riskBadge.text }}</span>
      <span class="ap-sp"></span>
    </div>
    <div class="ap-body">
      <!-- 阅读顺序与审批卡一致：你的请求 → 这条命令会做什么 → 逐段拆解 → 原文。
           解释由后端解释层随 tool_deliver 下发（纯展示，不参与任何放行判定），
           失败/未携带时不渲染 -->
      <div v-if="delivery.task" class="ap-keyval" :title="delivery.task">
        <span class="ap-k">你的请求</span>
        <span class="ap-v ap-2line">{{ delivery.task }}</span>
      </div>
      <div v-if="delivery.explain" class="ap-keyval">
        <span class="ap-k">这条命令会做什么</span>
        <span class="ap-v">{{ delivery.explain }}</span>
        <span v-if="explainIsLlm" class="ap-tag-inline">AI 生成，仅供参考</span>
      </div>
      <!-- 复合命令：逐段「意图 + 命令片段」列表，完整原文收进折叠区 ——
           用户先读人话结论，需要核对原文再展开；非只读段标红并显示后果说明 -->
      <div v-if="segments.length" class="seg-list">
        <div
          v-for="(seg, i) in segments"
          :key="i"
          class="seg-row"
          :class="{ 'seg-row-risky': seg.level !== 'safe' }"
        >
          <span class="seg-idx" :class="{ 'seg-idx-bad': seg.level !== 'safe' }">{{ i + 1 }}</span>
          <div class="seg-main">
            <span class="seg-intent">{{ seg.intent }}</span>
            <code class="seg-cmd" :title="seg.command">{{ seg.command }}</code>
            <span v-if="seg.note" class="seg-note">{{ seg.note }}</span>
          </div>
        </div>
      </div>
      <details v-if="segments.length" class="seg-raw">
        <summary>完整命令原文（{{ segments.length }} 段，点击展开核对）</summary>
        <pre class="delivery-code">{{ delivery.command }}</pre>
      </details>
      <pre v-if="!segments.length" class="delivery-code">{{ delivery.command }}</pre>
    </div>
    <div class="ap-actions">
      <button class="ap-btn ap-btn-grant" @click="agentStore.confirmDeliver(delivery.tool_id)">确认执行</button>
      <button class="ap-btn ap-btn-deny" @click="agentStore.rejectDeliver(delivery.tool_id)">拒绝执行</button>
    </div>
  </div>
</template>

<style scoped>
/* 与审批停靠区的 .ad-body 同高：整卡有界后正文自己滚，确认/拒绝不会落到可视区外 */
.delivery-card {
  border-color: var(--card-warn-border);
  max-height: min(38vh, 340px);
}
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

/* 卡头风险徽章：聚合级别展示（safe 只读绿 / 非 safe 警示红）。
   底色用文字令牌自身 12% 混入，与 col-badge 同一套派生法，不吃死色值。 */
.dc-risk {
  font-size: 10.5px;
  padding: 1px 8px;
  border-radius: 4px;
  white-space: nowrap;
}
.dc-risk-safe { color: var(--accent-green); background: color-mix(in srgb, var(--accent-green) 12%, transparent); }
.dc-risk-bad { color: var(--accent-red); background: color-mix(in srgb, var(--accent-red) 12%, transparent); }
.dc-risk-warn { color: var(--status-warn-text); background: color-mix(in srgb, var(--status-warn-text) 12%, transparent); }

/* 逐段拆解列表：序号 + 意图 + 命令片段。行背景斑马纹与终端表同思路。 */
.seg-list {
  border: 1px solid var(--border-default);
  border-radius: 6px;
  overflow: hidden;
}
.seg-row {
  display: flex;
  gap: 8px;
  padding: 6px 10px;
  align-items: flex-start;
}
.seg-row + .seg-row { border-top: 1px solid var(--border-subtle); }
.seg-row-risky { background: color-mix(in srgb, var(--accent-red) 6%, transparent); }
.seg-idx {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--bg-hover);
  color: var(--text-tertiary);
  font-size: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 1px;
}
.seg-idx-bad { color: var(--accent-red); }
.seg-main { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.seg-intent { font-size: 12px; color: var(--text-primary); line-height: 1.45; }
.seg-cmd {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.seg-note { font-size: 11px; color: var(--accent-red); line-height: 1.45; }

/* 完整原文折叠区：summary 用次要色调，展开后的 pre 复用 delivery-code */
.seg-raw { margin-top: 8px; }
.seg-raw summary {
  font-size: 11px;
  color: var(--text-tertiary);
  cursor: pointer;
  user-select: none;
  margin-bottom: 6px;
}
.seg-raw summary:hover { color: var(--text-secondary); }
</style>
