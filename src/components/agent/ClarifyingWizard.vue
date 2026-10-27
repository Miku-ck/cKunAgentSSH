<template>
  <div
    v-if="wizard"
    class="ap-card ap-clarify"
    role="group"
    aria-label="AI 需要你澄清意图"
  >
    <!-- 顶栏：图标 + 标题 + 进度（复用 .ap-head 骨架，与审批卡同视觉密度） -->
    <div class="ap-head">
      <svg class="ap-ic ap-clarify-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
        <circle cx="12" cy="12" r="10"/>
        <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
        <line x1="12" y1="17" x2="12.01" y2="17"/>
      </svg>
      <span class="ap-ttl ap-clarify-ttl">AI 需要你澄清意图</span>
      <span class="ap-sp"></span>
      <span class="clarify-progress">{{ displayStep }} / {{ wizard?.totalSteps ?? 0 }}</span>
    </div>

    <!-- 命令上下文：命令预览 + 触发原因（信息有价值，随卡进 dock） -->
    <div v-if="commandText || wizard?.reason" class="clarify-context">
      <div v-if="commandText" class="ctx-cmd">
        <span class="ctx-cmd-label">{{ wizard?.tool || 'command' }}</span>
        <code class="ctx-cmd-text">{{ commandText }}</code>
      </div>
      <div v-if="wizard?.reason" class="ctx-reason">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
          <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/>
          <line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
        <span>{{ wizard.reason }}</span>
      </div>
    </div>

    <!-- 问题正文 + 选项（.ap-body 内滚，头部与动作栏常驻） -->
    <div class="ap-body">
      <p class="clarify-question">
        <span class="qnum">{{ displayStep }}.</span>
        {{ currentQuestion.question }}
      </p>

      <div class="clarify-options">
        <button
          v-for="opt in currentQuestion.options"
          :key="opt.id"
          class="clarify-option"
          :class="{ selected: selectedChoice === opt.id }"
          @click="selectOption(opt.id)"
        >
          <span class="badge" :class="{ selected: selectedChoice === opt.id }">{{ opt.id }}</span>
          <span class="opt-label">{{ opt.label }}</span>
        </button>

        <!-- D 自定义输入 -->
        <div
          v-if="currentQuestion.allowCustom"
          class="clarify-option custom-wrap"
          :class="{ selected: selectedChoice === 'custom' }"
        >
          <span class="badge" :class="{ selected: selectedChoice === 'custom' }">D</span>
          <input
            v-model="customText"
            class="custom-input"
            type="text"
            placeholder="输入自定义回答…"
            @focus="selectedChoice = 'custom'"
            @keydown.enter="handleAdvance"
          />
        </div>
      </div>
    </div>

    <!-- 底部动作：复用审批卡按钮 —— 绿=完成放行，靠右否决位=跳过 -->
    <div class="ap-actions">
      <button
        class="ap-btn ap-btn-grant"
        :disabled="!canAdvance || submitting"
        @click="handleAdvance"
      >
        <span v-if="submitting">提交中…</span>
        <span v-else>完成 ↵</span>
      </button>
      <button
        class="ap-btn ap-btn-deny"
        :disabled="submitting"
        title="跳过澄清，AI 将按原始指令自行判断"
        @click="handleSkip"
      >跳过</button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * ClarifyingWizard — 意图澄清卡（决策面），消费方是输入区上方的 ApprovalDock
 *
 * 后端 ClarificationInterceptor 在 PRE_TOOL_CALL 发现意图不明的工具调用（如
 * rm -rf *）时一次性推送全部 steps（current_step=0）+ 当前步 question/options/
 * allowCustom。前端负责：
 *   1. 渲染当前题 + 收集选择（本地累加 answers）
 *   2. 点"完成"后若非末步 → 前进到下一题（清空选择状态）
 *   3. 若是末步 → POST /agent/clarification/answer 提交全部 answers
 *   4. "跳过" → POST /agent/clarification/skip 并关闭
 *
 * 呈现：与审批卡同风格的非模态决策卡（骨架走全局 approval-card.css 的 .ap-*）。
 * 曾是全屏遮罩的居中弹窗，2026-10-23 并入 ApprovalDock —— 澄清与审批都是"要你
 * 拍板"，位置与风格应一致，且都常驻输入区上方、不随消息流滚走。裁决只走按钮，
 * 不再挂全局键盘监听（非模态卡下全局按键会误伤输入框/终端）。
 *
 * 关闭时机：提交/跳过成功后 clearClarification() 立即置 null（不等 SSE 回执，
 * P0-2）；run 终态四条出口也会清空，卡随 dock v-if 自然消失。
 */
