<template>
  <Teleport to="body">
    <div class="qs-overlay" @click.self="close">
      <div ref="dialogRef" class="qs-dialog" role="dialog" aria-modal="true" aria-label="连接 SSH" tabindex="-1">
        <div class="qs-head">
          <h2>连接 SSH</h2>
          <button class="qs-x" aria-label="关闭" @click="close">✕</button>
        </div>

        <!-- 一行地址吃掉主机 / 端口 / 用户：粘贴 `ssh user@host -A` 就能连，
             不用先在四个字段里拆开再拼回去。 -->
        <div class="qs-addr">
          <input
            ref="addrRef"
            v-model="addr"
            type="text"
            spellcheck="false"
            autocomplete="off"
            placeholder="例如：ssh user@example.com -A"
            aria-label="SSH 地址"
            @keydown.enter.prevent="submit"
          >
          <button class="qs-go" :class="{ ready }" :disabled="!ready || !!busy" @click="submit">
            {{ busy ? '连接中…' : '连接' }}
          </button>
        </div>

        <div class="qs-hist-head">
          <span>SSH 主机历史</span>
          <button title="重新读取主机列表" aria-label="刷新主机列表" @click="refresh">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" width="13" height="13">
              <path d="M13 8a5 5 0 1 1-1.6-3.7M13 2.6V5.4h-2.8" />
            </svg>
          </button>
        </div>

        <div class="qs-hist">
          <div v-if="loading" class="qs-empty">正在读取…</div>
          <div v-else-if="!history.length" class="qs-empty">还没有主机记录</div>
          <template v-else>
            <div
              v-for="c in history"
              :key="c.id"
              class="qs-row"
              :title="`${c.username}@${c.host}:${c.port}`"
            >
              <svg class="qs-ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="14" height="14">
                <rect x="2" y="3" width="12" height="10" rx="1.5" /><path d="M5 6.5 7 8l-2 1.5M9 10.5h2" />
              </svg>
              <span class="qs-host">{{ c.host }}</span>
              <button class="qs-row-go" :disabled="!!busy" @click="connectTo(c)">
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" width="11" height="11">
                  <path d="M6 3H4a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h2M9 5l3 3-3 3M12 8H6" />
                </svg>
                连接
              </button>
            </div>
          </template>
        </div>

        <div class="qs-foot">
          <button class="qs-config" @click="openConfig">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="12" height="12">
              <circle cx="8" cy="8" r="2.1" />
              <path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M12.4 3.6l-1.2 1.2M4.8 11.2l-1.2 1.2" />
            </svg>
            打开 SSH 配置文件
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useSSHStore, type SSHConnection } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useModalFocusTrap } from '@/composables/useModalFocusTrap'

const emit = defineEmits<{ close: [] }>()

const ssh = useSSHStore()
const toast = useToastStore()
const addr = ref('')
const busy = ref(false)
const loading = ref(false)

const dialogRef = ref<HTMLElement | null>(null)
const addrRef = ref<HTMLInputElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)

/**
 * 解析一行 SSH 地址。接受三种写法：
 *   `ssh user@example.com -A`   ssh 命令原文（带不带 -A / -p 都行）
 *   `user@host:2222`            短写
 *   `host`                      只有主机，用户回退 root
 * 端口默认 22。解析不出主机就返回 null —— 连接按钮据此保持禁用，不猜。
 */
function parseAddr(raw: string): { user: string; host: string; port: number } | null {
  let s = raw.trim()
  if (!s) return null
  s = s.replace(/^ssh\s+/i, '')
  let port = 22
  const pFlag = s.match(/(?:^|\s)-p\s*(\d+)/)
  if (pFlag) port = Number(pFlag[1])
  const core = s.split(/\s+/).find(t => !t.startsWith('-')) || ''
  const shortPort = core.match(/^(.*):(\d+)$/)
  const body = shortPort ? shortPort[1] : core
  if (shortPort) port = Number(shortPort[2])
  if (!body) return null
  const at = body.lastIndexOf('@')
  const user = at >= 0 ? body.slice(0, at) : 'root'
  const host = at >= 0 ? body.slice(at + 1) : body
  if (!host) return null
  return { user, host, port }
}

const parsed = computed(() => parseAddr(addr.value))
const ready = computed(() => !!parsed.value)

/** 常连的在上面：lastConnectedAt 没有记录时按名称排，避免顺序随机跳动。 */
const history = computed(() =>
  [...ssh.connections].sort((a, b) =>
    (b.lastConnectedAt || 0) - (a.lastConnectedAt || 0) || a.host.localeCompare(b.host),
  ),
)

