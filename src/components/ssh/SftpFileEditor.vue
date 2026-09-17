<template>
  <div v-if="visible" class="editor-overlay" @click.self="handleClose">
    <div class="editor-dialog">
      <!-- 头部 -->
      <div class="editor-header">
        <div class="header-left">
          <svg class="file-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
            <polyline points="13 2 13 9 20 9"/>
          </svg>
          <span class="file-name">{{ name }}</span>
          <span class="file-path">{{ path }}</span>
          <span v-if="dirty" class="dirty-dot" title="未保存">●</span>
          <span v-if="readOnly && !loading && !error" class="readonly-badge">只读</span>
        </div>
        <div class="header-right">
          <button
            class="save-btn"
            :disabled="!dirty || readOnly || saving || loading || !!error"
            @click="save"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
              <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
              <polyline points="17 21 17 13 7 13 7 21"/>
              <polyline points="7 3 7 8 15 8"/>
            </svg>
            保存
          </button>
          <button class="close-btn" @click="handleClose" title="关闭">×</button>
        </div>
      </div>

      <!-- 加载态 -->
      <div v-if="loading" class="editor-state">
        <svg class="spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10" stroke-opacity="0.25"/>
          <path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/>
        </svg>
        <span>加载中...</span>
      </div>
      <!-- 错误态 -->
      <div v-else-if="error" class="editor-state error">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="32" height="32">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <span class="error-msg">{{ error }}</span>
        <button class="error-close" @click="emit('close')">关闭</button>
      </div>
      <!-- 编辑器 -->
      <div v-else class="editor-body">
        <div ref="container" class="monaco-container"></div>
      </div>

      <!-- 状态栏 -->
      <div class="editor-footer">
        <span class="footer-item">{{ language }}</span>
        <span class="footer-sep">|</span>
        <span class="footer-item">UTF-8</span>
        <span class="footer-sep">|</span>
        <span class="footer-item">{{ formatBytes(size) }}</span>
        <template v-if="!loading && !error">
          <span class="footer-sep">|</span>
          <span class="footer-item">行 {{ cursorPos.line }}, 列 {{ cursorPos.col }}</span>
        </template>
        <span v-if="readOnly && !loading && !error" class="footer-item readonly-hint">
          {{ readOnlyReason }}
        </span>
        <span v-if="side === 'remote' && !loading && !error" class="footer-item remote-tag">
          远程
        </span>
      </div>
    </div>

    <!-- 未保存关闭确认 -->
    <div v-if="unsavedDialog.visible" class="unsaved-overlay" @click.self="dismissUnsavedDialog">
      <div class="unsaved-dialog">
        <div class="unsaved-header">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
            <circle cx="12" cy="12" r="10"/>
            <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
            <line x1="12" y1="17" x2="12.01" y2="17"/>
          </svg>
          <span class="unsaved-title">未保存的更改</span>
        </div>
        <div class="unsaved-body">
          <p>「{{ name }}」已修改，是否保存？</p>
        </div>
        <div class="unsaved-footer">
          <button class="unsaved-btn neutral" @click="dismissUnsavedDialog">取消</button>
          <button class="unsaved-btn discard" @click="discardAndClose">不保存</button>
          <button class="unsaved-btn save" :disabled="saving" @click="saveAndClose">
            {{ saving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { formatBytes } from '@/utils/format'
import { ref, computed, watch, onUnmounted, nextTick, markRaw } from 'vue'
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js'
import { getLanguage } from '@/utils/language'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

const props = defineProps<{
  visible: boolean
  side: 'local' | 'remote'
  path: string
  name: string
  sshSessionId: string
}>()

const emit = defineEmits<{
  close: []
  saved: []
}>()

const toast = useToastStore()

const loading = ref(false)
const error = ref('')
const content = ref('')
const originalContent = ref('')
const size = ref(0)
const readOnly = ref(false)
const language = ref('plaintext')
const dirty = ref(false)
const saving = ref(false)
const cursorPos = ref({ line: 1, col: 1 })

// P3 修复：只读脚注按实际原因显示（原固定"文件较大"）。
// 读取接口（main.ts fs:readFileForEditor / ssh-manager.ts sftpReadFile）仅在
// size > 5MB 时返回 readOnly=true；>10MB 与二进制/无权限都以 error 抛出、
// 不会进入只读分支，故只读分支的原因恒为"文件过大（超过 5MB）"。
const readOnlyReason = computed(() => {
  if (!readOnly.value) return ''
  return `文件较大（${formatBytes(size.value)}，超过 5MB），以只读模式打开`
})

const container = ref<HTMLElement | null>(null)
let editor: monaco.editor.IStandaloneCodeEditor | null = null
let model: monaco.editor.ITextModel | null = null

// 未保存关闭确认
const unsavedDialog = ref({
  visible: false,
})

// visible 变 true 时加载文件
watch(
  () => props.visible,
  async (v) => {
    if (v) {
      await loadFile()
    } else {
      disposeEditor()
    }
  },
)

async function loadFile(): Promise<void> {
  loading.value = true
  error.value = ''
  dirty.value = false
  disposeEditor()
  try {
    // B9 修复：浏览器（非 Electron）下原 `window.electronAPI!` 非空断言
    // 在打开编辑器时即 TypeError — 显式守卫并给出环境提示
    const api = window.electronAPI
    if (!api) {
      error.value = '文件编辑仅 Electron 环境可用'
      return
    }
    let result: EditorFileContent
    if (props.side === 'local') {
      result = await api.fsReadFileForEditor(props.path)
    } else {
      if (!props.sshSessionId) throw new Error('SSH 会话未连接')
      result = await api.sftpReadFile(props.sshSessionId, props.path)
    }
    content.value = result.content
    originalContent.value = result.content
    size.value = result.size
    readOnly.value = result.readOnly
    language.value = getLanguage(props.name)
    loading.value = false
    // 竞态条件守卫：加载期间组件可能已关闭（visible 变为 false）
    if (!props.visible) return
    await nextTick()
    if (!props.visible) return
    initEditor()
  } catch (err: any) {
    error.value = toFriendlyMessage(err, '打开失败')
  } finally {
    loading.value = false
  }
}

function initEditor(): void {
  if (!container.value) return
  disposeEditor()
  model = markRaw(monaco.editor.createModel(content.value, language.value))
  editor = monaco.editor.create(container.value, {
    model,
    // H13 修复：不传 theme，跟随全局 setTheme（由 useAppearance → applyMonacoTheme 控制）
    fontSize: 13,
    fontFamily: 'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, "Courier New", monospace',
    fontLigatures: true,
    minimap: { enabled: true },
    lineNumbers: 'on',
    renderLineHighlight: 'all',
    scrollBeyondLastLine: false,
    tabSize: 2,
    readOnly: readOnly.value,
    automaticLayout: true,
    padding: { top: 6 },
  })
  model.onDidChangeContent(() => {
    content.value = model!.getValue()
    dirty.value = content.value !== originalContent.value
  })
  editor.onDidChangeCursorPosition((e) => {
    cursorPos.value = { line: e.position.lineNumber, col: e.position.column }
  })
  editor.addAction({
    id: 'sftp-editor-save',
    label: '保存',
    keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
    run: () => {
      save()
    },
  })
  // Optimization 修复：保存 timerId，组件卸载时清理，避免 dispose 后仍回调
  _layoutTimers.push(window.setTimeout(() => editor?.layout(), 50))
  _layoutTimers.push(window.setTimeout(() => editor?.layout(), 200))
  editor.focus()
}

/** Monaco 布局延时的定时器引用（组件卸载时清理） */
const _layoutTimers: number[] = []

function disposeEditor(): void {
  // C29 修复：清理所有未触发的 layout 定时器（原实现只靠组件卸载时清理，
  // 关闭后重开/多次 dispose 会让旧定时器回调到已释放的编辑器）
  for (const t of _layoutTimers) clearTimeout(t)
  _layoutTimers.length = 0
  if (editor) {
    editor.dispose()
    editor = null
  }
  if (model) {
    model.dispose()
    model = null
  }
}

async function save(): Promise<void> {
  if (!dirty.value || readOnly.value || saving.value) return
  saving.value = true
  try {
    // B9 修复：浏览器（非 Electron）下显式守卫，不再用非空断言
    const api = window.electronAPI
    if (!api) {
      toast.error('文件编辑仅 Electron 环境可用')
      saving.value = false
      return
    }
    if (props.side === 'local') {
      await api.fsWriteFile(props.path, content.value)
    } else {
      await api.sftpWriteFile(props.sshSessionId, props.path, content.value)
    }
    originalContent.value = content.value
    dirty.value = false
    toast.success('已保存')
    emit('saved')
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function handleClose(): Promise<void> {
  if (dirty.value) {
    unsavedDialog.value.visible = true
    return
  }
  emit('close')
}

function dismissUnsavedDialog(): void {
  unsavedDialog.value.visible = false
}

function discardAndClose(): void {
  unsavedDialog.value.visible = false
  emit('close')
}

async function saveAndClose(): Promise<void> {
  await save()
  if (!dirty.value) {
    unsavedDialog.value.visible = false
    emit('close')
  }
}

onUnmounted(() => {
  // Optimization 修复：清理未触发的布局定时器
  _layoutTimers.forEach(t => clearTimeout(t))
  disposeEditor()
})
</script>

<style scoped>
.editor-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
}

.editor-dialog {
  width: 80vw;
  height: 85vh;
  max-width: 1400px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: var(--shadow-lg);
}

/* 头部 */
.editor-header {
  height: 42px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}
.file-icon {
  width: 15px;
  height: 15px;
  color: var(--accent-cyan);
  flex-shrink: 0;
}
.file-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  flex-shrink: 0;
}
.file-path {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.dirty-dot {
  color: var(--accent-yellow, #e2c08d);
  font-size: 10px;
  flex-shrink: 0;
}
.readonly-badge {
  padding: 1px 6px;
  font-size: 10px;
  border-radius: 8px;
  background: rgba(226, 192, 141, 0.12);
  color: var(--accent-yellow, #e2c08d);
  flex-shrink: 0;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.save-btn {
  height: 28px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  gap: 5px;
  background: var(--accent-blue);
  border: 1px solid var(--accent-blue);
  color: #fff;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font-sans);
  transition: opacity 0.1s;
}
.save-btn:hover:not(:disabled) {
  opacity: 0.9;
}
.save-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.save-btn svg {
  width: 13px;
  height: 13px;
}
.close-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  font-size: 20px;
  cursor: pointer;
  border-radius: 4px;
  line-height: 1;
}
.close-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* 加载/错误态 */
.editor-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--text-tertiary);
  font-size: 13px;
}
.editor-state.error {
  color: var(--accent-red);
}
.editor-state .spinner {
  width: 24px;
  height: 24px;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.error-msg {
  max-width: 500px;
  text-align: center;
  line-height: 1.6;
}
.error-close {
  margin-top: 8px;
  height: 30px;
  padding: 0 16px;
  background: transparent;
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font-sans);
}
.error-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* 编辑器主体 */
.editor-body {
  flex: 1;
  min-height: 0;
  position: relative;
  background: var(--bg-base);
}
.monaco-container {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
}

/* 状态栏 */
.editor-footer {
  height: 26px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  background: var(--bg-elevated);
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  flex-shrink: 0;
}
.footer-item {
  white-space: nowrap;
}
.footer-sep {
  color: var(--text-tertiary);
  opacity: 0.4;
}
.readonly-hint {
  color: var(--accent-yellow, #e2c08d);
  margin-left: auto;
}
.remote-tag {
  color: var(--accent-green);
  margin-left: auto;
}

/* 未保存关闭确认弹窗 */
.unsaved-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1200;
}
.unsaved-dialog {
  width: 360px;
  max-width: 90vw;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.unsaved-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border-subtle);
}
.unsaved-header svg {
  color: var(--accent-yellow, #e2c08d);
  flex-shrink: 0;
}
.unsaved-body {
  padding: 16px;
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.unsaved-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--border-subtle);
}
.unsaved-btn {
  height: 30px;
  padding: 0 14px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
  font-family: var(--font-sans);
  border: 1px solid var(--border-default);
}
.unsaved-btn.neutral {
  background: transparent;
  color: var(--text-secondary);
}
.unsaved-btn.neutral:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.unsaved-btn.discard {
  background: transparent;
  color: var(--accent-red, #f48771);
  border-color: rgba(244, 135, 113, 0.4);
}
.unsaved-btn.discard:hover {
  background: rgba(244, 135, 113, 0.12);
}
.unsaved-btn.save {
  background: var(--accent-blue);
  color: #fff;
  border-color: var(--accent-blue);
}
.unsaved-btn.save:hover:not(:disabled) {
  opacity: 0.9;
}
.unsaved-btn.save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