import { ref, computed, watch } from 'vue'
import { useAgentStore } from '@/stores/agent'
import { useToastStore } from '@/stores/toast'

const agentStore = useAgentStore()

const selectedChoice = ref<string | null>(null)
const customText = ref('')
const submitting = ref(false)

/** 本地步索引（前端推进用，与 wizard.currentStep 解耦） */
const localStep = ref(0)

/** 当前向导状态 — 直接引用 store；非空即渲染（v-if），为空即消失 */
const wizard = computed(() => agentStore.clarificationWizard)

/** 新向导到达 → 归零本地进度与选择。
 *
 * 必须 immediate：本组件可能挂在 clarification_needed 之后才创建（运行中切走再回
 * 首页 → 全新实例、store 里 wizard 已非空但 watch 从未触发），少了 immediate 首题
 * 的本地步/选择不会重置。*/
watch(wizard, (w) => {
  if (w) {
    localStep.value = 0
    selectedChoice.value = null
    customText.value = ''
  }
}, { immediate: true })

/** 显示用的步数（1-based） */
const displayStep = computed(() => {
  const base = wizard.value?.currentStep ?? 0
  return Math.max(1, base + localStep.value + 1)
})

/** 当前步索引（0-based）：后端起始步 + 前端本地推进 */
const stepIndex = computed(() => (wizard.value?.currentStep ?? 0) + localStep.value)

/** 当前步骤的题目（question / options / allowCustom）
 *
 * 全量 steps 是后端契约（clarification_needed.steps）；缺 steps 时回落到事件
 * 自带的当前步字段，保证单步/旧事件仍能渲染。 */
const currentQuestion = computed(() => {
  const w = wizard.value
  if (!w) return { question: '', options: [] as Array<{ id: string; label: string }>, allowCustom: true }
  const s = w.steps?.[stepIndex.value]
  return {
    question: s?.question ?? w.question ?? '',
    options: s?.options ?? w.options ?? [],
    allowCustom: s?.allow_custom ?? w.allowCustom ?? true,
  }
})

const canAdvance = computed(() => {
  const choice = selectedChoice.value
  if (choice === null) return false
  // 只选了「自定义」却不填内容 → 提交上去是无意义的 "choice: custom"，
  // 后端原样注入 LLM 上下文，等于用户答了个空
  if (choice === 'custom') return customText.value.trim().length > 0
  return true
})

/** 从 args 提取可展示的命令文本（command/shell/cmd → JSON 兜底） */
const commandText = computed(() => {
  const a = wizard.value?.args
  if (!a || typeof a !== 'object') return ''
  if (typeof a.command === 'string') return a.command
  if (typeof a.shell === 'string') return a.shell
  if (typeof a.cmd === 'string') return a.cmd
  try {
    const s = JSON.stringify(a)
    return s.length > 200 ? s.slice(0, 200) + '…' : s
  } catch {
    return ''
  }
})

function selectOption(id: string) {
  selectedChoice.value = id
  customText.value = ''
}

/** 记录当前答案到 wizard.answers */
function recordCurrentAnswer() {
  const w = wizard.value
  if (!w || selectedChoice.value === null) return
  const stepKey = String(stepIndex.value)
  const choice = selectedChoice.value
  if (choice === 'custom') {
    w.answers[stepKey] = { choice, custom_text: customText.value || undefined }
  } else {
    w.answers[stepKey] = { choice }
  }
}

/** 前进到下一题或提交末步答案 */
async function handleAdvance() {
  if (!canAdvance.value || !wizard.value || submitting.value) return

  recordCurrentAnswer()

  const w = wizard.value
  const lastStepIdx = w.totalSteps - 1
  const currentIdx = w.currentStep + localStep.value

  if (currentIdx < lastStepIdx) {
    localStep.value++
    selectedChoice.value = null
    customText.value = ''
  } else {
    await submitAnswers()
  }
}

