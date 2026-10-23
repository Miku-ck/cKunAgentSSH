# 意图澄清卡去弹窗化（并入审批决策面）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把意图澄清（ClarifyingWizard）从 Teleport 居中弹窗改为与审批卡同风格的非模态决策卡，挂进输入区上方的 ApprovalDock。

**Architecture:** 纯前端呈现层重构。ClarifyingWizard.vue 原地改卡形态复用 `approval-card.css` 的 `.ap-*` 骨架；挂载点从 AgentRunPanel 搬到 ApprovalDock；store 状态机、SSE 事件、answer/skip 端点零改动。守卫采用本仓既有的源码/CSS 文本断言手法（`?raw` import）。

**Tech Stack:** Vue 3 `<script setup>` + TypeScript + Pinia + Vitest（`?raw` 文本守卫）。

**Spec:** `docs/superpowers/specs/2026-10-23-clarification-card-design.md`

## Global Constraints

- 不新增任何第三方依赖。
- 全部走 CSS 变量令牌（`--card-perm-*` / `--status-warn-*` / `--bg-*` 等），不写死色值；卡骨架/按钮复用全局 `src/styles/approval-card.css` 的 `.ap-*`，不在组件内复制一份。
- 后端零改动：不动 ClarificationInterceptor、`clarification_needed`/`clarification_done` SSE 事件与 answer/skip 端点契约，不跑 pytest。
- 提交遵循约定式格式；共享工作树精准提交 —— 每个 commit 只 `git add` 本任务涉及的目标文件，不 `git add .`、不触碰工作区里其他 WIP。
- 验证命令：结构/类型用 `npm run typecheck`；单测用 `npm run test`（等价 `vitest run`）。

---

### Task 1: 澄清卡去弹窗、挂进 ApprovalDock

**Files:**
- Modify: `src/components/agent/ClarifyingWizard.vue`（整体重写为卡形态）
- Modify: `src/components/agent/ApprovalDock.vue`（引入并渲染澄清卡 + 扩 v-if）
- Modify: `src/components/agent/AgentRunPanel.vue`（摘除澄清卡挂载与 import + 改注释）
- Modify: `src/components/home/HomeWorkspace.vue`（更新挂载历史注释，无结构改动）
- Test: `src/components/agent/approval-trace-boundary.test.ts`（新增守卫）

**Interfaces:**
- Consumes: `agent.clarificationWizard`（store 暴露的 `ClarificationWizardState | null`，字段 `runId/tool/args/reason/currentStep/totalSteps/question/options/allowCustom/steps/answers/submitted`）；`agentStore.submitClarification`/`skipClarification`/`clearClarification`；全局 `.ap-card/.ap-head/.ap-body/.ap-actions/.ap-btn/.ap-btn-grant/.ap-btn-deny` 类。
- Produces: `<ClarifyingWizard />` 作为纯展示决策卡，仅由 ApprovalDock 消费；组件不再有 `visible`/Teleport/全局键盘对外契约。

- [ ] **Step 1: 写失败守卫**

在 `src/components/agent/approval-trace-boundary.test.ts` 顶部 import 区追加两行（紧跟现有 `import approvalCardCss ...` 之后）：

```ts
import clarifyCard from '@/components/agent/ClarifyingWizard.vue?raw'
```

在文件末尾（最后一个 `describe` 之后）追加：

```ts
describe('意图澄清并入决策面（不再是弹窗）', () => {
  // 澄清卡曾是 Teleport 居中模态；2026-10-23 起与审批同为输入区上方非模态决策卡。
  // 这组守卫钉住：唯一渲染位在 ApprovalDock、AgentRunPanel 不再挂它、卡本体不是模态。
  it('澄清卡只挂在 ApprovalDock，不再挂在消息流末尾的 AgentRunPanel', () => {
    expect(approvalDock).toContain('ClarifyingWizard')
    expect(approvalDock).toContain('clarificationWizard')
    expect(agentRunPanel).not.toContain('ClarifyingWizard')
  })

  it('澄清卡不是模态弹窗：无 Teleport / 遮罩 / aria-modal / 全局 Esc', () => {
    expect(clarifyCard).not.toContain('Teleport')
    expect(clarifyCard).not.toContain('clarify-overlay')
    expect(clarifyCard).not.toContain('aria-modal')
    expect(clarifyCard).not.toMatch(/Escape/)
    expect(clarifyCard).toContain('ap-card')     // 复用审批卡骨架
  })

  it('澄清卡与审批卡同限高，头/动作常驻、正文内滚', () => {
    expect(cssBody(clarifyCard, '.ap-clarify')).toMatch(/max-height:\s*min\(38vh,\s*340px\)/)
  })
})
```

