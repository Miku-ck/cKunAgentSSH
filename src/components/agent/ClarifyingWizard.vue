<template>
  <Teleport to="body">
    <Transition name="clarify-fade">
      <div v-if="visible" class="clarify-overlay" @click.self="">
        <div class="clarify-dialog" role="dialog" aria-modal="true" aria-label="澄清向导">
          <!-- 顶部栏：图标 + 标题 + 进度 -->
          <div class="clarify-header">
            <div class="clarify-header-left">
              <svg class="clarify-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
              <span class="clarify-title">问题</span>
            </div>
            <div class="clarify-progress">
              {{ displayStep }} / {{ wizard?.totalSteps ?? 0 }}
            </div>
          </div>

          <!-- 命令上下文：命令预览 + 触发原因 -->
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

          <!-- 问题正文 + 选项 -->
          <div class="clarify-body">
            <p class="clarify-question">
              <span class="qnum">{{ displayStep }}.</span>
              {{ wizard?.question || '' }}
            </p>

            <div class="clarify-options">
              <button
                v-for="opt in wizard?.options || []"
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
                v-if="wizard?.allowCustom"
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

          <!-- 底部操作 -->
          <div class="clarify-footer">
            <button class="cbtn skip" :disabled="submitting" @click="handleSkip">跳过</button>
            <button
              ref="nextBtnRef"
              class="cbtn done"
              :disabled="!canAdvance || submitting"
              @click="handleAdvance"
            >
              <span v-if="submitting">提交中…</span>
              <span v-else>完成 ↵</span>
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
/**
 * ClarifyingWizard — 澄清向导多层卡片 UI
 *
 * 行为：后端 ClarificationInterceptor 在 PRE_TOOL_CALL 发现意图不明时一次性
 * 推送全部 steps（current_step=0）+ 当前步的 question/options/allowCustom。
 * 前端负责：
 *   1. 渲染当前题 + 收集选择（本地累加 answers）
 *   2. 点"完成/Next"后若非末步 → 前进到下一题（清空选择状态）
 *   3. 若是末步 → POST /agent/clarification/answer 提交全部 answers
 *   4. "跳过" → POST /agent/clarification/skip 并关闭
 *
 * 顶部新增命令预览（从 args.command/shell/cmd 提取）+ 触发原因（reason），
 * 用户一眼看到"AI 想跑什么命令 + 为什么被拦"。
 *
 * 关闭时机：store 收到 clarification_done 事件后 800ms 自动清除状态。
 */
import { ref, computed, watch, nextTick, onUnmounted } from 'vue'
import { useAgentStore } from '@/stores/agent'

const agentStore = useAgentStore()

const visible = ref(false)
const nextBtnRef = ref<HTMLButtonElement | null>(null)
const selectedChoice = ref<string | null>(null)
const customText = ref('')
const submitting = ref(false)

/** 本地步索引（前端推进用，与 wizard.currentStep 解耦） */
const localStep = ref(0)

/** 当前向导状态 — 直接引用 store 实现双向绑定 */
const wizard = computed(() => agentStore.clarificationWizard)

/** 显示用的步数（1-based） */
const displayStep = computed(() => {
  const base = wizard.value?.currentStep ?? 0
  return Math.max(1, base + localStep.value + 1)
})

/** 当前步骤的题目（question / options / allowCustom） */
const currentQuestion = computed(() => {
  if (!wizard.value) return { question: '', options: [], allowCustom: true }
  return {
    question: wizard.value.question || '',
    options: wizard.value.options || [],
    allowCustom: wizard.value.allowCustom ?? true,
  }
})

const canAdvance = computed(() => selectedChoice.value !== null)

/** 从 args 提取可展示的命令文本（command/shell/cmd → JSON 兜底） */
const commandText = computed(() => {
  const a = wizard.value?.args
  if (!a || typeof a !== 'object') return ''
  if (typeof a.command === 'string') return a.command
  if (typeof a.shell === 'string') return a.shell
  if (typeof a.cmd === 'string') return a.cmd
  // 非 shell_exec 工具：序列化为单行预览
  try {
    const s = JSON.stringify(a)
    return s.length > 200 ? s.slice(0, 200) + '…' : s
  } catch {
    return ''
  }
})