async function submitAnswers() {
  const w = wizard.value
  if (!w || submitting.value) return
  // P0-6 校验：仅允许当前活跃 run 提交
  if (w.runId && agentStore.lastRunId && w.runId !== agentStore.lastRunId) {
    console.warn('[ClarifyingWizard] run_id 不匹配当前活跃 run，忽略:', w.runId)
    return
  }
  submitting.value = true
  try {
    await agentStore.submitClarification(w.runId, { ...w.answers })
    // P0-2 修复：提交成功后立即自行关闭，不依赖 clarification_done SSE 回执
    agentStore.clearClarification()
  } catch (err) {
    console.error('[ClarifyingWizard] submit failed:', err)
    useToastStore().error('提交失败，请重试')
  } finally {
    submitting.value = false
  }
}

async function handleSkip() {
  if (submitting.value) return
  const w = wizard.value
  if (!w) return
  // P0-6 校验：仅允许当前活跃 run 跳过
  if (w.runId && agentStore.lastRunId && w.runId !== agentStore.lastRunId) {
    console.warn('[ClarifyingWizard] run_id 不匹配当前活跃 run，忽略:', w.runId)
    return
  }
  submitting.value = true
  try {
    await agentStore.skipClarification(w.runId)
    // P0-2 修复：跳过成功后立即自行关闭
    agentStore.clearClarification()
  } catch (err) {
    console.error('[ClarifyingWizard] skip failed:', err)
    useToastStore().error('跳过失败，请重试')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
/*
 * 卡骨架 / 头部 / 按钮走全局 approval-card.css 的 .ap-*（与审批卡同源，免漂移）。
 * 这里只留审批卡家族没有的元素：命令上下文条、问题选项列表、自定义输入、进度。
 * 配色沿用项目令牌（--card-perm-* / --status-warn-* / --bg-*），不写死色值。
 */

/* 与 .ap-perm 同限高：正文（.ap-body）内滚，头部与动作栏常驻可视区 */
.ap-clarify { max-height: min(38vh, 340px); }
.ap-clarify-accent { color: var(--card-perm-text); }
.ap-clarify-ttl { color: var(--card-perm-text); }

.clarify-progress {
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
}

/* ── 命令上下文条 ── */
.clarify-context {
  padding: 8px 13px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--bg-input);
}
.ctx-cmd { display: flex; align-items: center; gap: 8px; }
.ctx-cmd-label {
  flex-shrink: 0;
  font-size: 10px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 1px 5px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xs);
  background: var(--bg-hover);
}
.ctx-cmd-text {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--status-info-text);
  background: var(--bg-input);
  border-radius: var(--radius-xs);
  padding: 3px 7px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
  display: block;
}
.ctx-reason {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  font-size: 11px;
  line-height: 1.4;
  color: var(--status-warn-text);
}
.ctx-reason svg { flex-shrink: 0; margin-top: 1px; opacity: 0.8; }

/* ── 问题正文 ── */
.clarify-question {
  margin: 0 0 10px;
  font-size: 13px;
  font-weight: 500;
  line-height: 1.5;
  color: var(--text-primary);
}
.qnum {
  color: var(--card-perm-text);
  font-weight: 600;
  margin-right: 6px;
  font-family: var(--font-mono);
}

/* ── 选项列表 ── */
.clarify-options { display: flex; flex-direction: column; gap: 4px; }
.clarify-option {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  width: 100%;
  min-height: 34px;
  padding: 6px 10px;
  background: var(--bg-input);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all 0.1s ease;
  text-align: left;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.45;
}
.clarify-option:hover {
  background: var(--bg-hover);
  border-color: var(--border-default);
  color: var(--text-primary);
}
.clarify-option.selected {
  background: var(--status-warn-bg);
  border-color: var(--card-warn-border);
  color: var(--text-primary);
}
.badge {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  border-radius: var(--radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
  font-family: var(--font-mono);
  background: var(--bg-hover);
  color: var(--text-tertiary);
  border: 1px solid var(--border-subtle);
  transition: all 0.1s ease;
  margin-top: 2px;
}
.badge.selected {
  background: var(--card-perm-text);
  /* 压在 --card-perm-text 上的前景：深色主题暖黄底需深字、浅色/手记深褐底需浅字。
     --bg-base 两头都对（黑/白），故不写死 */
  color: var(--bg-base);
  border-color: var(--card-perm-text);
}
.opt-label { flex: 1; word-break: break-word; }

/* ── 自定义输入 ── */
.custom-wrap { cursor: default; align-items: center; }
.custom-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-primary);
  padding: 2px 0;
  min-width: 0;
}
.custom-input::placeholder { color: var(--text-tertiary); }

@media (prefers-reduced-motion: reduce) {
  .clarify-option, .badge { transition: none; }
}
</style>
