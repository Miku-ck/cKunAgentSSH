<template>
  <Teleport to="body">
    <div v-if="visible" class="chmod-overlay" @click.self="cancel">
      <div ref="dialogRef" class="chmod-dialog" tabindex="-1" role="dialog" aria-modal="true" aria-label="修改权限">
        <div class="chmod-head">
          <div class="chmod-title">修改权限</div>
          <div class="chmod-sub" :title="path">{{ name }}</div>
        </div>

        <!-- 当前权限 -->
        <div class="chmod-current">
          <span class="chmod-label">当前</span>
          <span class="chmod-mode">{{ currentMode }}</span>
          <span class="chmod-rwx">{{ currentRwx }}</span>
        </div>

        <!-- rwx 勾选矩阵 -->
        <div class="chmod-grid">
          <span class="chmod-grid-head"></span>
          <span class="chmod-grid-head">读 r</span>
          <span class="chmod-grid-head">写 w</span>
          <span class="chmod-grid-head">执行 x</span>
          <div v-for="row in ROWS" :key="row.label" class="chmod-row">
            <span class="chmod-row-label">{{ row.label }}</span>
            <button
              v-for="bit in row.bits"
              :key="bit.mask"
              class="chmod-check"
              :class="{ on: (mode & bit.mask) !== 0 }"
              :aria-pressed="(mode & bit.mask) !== 0"
              @click="toggle(bit.mask)"
            >{{ (mode & bit.mask) !== 0 ? '✓' : '' }}</button>
          </div>
        </div>

        <!-- 八进制输入 -->
        <div class="chmod-octal">
          <span class="chmod-label">八进制</span>
          <input
            v-model="octalText"
            class="chmod-octal-input"
            spellcheck="false"
            inputmode="numeric"
            maxlength="4"
            @keydown.enter.prevent="confirm"
            @input="onOctalInput"
          />
          <span class="chmod-rwx">{{ rwxText }}</span>
        </div>
        <div v-if="octalError" class="chmod-error">{{ octalError }}</div>

        <!-- 常用预设 -->
        <div class="chmod-presets">
          <button
            v-for="p in PRESETS"
            :key="p.mode"
            class="chmod-preset"
            @click="applyPreset(p.mode)"
          >{{ p.label }}</button>
        </div>

        <div class="chmod-actions">
          <button class="chmod-btn" @click="cancel">取消</button>
          <button class="chmod-btn chmod-btn-primary" :disabled="octalError !== ''" @click="confirm">应用</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'

const props = defineProps<{
  visible: boolean
  /** 目标名（展示用） */
  name: string
  /** 完整路径（展示/标题用） */
  path: string
  /** 当前权限位（来自远端 mode 字符串，见 store.chmodTextToBits） */
  currentBits: number
}>()

const emit = defineEmits<{
  (e: 'apply', mode: number): void
  (e: 'cancel'): void
}>()

const ROWS: Array<{ label: string; bits: Array<{ mask: number }> }> = [
  { label: '所有者', bits: [{ mask: 0o400 }, { mask: 0o200 }, { mask: 0o100 }] },
  { label: '组', bits: [{ mask: 0o040 }, { mask: 0o020 }, { mask: 0o010 }] },
  { label: '其他', bits: [{ mask: 0o004 }, { mask: 0o002 }, { mask: 0o001 }] },
]

const PRESETS: Array<{ label: string; mode: number }> = [
  { label: '644', mode: 0o644 },
  { label: '755', mode: 0o755 },
  { label: '600', mode: 0o600 },
  { label: '777', mode: 0o777 },
]

const mode = ref(props.currentBits)
const octalText = ref(modeToOctal(props.currentBits))
const octalError = ref('')

const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)
watch(() => props.visible, (v) => { if (v) activate(); else deactivate() })
function onKeydown(e: KeyboardEvent): void { if (e.key === 'Escape') emit('cancel') }
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

function modeToOctal(m: number): string {
  return m.toString(8).padStart(3, '0')
}

function toggle(mask: number): void {
  // 与八进制输入双向同步：勾选变化即重算文本
  mode.value = (mode.value & mask) !== 0 ? (mode.value & ~mask) : (mode.value | mask)
  octalText.value = modeToOctal(mode.value)
  octalError.value = ''
}

