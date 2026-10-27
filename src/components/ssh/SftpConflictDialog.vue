<template>
  <Teleport to="body">
    <div v-if="visible" class="conflict-overlay" @click.self="cancel">
      <div ref="dialogRef" class="conflict-dialog" tabindex="-1" role="dialog" aria-modal="true" aria-label="文件冲突处理">
        <div class="conflict-head">
          <div class="conflict-title">目标位置存在同名文件</div>
          <div class="conflict-sub">
            共 {{ items.length }} 项冲突，逐项选择处理方式，或用上方「全部」按钮统一处理。
            选择覆盖时，同名目录会合并，目录内同名文件将被覆盖。
          </div>
        </div>

        <!-- 应用到全部 -->
        <div class="conflict-bulk">
          <span class="conflict-bulk-label">全部</span>
          <button class="conflict-bulk-btn" @click="applyAll('overwrite')">覆盖</button>
          <button class="conflict-bulk-btn" @click="applyAll('skip')">跳过</button>
          <button class="conflict-bulk-btn" @click="applyAll('rename')">重命名</button>
        </div>

        <!-- 表头 -->
        <div class="conflict-row conflict-row-head">
          <span>文件</span>
          <span>要传入的</span>
          <span>已存在的</span>
          <span>处理</span>
        </div>

        <div class="conflict-body">
          <div v-for="item in items" :key="item.id" class="conflict-row">
            <div class="cf-name" :title="item.name">
              <span class="cf-name-text">{{ item.name }}</span>
            </div>
            <div class="cf-meta">{{ metaText(item.source) }}</div>
            <div class="cf-meta cf-meta-warn">{{ metaText(item.target) }}</div>
            <div class="cf-action">
              <div class="cf-seg">
                <button
                  v-for="opt in ACTION_OPTS"
                  :key="opt.v"
                  class="cf-seg-btn"
                  :class="{ on: choices[item.id] === opt.v }"
                  @click="setAction(item, opt.v)"
                >{{ opt.t }}</button>
              </div>
              <input
                v-if="choices[item.id] === 'rename'"
                v-model="names[item.id]"
                class="cf-rename-input"
                spellcheck="false"
                @keydown.enter.prevent="confirm"
              />
            </div>
          </div>
        </div>

        <div class="conflict-actions">
          <button class="conflict-btn" @click="cancel">取消</button>
          <button class="conflict-btn conflict-btn-primary" @click="confirm">开始传输</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'
import { formatBytes, formatRelativeTime } from '@/utils/format'
import { autoRename, type ConflictAction, type ConflictDecision, type ConflictItem, type ConflictMeta } from '@/utils/transfer-conflict'

const props = defineProps<{
  visible: boolean
  items: ConflictItem[]
}>()

const emit = defineEmits<{
  (e: 'resolve', decisions: ConflictDecision[]): void
  (e: 'cancel'): void
}>()

const ACTION_OPTS: Array<{ v: ConflictAction; t: string }> = [
  { v: 'overwrite', t: '覆盖' },
  { v: 'skip', t: '跳过' },
  { v: 'rename', t: '重命名' },
]

const choices = reactive<Record<string, ConflictAction>>({})
const names = reactive<Record<string, string>>({})

const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)
// 必答题：打开即接管焦点（Tab 圈在弹窗内）
watch(() => props.visible, (v) => { if (v) activate(); else deactivate() })
// Esc 走 window：原先写在容器上（@keydown.esc），焦点不在弹窗内就收不到
function onKeydown(e: KeyboardEvent): void { if (e.key === 'Escape') emit('cancel') }
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

// 一批冲突的目标名全部已占用，作为重命名自动命名的初始占用集，逐行叠加已生成的新名。
function suggestName(item: ConflictItem): string {
  const taken = new Set(props.items.map(i => i.name))
  for (const id of Object.keys(choices)) {
    if (choices[id] === 'rename' && names[id]) taken.add(names[id])
  }
  return autoRename(item.name, taken)
}

function setAction(item: ConflictItem, action: ConflictAction): void {
  choices[item.id] = action
  if (action === 'rename' && !names[item.id]) names[item.id] = suggestName(item)
}

function applyAll(action: ConflictAction): void {
  for (const item of props.items) setAction(item, action)
}