- [ ] **Step 2: 跑到失败**

Run: `npx vitest run src/components/agent/approval-trace-boundary.test.ts`
Expected: 新增 describe 内 3 条 FAIL（`approvalDock` 尚不含 ClarifyingWizard；`clarifyCard` 仍含 Teleport/Escape/aria-modal；`agentRunPanel` 仍含 ClarifyingWizard；`.ap-clarify` 尚不存在）。

- [ ] **Step 3: 重写 ClarifyingWizard.vue**

用下列完整内容整体替换 `src/components/agent/ClarifyingWizard.vue`（删除 Teleport/遮罩/过渡/`visible`/`nextBtnRef`/全局 `keydown` 监听与 `onUnmounted`；卡骨架与按钮走 `.ap-*`；逻辑与状态机原样保留，多步推进去掉抢焦点的 `nextTick`）：

```vue
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
  border-radius: 2px;
  background: var(--bg-hover);
}
.ctx-cmd-text {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--status-info-text);
  background: var(--bg-input);
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
  border-radius: 2px;
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
```

注：组件内的任何注释不能写 `Teleport` / `aria-modal` / `clarify-overlay` / `Escape` 这些字面量（哪怕只是历史说明）—— Step 1 的 `clarifyCard).not.toContain(...)` 守卫会因此扑空；要描述旧形态用“全屏遮罩的居中弹窗”“全局键盘监听”这类中文。

- [ ] **Step 4: 挂进 ApprovalDock.vue**

改 `src/components/agent/ApprovalDock.vue`：

(a) import 区追加（紧跟 `import DeliveryCard from './DeliveryCard.vue'` 之后）：

```ts
import ClarifyingWizard from './ClarifyingWizard.vue'
```

(b) script 里在 `const deliveries = ...` 之后追加：

```ts
const wizard = computed(() => agent.clarificationWizard)
```

(c) template 根 `<section>` 条件与首个子节点：

把
```html
  <section v-if="pending.length || deliveries.length" class="approval-dock" aria-label="待放行的操作">
    <template v-if="pending.length">
```
改为
```html
  <section v-if="pending.length || deliveries.length || wizard" class="approval-dock" aria-label="待放行的操作">
    <!-- 意图澄清：run 阻塞在澄清上，先答它才有后续审批，故排在权限队列之前 -->
    <ClarifyingWizard v-if="wizard" />
    <template v-if="pending.length">
```

- [ ] **Step 5: 从 AgentRunPanel.vue 摘除**

改 `src/components/agent/AgentRunPanel.vue`：

(a) 删除 import 行 `import ClarifyingWizard from './ClarifyingWizard.vue'`。

(b) 删除模板里的 `<ClarifyingWizard />`（原 `<template>` 下第一行）。

(c) 把头部注释里的区块列表条目：
```
 * - 澄清向导（ClarifyingWizard：自带 visible 门控 + Teleport，不受下方整块开关影响）
```
改为：
```
 * - （意图澄清已并入输入区上方 ApprovalDock 的澄清卡，不在此面板）
```
注：这条注释不能写 `ClarifyingWizard` 字面量 —— Step 1 的守卫 `agentRunPanel).not.toContain('ClarifyingWizard')` 会因此扑空。

- [ ] **Step 6: 更新 HomeWorkspace.vue 注释**

把消息流末尾 `<AgentRunPanel />` 上方的注释块第二点：
```
               ② 它不在 isEmpty 的 v-else 分支里 —— 消息为空时整块（含澄清向导）曾经
                  跟着一起不挂载，那是这份面板最早踩过的挂载洞，洞的形状由位置决定，
                  不由「今天有没有空态下的 run」决定。 -->
```
改为：
```
               ② 它不在 isEmpty 的 v-else 分支里 —— 消息为空时整块曾经跟着一起不挂载，
                  那是这份面板最早踩过的挂载洞，洞的形状由位置决定，不由「今天有没有
                  空态下的 run」决定。注：意图澄清已移入输入区上方的 ApprovalDock。 -->
```