/** 向导状态出现 → 显示；被清除 → 隐藏 */
watch(wizard, (w) => {
  if (w) {
    visible.value = true
    localStep.value = 0
    selectedChoice.value = null
    customText.value = ''
    nextTick(() => nextBtnRef.value?.focus())
  } else {
    visible.value = false
  }
})

function selectOption(id: string) {
  selectedChoice.value = id
  customText.value = ''
}

/** 记录当前答案到 wizard.answers */
function recordCurrentAnswer() {
  const w = wizard.value
  if (!w) return
  const stepKey = String(w.currentStep + localStep.value)
  const choice = selectedChoice.value!
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
    // 前进到下一题
    localStep.value++
    selectedChoice.value = null
    customText.value = ''
    nextTick(() => nextBtnRef.value?.focus())
  } else {
    // 末步 → 提交全部答案
    await submitAnswers()
  }
}

async function submitAnswers() {
  const w = wizard.value
  if (!w || submitting.value) return
  submitting.value = true
  try {
    await agentStore.submitClarification(w.runId, { ...w.answers })
  } catch (err) {
    console.error('[ClarifyingWizard] submit failed:', err)
  } finally {
    submitting.value = false
  }
}

async function handleSkip() {
  if (submitting.value) return
  const w = wizard.value
  if (!w) return
  submitting.value = true
  try {
    await agentStore.skipClarification(w.runId)
  } catch (err) {
    console.error('[ClarifyingWizard] skip failed:', err)
  } finally {
    submitting.value = false
  }
}

function handleKeydown(e: KeyboardEvent) {
  if (!visible.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    handleSkip()
  }
}

watch(visible, (v) => {
  if (v) {
    window.addEventListener('keydown', handleKeydown, true)
  } else {
    window.removeEventListener('keydown', handleKeydown, true)
  }
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown, true)
})
</script>

<style scoped>
/*
 * 样式对齐项目设计系统：
 *   - 背景用 --card-perm-bg（暖暗棕），边框用 --card-perm-border
 *   - 完成按钮复用 --card-perm-btn-* 绿色系（与权限卡片"允许"按钮同风格）
 *   - 选中态用 --status-warn-* 暖黄高亮，对话号用 --card-perm-text 暖黄
 *   - 整体更紧凑（padding / 圆角更小），匹配权限卡片视觉密度
 */
.clarify-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 3200;
  backdrop-filter: blur(2px);
}

