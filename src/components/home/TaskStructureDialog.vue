<template>
  <Teleport to="body">
    <div class="ts-overlay" @click.self="$emit('close')">
      <div
        ref="dialogRef"
        class="ts-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ts-title"
        tabindex="-1"
      >
        <div class="ts-head">
          <h2 id="ts-title" class="ts-title">长任务结构化</h2>
          <span class="ts-sub">对照原文 · 未采用不会改动输入框</span>
          <button type="button" class="ts-close" title="关闭" aria-label="关闭" @click="$emit('close')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>

        <div class="ts-body">
          <!-- 左：原文（锁定基准，只读） -->
          <section class="ts-col ts-orig">
            <header class="ts-col-h">原文（基准 · 不可编辑）</header>
            <pre class="ts-orig-text">{{ original }}</pre>
          </section>

          <!-- 右：结构化稿 -->
          <section class="ts-col ts-struct">
            <header class="ts-col-h">结构化草稿</header>

            <div class="ts-target">
              <span class="ts-field-label">目标主机</span>
              <span class="ts-host" :title="'来源：当前选定目标，模型不得改动'">{{ structure.fields.host.value || '（未选远程目标）' }}</span>
              <span class="ts-env" :class="`ts-env-${structure.fields.environment}`">{{ structure.fields.environment === 'prod' ? '生产' : '测试' }}</span>
            </div>

            <div v-if="structure.steps.length" class="ts-block">
              <div class="ts-block-h">步骤</div>
              <label
                v-for="s in structure.steps"
                :key="s.id"
                class="ts-step"
                :class="{ 'ts-step-added': s.requires_confirm }"
              >
                <input
                  v-if="s.requires_confirm"
                  type="checkbox"
                  class="ts-check"
                  :checked="confirmedAdded.includes(s.id)"
                  @change="$emit('toggle-confirm', s.id)"
                />
                <span v-else class="ts-step-dot" aria-hidden="true">·</span>
                <span class="ts-step-text">{{ s.text }}</span>
                <ProvenanceTag :provenance="s.provenance" />
              </label>
            </div>

            <div class="ts-block">
              <div class="ts-block-h">运维字段</div>
              <div v-for="f in fieldRows" :key="f.key" class="ts-field">
                <span class="ts-field-label">{{ f.label }}</span>
                <span class="ts-field-value">{{ f.value || '—' }}</span>
                <ProvenanceTag :provenance="f.provenance" />
              </div>
            </div>

            <div v-if="structure.ambiguities.length" class="ts-block">
              <div class="ts-block-h">待澄清</div>
              <div v-for="a in structure.ambiguities" :key="a.id" class="ts-amb">
                <div class="ts-amb-q">{{ a.question }}</div>
                <div v-if="a.why" class="ts-amb-why">{{ a.why }}</div>
              </div>
            </div>
          </section>
        </div>

        <footer class="ts-foot">
          <button type="button" class="btn btn-ghost" @click="$emit('revert')">恢复原文</button>
          <span v-if="!allAddedConfirmed && hasAdded" class="ts-hint">需先确认全部「新增·需确认」步骤</span>
          <button type="button" class="btn btn-ghost" @click="$emit('close')">取消</button>
          <button
            type="button"
            class="btn btn-primary"
            :disabled="!allAddedConfirmed"
            @click="$emit('accept')"
          >采用并填入输入框</button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