function onOctalInput(): void {
  const raw = octalText.value.trim()
  if (raw === '') { octalError.value = '请输入权限值'; return }
  if (!/^[0-7]{1,4}$/.test(raw)) { octalError.value = '只允许 0–7，最多 4 位'; return }
  const v = parseInt(raw, 8)
  if (v > 0o7777) { octalError.value = '超出范围（最大 7777）'; return }
  octalError.value = ''
  mode.value = v
}

function applyPreset(m: number): void {
  mode.value = m
  octalText.value = modeToOctal(m)
  octalError.value = ''
}

function rwxChar(bit: number, on: string, off: string): string {
  return ((mode.value & bit) !== 0) ? on : off
}

const rwxText = computed(() => {
  return [0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001]
    .map((b, i) => rwxChar(b, 'rwx'[i % 3], '-'))
    .join('')
})

const currentMode = computed(() => modeToOctal(props.currentBits))
const currentRwx = computed(() => {
  return [0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001]
    .map((b, i) => ((props.currentBits & b) !== 0) ? 'rwx'[i % 3] : '-')
    .join('')
})

function confirm(): void {
  if (octalError.value) return
  emit('apply', mode.value)
}

function cancel(): void {
  emit('cancel')
}

// 每次打开重置到当前权限
watch(
  () => props.visible,
  (v) => {
    if (v) {
      mode.value = props.currentBits
      octalText.value = modeToOctal(props.currentBits)
      octalError.value = ''
    }
  },
  { immediate: true },
)
</script>

<style scoped>
.chmod-overlay {
  position: fixed; inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
  z-index: 1200;
}
.chmod-dialog {
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  outline: none;
  padding: var(--spacing-20);
  width: 380px; max-width: 92vw;
  font-family: var(--font-sans);
  color: var(--text-primary);
}
.chmod-head { margin-bottom: var(--spacing-14); }
.chmod-title { font-size: 1.1538rem; font-weight: 600; }
.chmod-sub {
  font-size: 0.9231rem; opacity: 0.66; margin-top: var(--spacing-4);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chmod-current {
  display: flex; align-items: baseline; gap: var(--spacing-8);
  margin-bottom: var(--spacing-12);
  font-size: 0.9231rem;
}
.chmod-label { opacity: 0.66; }
.chmod-mode { font-variant-numeric: tabular-nums; font-weight: 600; }
.chmod-rwx { font-family: var(--font-mono); opacity: 0.8; letter-spacing: 1px; }

.chmod-grid {
  display: grid;
  grid-template-columns: 4.5rem repeat(3, 1fr);
  gap: var(--spacing-4);
  align-items: center;
  margin-bottom: var(--spacing-14);
}
.chmod-grid-head { font-size: 0.8462rem; opacity: 0.6; text-align: center; }
.chmod-row { display: contents; }
.chmod-row-label { font-size: 0.9231rem; opacity: 0.8; }
.chmod-check {
  height: 1.6rem;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--accent-text);
  font-size: 0.8462rem;
  cursor: pointer;
  line-height: 1;
}
.chmod-check.on {
  background: var(--accent-primary);
  border-color: var(--accent-primary);
  color: var(--accent-on);
}

.chmod-octal {
  display: flex; align-items: center; gap: var(--spacing-8);
  margin-bottom: var(--spacing-6);
  font-size: 0.9231rem;
}
.chmod-octal-input {
  width: 4rem;
  padding: var(--spacing-4) var(--spacing-6);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 0.9231rem;
  text-align: center;
}
.chmod-error { color: var(--danger-text); font-size: 0.8462rem; margin-bottom: var(--spacing-6); }

.chmod-presets { display: flex; gap: var(--spacing-6); margin-bottom: var(--spacing-14); }
.chmod-preset {
  font-size: 0.8462rem; padding: var(--spacing-2) var(--spacing-8);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
  cursor: pointer;
  font-family: var(--font-mono);
}
.chmod-preset:hover { border-color: var(--accent-primary); }

.chmod-actions {
  display: flex; justify-content: flex-end; gap: var(--spacing-8);
  margin-top: var(--spacing-8);
}
.chmod-btn {
  font-size: 1rem; padding: var(--spacing-6) var(--spacing-14);
  border: 1px solid var(--border-default); border-radius: var(--radius-sm);
  background: var(--bg-input); color: var(--text-primary);
  cursor: pointer;
}
.chmod-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.chmod-btn-primary {
  background: var(--accent-primary);
  border-color: var(--accent-primary);
  color: var(--accent-on);
}
</style>