async function refresh(): Promise<void> {
  loading.value = true
  try {
    await ssh.loadConnections()
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

/** 同一 connectionId 已开会话就切过去，不再新堆一个标签（主机历史里连第二次是常态）。 */
async function connectTo(c: SSHConnection): Promise<void> {
  if (busy.value) return
  const existing = ssh.sessions.find(s => s.type === 'remote' && s.connectionId === c.id)
  if (existing) {
    ssh.switchSession(existing.id)
    emit('close')
    return
  }
  busy.value = true
  try {
    await ssh.connectRemote(c.id, 80, 24)
    emit('close')
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

/** 手输地址：命中已存连接就用它的凭据，否则按 ssh-agent 试连（密钥就绪时可直接进）。 */
async function submit(): Promise<void> {
  const p = parsed.value
  if (!p || busy.value) return
  const hit = ssh.connections.find(c => c.host === p.host && c.port === p.port && c.username === p.user)
  if (hit) {
    await connectTo(hit)
    return
  }
  const sameHost = ssh.connections.find(c => c.host === p.host && c.port === p.port)
  if (sameHost) {
    await connectTo(sameHost)
    return
  }
  busy.value = true
  try {
    const id = await ssh.addConnection({
      name: `${p.user}@${p.host}`,
      host: p.host,
      port: p.port,
      username: p.user,
      authType: 'agent',
      environment: 'test',
    })
    const saved = ssh.connections.find(c => c.id === id)
    if (saved) await connectTo(saved)
    else emit('close')
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

function openConfig(): void {
  void window.electronAPI?.sshOpenConfig()
}

function close(): void {
  emit('close')
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') close()
}

onMounted(() => {
  activate()
  window.addEventListener('keydown', onKeydown)
  addrRef.value?.focus()
  if (!ssh.connections.length) void refresh()
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  deactivate()
})
</script>

<style scoped>
.qs-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--scrim);
  backdrop-filter: blur(2px);
}

.qs-dialog {
  width: 420px;
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  outline: none;
}

.qs-head {
  display: flex;
  align-items: center;
  padding: 14px 16px 10px;
}

.qs-head h2 {
  margin: 0;
  font-size: 1.0769rem;
  font-weight: 600;
  color: var(--text-primary);
}

.qs-x {
  margin-left: auto;
  border: none;
  background: none;
  color: var(--text-secondary);
  font-size: 1rem;
  line-height: 1;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.qs-x:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.qs-addr {
  display: flex;
  gap: 6px;
  padding: 0 16px 4px;
}

.qs-addr input {
  flex: 1;
  min-width: 0;
  height: 30px;
  padding: 0 9px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-input);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 0.9231rem;
  outline: none;
}

.qs-addr input:focus {
  border-color: var(--accent-primary);
}

.qs-addr input::placeholder {
  color: var(--text-tertiary);
  font-family: inherit;
}

.qs-go {
  height: 30px;
  padding: 0 13px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-tertiary);
  font-size: 0.9231rem;
  cursor: not-allowed;
}

.qs-go.ready {
  border-color: var(--accent-primary);
  background: var(--accent-primary);
  color: var(--accent-on);
  cursor: pointer;
}

.qs-go.ready:hover:not(:disabled) {
  filter: brightness(1.08);
}

.qs-hist-head {
  display: flex;
  align-items: center;
  padding: 14px 16px 7px;
}

.qs-hist-head span {
  font-size: 0.9231rem;
  color: var(--text-secondary);
}

.qs-hist-head button {
  margin-left: auto;
  border: none;
  background: none;
  color: var(--text-tertiary);
  padding: 3px;
  line-height: 0;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.qs-hist-head button:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.qs-hist {
  margin: 0 16px;
  min-height: 96px;
  max-height: 210px;
  overflow-y: auto;
  border-radius: var(--radius-md);
}

.qs-row {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 34px;
  padding: 0 9px;
  background: var(--bg-input);
  border-bottom: 1px solid var(--bg-elevated);
}

.qs-row:first-child {
  border-top-left-radius: var(--radius-md);
  border-top-right-radius: var(--radius-md);
}

.qs-row:last-child {
  border-bottom: none;
  border-bottom-left-radius: var(--radius-md);
  border-bottom-right-radius: var(--radius-md);
}

.qs-ico {
  flex: none;
  color: var(--text-secondary);
}

.qs-host {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 0.9231rem;
  color: var(--text-primary);
}

.qs-row-go {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 9px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.8462rem;
  cursor: pointer;
}

.qs-row-go:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.qs-empty {
  padding: 26px 9px;
  text-align: center;
  font-size: 0.9231rem;
  color: var(--text-tertiary);
}

.qs-foot {
  display: flex;
  align-items: center;
  padding: 12px 16px 14px;
}

.qs-config {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: none;
  background: none;
  color: var(--text-tertiary);
  font-size: 0.9231rem;
  padding: 4px 6px;
  margin-left: -6px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.qs-config:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
}
</style>