/**
 * 长任务结构化对照弹窗（spec 2026-10-24-task-structuring · 接缝 S7）
 *
 * 居中弹窗（不是抽屉 —— DESIGN.md 禁止抽屉式交互）。左原文锁定、右结构可核对，
 * 组件本身不碰 store：采用/回退/确认都由宿主（HomeComposer 经 useTaskStructuring）
 * 裁决，这里只呈现事实与回抛意图 —— 纯展示，可被"绝不静默替换"的守卫测试安心信任。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'
import type { StructureResponse } from '@/utils/task-structure-core'
import ProvenanceTag from './ProvenanceTag.vue'

const props = defineProps<{
  structure: StructureResponse
  original: string
  confirmedAdded: string[]
  allAddedConfirmed: boolean
}>()

const emit = defineEmits<{
  close: []
  accept: []
  revert: []
  'toggle-confirm': [id: string]
}>()

const hasAdded = computed(() => props.structure.steps.some(s => s.requires_confirm))

const fieldRows = computed(() => {
  const f = props.structure.fields
  return [
    { key: 'impact_scope', label: '影响范围', value: f.impact_scope.value, provenance: f.impact_scope.provenance },
    { key: 'rollback', label: '回滚方案', value: f.rollback.value, provenance: f.rollback.provenance },
    { key: 'pre_checks', label: '前置检查', value: f.pre_checks.value, provenance: f.pre_checks.provenance },
    { key: 'change_window', label: '变更窗口', value: f.change_window.value, provenance: f.change_window.provenance },
  ]
})

const dialogRef = ref<HTMLElement | null>(null)
// 这份弹窗原先只管 Esc：Tab 会跑到背后的输入框，关掉后焦点也留在 body 上。
const { activate, deactivate } = useModalFocusTrap(dialogRef, { initialFocus: '.ts-close' })

function onWindowKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    // 关闭 = 不采用：宿主的 close() 清基准，sharedDraft 从未被动过。
    emit('close')
  }
}

onMounted(() => {
  activate()
  window.addEventListener('keydown', onWindowKeydown)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onWindowKeydown)
  deactivate()
})
</script>

<style scoped>
.ts-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  animation: ts-fade-in 140ms ease-out;
}

.ts-dialog {
  width: min(860px, calc(100vw - 48px));
  max-height: min(680px, calc(100vh - 96px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-lg);
  outline: none;
  animation: ts-dialog-in 170ms cubic-bezier(0.22, 1, 0.36, 1);
}

.ts-head {
  flex: 0 0 auto;
  height: 48px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  border-bottom: 1px solid var(--border-subtle);
}

.ts-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.ts-sub {
  font-size: 11px;
  color: var(--text-tertiary);
}

.ts-close {
  margin-left: auto;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.ts-close:hover { background: var(--bg-hover); color: var(--text-primary); }
.ts-close svg { width: 13px; height: 13px; }

.ts-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr 1.2fr;
  gap: 1px;
  background: var(--border-subtle);
  overflow: hidden;
}

.ts-col {
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px;
  background: var(--bg-elevated);
}

.ts-col-h {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  margin-bottom: 8px;
}

.ts-orig-text {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
}

.ts-target {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  margin-bottom: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-base);
}

.ts-host {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
}

.ts-env {
  margin-left: auto;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
}

.ts-env-test { color: var(--status-success-text, var(--text-secondary)); }
.ts-env-prod {
  color: var(--status-warn-text);
  background: color-mix(in srgb, var(--status-warn-text) 14%, transparent);
}

.ts-block { margin-bottom: 14px; }
.ts-block-h {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.ts-step {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--text-primary);
}

.ts-step-added {
  background: color-mix(in srgb, var(--status-warn-text) 10%, transparent);
}

.ts-step-dot { color: var(--text-tertiary); }
.ts-check { margin-top: 2px; }
.ts-step-text { flex: 1; line-height: 1.5; }

.ts-field {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 12px;
}

.ts-field-label { color: var(--text-tertiary); min-width: 60px; }
.ts-field-value { flex: 1; color: var(--text-primary); }

.ts-amb { padding: 4px 0; }
.ts-amb-q { font-size: 12px; color: var(--text-primary); }
.ts-amb-why { font-size: 11px; color: var(--text-tertiary); }

.ts-foot {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 10px 14px;
  border-top: 1px solid var(--border-subtle);
}

.ts-hint {
  margin-right: auto;
  font-size: 11px;
  color: var(--status-warn-text);
}

@keyframes ts-fade-in { from { opacity: 0; } }
@keyframes ts-dialog-in { from { opacity: 0; transform: scale(0.96); } }

@media (prefers-reduced-motion: reduce) {
  .ts-overlay, .ts-dialog { animation: none; }
}
</style>
