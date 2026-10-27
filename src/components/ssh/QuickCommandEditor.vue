<template>
  <Teleport to="body">
  <div v-if="visible" class="qc-editor-overlay">
    <div ref="dialogRef" class="qc-editor-dialog" role="dialog" aria-modal="true" aria-labelledby="qc-editor-title" tabindex="-1">
      <div class="qc-editor-header">
        <h3 id="qc-editor-title">{{ isEdit ? '编辑快捷命令' : '新建快捷命令' }}</h3>
        <button class="qc-editor-close" @click="handleClose">&times;</button>
      </div>

      <div class="qc-editor-body">
        <div class="qc-form-row">
          <label>命令名称 <span class="qc-char-count">{{ form.name.length }}/30</span></label>
          <input
            v-model="form.name"
            type="text"
            maxlength="30"
            class="qc-input"
            spellcheck="false"
          />
        </div>

        <div class="qc-form-row">
          <label>命令内容 <span class="qc-char-count">{{ form.command.length }}/1000</span></label>
          <textarea
            v-model="form.command"
            rows="4"
            maxlength="1000"
            placeholder="输入要发送到终端的命令..."
            class="qc-textarea"
            spellcheck="false"
          />
        </div>

        <div class="qc-form-row">
          <label>颜色标识</label>
          <div class="qc-color-grid">
            <button
              v-for="c in presetColors"
              :key="c"
              class="qc-color-swatch"
              :class="{ active: form.color === c }"
              :style="{ backgroundColor: c }"
              @click="form.color = c"
            />
            <input
              v-model="form.color"
              type="color"
              class="qc-color-custom"
              title="自定义颜色"
            />
          </div>
        </div>
      </div>

      <div class="qc-editor-footer">
        <button class="qc-btn secondary" @click="handleClose">取消</button>
        <button
          class="qc-btn primary"
          :disabled="!canSave"
          @click="handleSave"
        >
          保存
        </button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import type { QuickCommand } from '@/stores/quickCommands'
import { PRESET_COLORS } from '@/stores/quickCommands'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'

const props = defineProps<{
  visible: boolean
  editData: QuickCommand | null
}>()

const emit = defineEmits<{
  close: []
  save: [data: { name: string; command: string; color: string }]
}>()

const presetColors = PRESET_COLORS

// 弹窗常驻挂载、靠 visible 开关：打开时接管 Tab 环形，关掉后还给触发它的按钮；
// 初始焦点就是名称输入框（原先靠打开后单独 nextTick 抢焦点，现已并入一处）
const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef, { initialFocus: '.qc-input' })

const form = ref({
  name: '',
  command: '',
  color: presetColors[0],
})

const isEdit = computed(() => props.editData !== null)
const canSave = computed(() => form.value.name.trim().length > 0 && form.value.command.trim().length > 0)

watch(() => props.visible, (show) => {
  if (!show) {
    deactivate()
    return
  }
  activate()
  if (props.editData) {
    form.value = {
      name: props.editData.name,
      command: props.editData.command,
      color: props.editData.color || presetColors[0],
    }
  } else {
    form.value = { name: '', command: '', color: presetColors[0] }
  }
})

function handleClose() {
  emit('close')
}

// Escape 关闭
function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', handleKeydown))
onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
  deactivate()
})

function handleSave() {
  if (!canSave.value) return
  emit('save', {
    name: form.value.name.trim(),
    command: form.value.command.trimEnd(),
    color: form.value.color,
  })
}
</script>

<style scoped>
.qc-editor-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.qc-editor-dialog {
  width: 420px;
  max-width: 90vw;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  /* 容器只是初始焦点落点（tabindex=-1），描边交给里面的输入框 */
  outline: none;
  overflow: hidden;
  font-family: var(--font-sans);
}

.qc-editor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-subtle);
}

.qc-editor-header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.qc-editor-close {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-size: 20px;
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.qc-editor-close:hover {
  background: var(--border-subtle);
  color: var(--text-primary);
}

.qc-editor-body {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.qc-form-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.qc-form-row label {
  font-size: 12px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.qc-char-count {
  color: var(--text-tertiary);
  font-size: 11px;
}

.qc-input,
.qc-textarea {
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 8px 10px;
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-mono);
  outline: none;
  resize: vertical;
  transition: border-color 0.15s;
}

.qc-input:focus,
.qc-textarea:focus {
  border-color: var(--accent-primary);
}

.qc-input::placeholder,
.qc-textarea::placeholder {
  color: var(--text-tertiary);
}

.qc-color-grid {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.qc-color-swatch {
  width: 26px;
  height: 26px;
  border-radius: var(--radius-sm);
  border: 2px solid var(--border-subtle);
  cursor: pointer;
  padding: 0;
  transition: border-color 0.15s;
}

.qc-color-swatch:hover {
  border-color: var(--text-secondary);
}

.qc-color-swatch.active {
  border-color: var(--text-primary);
  box-shadow: 0 0 0 1px var(--text-primary);
}

.qc-color-custom {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: transparent;
  cursor: pointer;
}

.qc-color-custom::-webkit-color-swatch-wrapper {
  padding: 0;
}

.qc-color-custom::-webkit-color-swatch {
  border: none;
  border-radius: var(--radius-sm);
}

.qc-editor-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 16px;
  border-top: 1px solid var(--border-subtle);
}

.qc-btn {
  padding: 6px 14px;
  border-radius: var(--radius-md);
  font-size: 13px;
  font-family: var(--font-sans);
  cursor: pointer;
  border: none;
  transition: all 0.15s;
}

.qc-btn.primary {
  background: var(--accent-primary);
  color: var(--text-primary);
  font-weight: 500;
}

.qc-btn.primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent-primary) 85%, black);
}

.qc-btn.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.qc-btn.secondary {
  background: transparent;
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
}

.qc-btn.secondary:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
</style>