- [ ] **Step 7: 跑到通过**

Run: `npx vitest run src/components/agent/approval-trace-boundary.test.ts src/components/home/HomeWorkspace.layout.test.ts`
Expected: 全部 PASS（新守卫绿；历史弹窗守卫不受影响）。

Run: `npm run typecheck`
Expected: 无错误（组件不再有 `visible`/`nextBtnRef`/`onUnmounted`/`nextTick` 悬空引用）。

- [ ] **Step 8: 全量测试 + 提交**

Run: `npm run test`
Expected: 全绿（`agent.test.ts` 的 clarification 状态机不受影响）。

```bash
git add src/components/agent/ClarifyingWizard.vue src/components/agent/ApprovalDock.vue src/components/agent/AgentRunPanel.vue src/components/home/HomeWorkspace.vue src/components/agent/approval-trace-boundary.test.ts
git commit -m "refactor(agent): 意图澄清卡去弹窗化并入审批决策面"
```

---

### Task 2: 澄清提醒 toast 文案对齐审批句式

**Files:**
- Modify: `src/stores/agent.ts`（`clarification_needed` 分支末尾的 `warning` 文案）
- Test: `src/components/agent/approval-trace-boundary.test.ts`（新增文案守卫）

**Interfaces:**
- Consumes: 无（仅字符串常量）。
- Produces: `clarification_needed` 新建向导时提示文案 = `AI 需要你澄清意图，回首页在输入框上方作答`（就地刷新分支在此之前已 `break`，不弹，行为不变）。

- [ ] **Step 1: 写失败守卫**

在 `src/components/agent/approval-trace-boundary.test.ts` 顶部 import 区追加：

```ts
import agentStoreSource from '@/stores/agent.ts?raw'
```

在 Task 1 新增的 `describe('意图澄清并入决策面（不再是弹窗）', ...)` 内追加一条 it：

```ts
  it('澄清提醒 toast 文案对齐审批句式（回首页在输入框上方作答）', () => {
    expect(agentStoreSource).toContain('AI 需要你澄清意图，回首页在输入框上方作答')
    expect(agentStoreSource).not.toContain('需要对不明确的操作进行确认')
  })
```

- [ ] **Step 2: 跑到失败**

Run: `npx vitest run src/components/agent/approval-trace-boundary.test.ts`
Expected: 新增 it FAIL（现文案仍是「AI 需要对不明确的操作进行确认」）。

- [ ] **Step 3: 改文案**

在 `src/stores/agent.ts` 的 `clarification_needed` 分支末尾，把：

```ts
        useToastStore().warning('AI 需要对不明确的操作进行确认')
```

改为：

```ts
        useToastStore().warning('AI 需要你澄清意图，回首页在输入框上方作答')
```

- [ ] **Step 4: 跑到通过**

Run: `npx vitest run src/components/agent/approval-trace-boundary.test.ts`
Expected: 全部 PASS。

- [ ] **Step 5: 全量 + 类型 + 提交**

Run: `npm run test`（全绿）；`npm run typecheck`（无错误）。

```bash
git add src/stores/agent.ts src/components/agent/approval-trace-boundary.test.ts
git commit -m "refactor(agent): 澄清提醒 toast 文案对齐审批句式"
```

---

## 验收清单

- 意图澄清不再是全屏遮罩弹窗；以一张 `.ap-card.ap-clarify` 卡出现在首页输入框上方的 ApprovalDock，排在权限审批队列之前，风格与审批卡一致。
- 提交/跳过/多步推进/run_id 校验/立即关闭等交互行为与改造前一致（store 状态机零改动）。
- 全局 Esc 监听、Teleport、遮罩、`aria-modal` 全部移除，卡内自定义输入的 Enter 提交保留。
- `approval-trace-boundary.test.ts` 4 条新守卫 + 原有守卫全绿；`agent.test.ts` 全绿；`typecheck` 无错误。