function metaText(m: ConflictMeta): string {
  // modified 为 0（未知/目录）时只显示大小
  const t = m.modified ? formatRelativeTime(m.modified, Date.now(), '') : ''
  const size = formatBytes(m.size)
  return t ? `${size} · ${t}` : size
}

function buildDecisions(): ConflictDecision[] {
  return props.items.map(item => ({
    path: item.path,
    action: choices[item.id] || 'overwrite',
    newName: choices[item.id] === 'rename' ? names[item.id] : undefined,
  }))
}

function confirm(): void {
  emit('resolve', buildDecisions())
}

function cancel(): void {
  emit('cancel')
}

// 新一批冲突进来时重置为「全部覆盖」，与旧版默认行为一致，用户可按需改选。
watch(
  () => props.items,
  (items) => {
    for (const key of Object.keys(choices)) delete choices[key]
    for (const key of Object.keys(names)) delete names[key]
    for (const item of items) choices[item.id] = 'overwrite'
  },
  { immediate: true },
)
</script>

<style scoped>
.conflict-overlay {
  position: fixed; inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
  z-index: 1200;
}
.conflict-dialog {
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  /* 容器只是初始焦点落点（tabindex=-1），描边交给里面的控件 */
  outline: none;
  padding: var(--spacing-20);
  width: 640px; max-width: 92vw;
  font-family: var(--font-sans);
  color: var(--text-primary);
}
.conflict-head { margin-bottom: var(--spacing-14); }
.conflict-title { font-size: 1.1538rem; font-weight: 600; }
.conflict-sub { font-size: 0.9231rem; opacity: 0.66; margin-top: var(--spacing-4); }
.conflict-bulk {
  display: flex; align-items: center; gap: var(--spacing-8);
  margin-bottom: var(--spacing-10);
}
.conflict-bulk-label { font-size: 0.9231rem; opacity: 0.66; }
.conflict-bulk-btn {
  font-size: 0.9231rem; padding: var(--spacing-4) var(--spacing-10);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
  cursor: pointer;
}
.conflict-bulk-btn:hover { border-color: var(--accent-primary); }

.conflict-row {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.8fr);
  gap: var(--spacing-10); align-items: center;
  padding: var(--spacing-6) 0;
  font-size: 0.9231rem;
}
.conflict-row-head {
  opacity: 0.6; font-weight: 600;
  border-bottom: 1px solid var(--border-subtle);
  padding-bottom: var(--spacing-6);
}
.conflict-body {
  max-height: 320px; overflow-y: auto;
}
.cf-name { min-width: 0; }
.cf-name-text {
  display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cf-meta { font-variant-numeric: tabular-nums; opacity: 0.85; }
.cf-meta-warn { opacity: 1; }

.cf-action { display: flex; flex-direction: column; gap: var(--spacing-4); }
.cf-seg { display: inline-flex; border: 1px solid var(--border-default); border-radius: var(--radius-sm); overflow: hidden; }
.cf-seg-btn {
  font-size: 0.9231rem; padding: 3px 9px;
  background: var(--bg-input); color: var(--text-primary);
  border: none; cursor: pointer;
  border-right: 1px solid var(--border-default);
}
.cf-seg-btn:last-child { border-right: none; }
.cf-seg-btn.on { background: var(--accent-primary); color: var(--accent-on); }
.cf-rename-input {
  width: 100%; height: 24px; padding: 0 var(--spacing-8);
  border: 1px solid var(--border-default); border-radius: var(--radius-sm);
  background: var(--bg-input); color: var(--text-primary);
  font-size: 0.9231rem; outline: none;
}
.cf-rename-input:focus { border-color: var(--accent-primary); }

.conflict-actions {
  display: flex; justify-content: flex-end; gap: var(--spacing-8);
  margin-top: var(--spacing-16);
}
.conflict-btn {
  font-size: 1rem; padding: var(--spacing-6) var(--spacing-14);
  border: 1px solid var(--border-default); border-radius: var(--radius-sm);
  background: var(--bg-input); color: var(--text-primary);
  cursor: pointer;
}
.conflict-btn-primary {
  background: var(--accent-primary);
  border-color: var(--accent-primary);
  color: var(--accent-on);
}
</style>