.clarify-dialog {
  width: 440px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 80px);
  background: var(--card-perm-bg, #2d221a);
  border: 1px solid var(--card-perm-border, #4a3a23);
  border-radius: var(--radius-md, 6px);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(0, 0, 0, 0.4);
  font-family: var(--font-sans);
  color: var(--text-primary, #ccc);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── 顶栏 ── */
.clarify-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}

.clarify-header-left {
  display: flex;
  align-items: center;
  gap: 7px;
}

.clarify-icon {
  color: var(--card-perm-text, #e6c07b);
  flex-shrink: 0;
}

.clarify-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--card-perm-text, #e6c07b);
  letter-spacing: 0.3px;
}

.clarify-progress {
  font-size: 11px;
  color: var(--text-tertiary, #767676);
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono, monospace);
}

/* ── 命令上下文条 ── */
.clarify-context {
  padding: 8px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: rgba(0, 0, 0, 0.18);
}

.ctx-cmd {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ctx-cmd-label {
  flex-shrink: 0;
  font-size: 10px;
  font-family: var(--font-mono, monospace);
  color: var(--text-tertiary, #767676);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 1px 5px;
  border: 1px solid var(--border-subtle, #2d2d30);
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.03);
}

.ctx-cmd-text {
  flex: 1;
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  color: var(--status-info-text, #569cd6);
  background: rgba(0, 0, 0, 0.25);
  border-radius: 2px;
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
  color: var(--status-warn-text, #dcdcaa);
}

.ctx-reason svg {
  flex-shrink: 0;
  margin-top: 1px;
  opacity: 0.8;
}

/* ── 问题区 ── */
.clarify-body {
  padding: 12px 14px 10px;
  flex: 1;
  overflow-y: auto;
}

.clarify-question {
  margin: 0 0 10px;
  font-size: 13px;
  font-weight: 500;
  line-height: 1.5;
  color: var(--text-primary, #ccc);
}

.qnum {
  color: var(--card-perm-text, #e6c07b);
  font-weight: 600;
  margin-right: 6px;
  font-family: var(--font-mono, monospace);
}

/* ── 选项列表 ── */
.clarify-options {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.clarify-option {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  width: 100%;
  min-height: 34px;
  padding: 6px 10px;
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid transparent;
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  transition: all 0.1s ease;
  text-align: left;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-secondary, #969696);
  line-height: 1.45;
}

.clarify-option:hover {
  background: rgba(255, 255, 255, 0.04);
  border-color: var(--border-default, #3e3e42);
  color: var(--text-primary, #ccc);
}

.clarify-option.selected {
  background: var(--status-warn-bg, #2d2d1a);
  border-color: var(--card-warn-border, #4a4533);
  color: var(--text-primary, #ccc);
}

.badge {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  border-radius: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
  font-family: var(--font-mono, monospace);
  background: rgba(255, 255, 255, 0.06);
  color: var(--text-tertiary, #767676);
  border: 1px solid var(--border-subtle, #2d2d30);
  transition: all 0.1s ease;
  margin-top: 2px;
}

.badge.selected {
  background: var(--card-perm-text, #e6c07b);
  color: #1a1a0a;
  border-color: var(--card-perm-text, #e6c07b);
}

.opt-label {
  flex: 1;
  word-break: break-word;
}

/* ── 自定义输入 ── */
.custom-wrap {
  cursor: default;
  align-items: center;
}

.custom-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-primary, #ccc);
  padding: 2px 0;
  min-width: 0;
}

.custom-input::placeholder {
  color: var(--text-tertiary, #555);
}

/* ── 底栏 ── */
.clarify-footer {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  padding: 8px 14px 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.05);
  background: rgba(0, 0, 0, 0.18);
}

.cbtn {
  height: 26px;
  min-width: 60px;
  padding: 0 12px;
  font-size: 11px;
  font-family: inherit;
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  transition: all 0.12s ease;
  outline: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 3px;
}

.cbtn.skip {
  background: transparent;
  color: var(--text-tertiary, #767676);
  border: 1px solid var(--border-default, #3e3e42);
}

.cbtn.skip:hover:not(:disabled) {
  background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  color: var(--text-secondary, #969696);
}

.cbtn.done {
  background: var(--card-perm-btn-bg, #3a5a3a);
  color: var(--card-perm-btn-text, #b0e0b0);
  border: 1px solid var(--card-perm-btn-border, #4a8a4a);
  font-weight: 500;
}

.cbtn.done:hover:not(:disabled) {
  background: var(--card-perm-btn-hover, #4a6a4a);
  border-color: var(--card-perm-resolved, #4a8a4a);
}

.cbtn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* ── 过渡动画 ── */
.clarify-fade-enter-active,
.clarify-fade-leave-active {
  transition: opacity 0.15s ease;
}

.clarify-fade-enter-active .clarify-dialog,
.clarify-fade-leave-active .clarify-dialog {
  transition: transform 0.15s ease, opacity 0.15s ease;
}

.clarify-fade-enter-from,
.clarify-fade-leave-to {
  opacity: 0;
}

.clarify-fade-enter-from .clarify-dialog,
.clarify-fade-leave-to .clarify-dialog {
  transform: scale(0.97) translateY(4px);
  opacity: 0;
}
</style>
