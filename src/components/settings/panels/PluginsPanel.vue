<template>
  <div class="panel">
    <header class="panel-header">
      <h3>插件管理 <InfoTip text="由 MCP server 或外部进程提供新工具，在本机执行，与远程 SSH 命令是两条独立边界，需单独授权。安装后先验证工具清单、确认启用才会启动；每次调用仍走审批与审计，插件工具不提供「始终允许」。" /></h3>
    </header>

    <div class="panel-body">
      <div class="list-body">
        <div class="list-bar">
          <span class="list-count">{{ store.loading && !store.plugins.length ? '加载中…' : `共 ${store.plugins.length} 个` }}</span>
          <div class="list-acts">
            <button type="button" class="btn btn-sm btn-ghost" :disabled="store.loading" @click="load">刷新</button>
            <button type="button" class="btn btn-sm btn-new" @click="openInstall">从本地安装</button>
          </div>
        </div>
        <p v-if="store.error" class="list-msg err">{{ store.error }}</p>
        <div v-if="store.plugins.length" class="list">
          <template v-for="p in store.plugins" :key="p.id">
            <div class="list-row" :class="{ off: !p.enabled }">
              <div class="row-main">
                <div class="row-title">
                  <span class="row-name">{{ p.name }}</span>
                  <span class="lbl" :class="statusOf(p).cls">{{ statusOf(p).text }}</span>
                </div>
                <span class="row-meta" :title="`${p.command} ${p.args_json}`">
                  <span class="mono">{{ p.command }}</span> {{ p.args_json }}
                  · r{{ p.config_rev }} · {{ store.toolsOf(p).length }} 个工具 · 超时 {{ p.timeout_s }}s
                </span>
              </div>
              <div class="row-acts">
                <button type="button" class="btn btn-sm btn-ghost" :disabled="busy" @click="onValidate(p)">验证工具</button>
                <button v-if="!p.consented_at" type="button" class="btn btn-sm btn-primary" :disabled="busy || !store.toolsOf(p).length" @click="onConsent(p)">确认启用</button>
                <template v-if="p.consented_at">
                  <button v-if="store.toolsOf(p).length" type="button" class="btn btn-sm btn-ghost" :aria-expanded="expanded.has(p.id)" @click="toggleTools(p.id)">{{ expanded.has(p.id) ? '收起' : '工具清单' }}</button>
                  <button type="button" class="btn btn-sm btn-ghost" @click="onToggle(p)">{{ p.enabled ? '停用' : '启用' }}</button>
                </template>
                <span class="sep" />
                <button type="button" class="btn btn-sm btn-danger" @click="onDelete(p)">删除</button>
              </div>
            </div>
            <div v-if="expanded.has(p.id)" class="row-detail">
              <p class="detail-line">
                勾选表示你确认该工具不改变任何状态；未勾选的工具每次调用都需审批，不可撤销、不自动重试。
                <InfoTip text="服务端声明的 readOnlyHint 仅供参考。" />
              </p>
              <div v-for="t in store.toolsOf(p)" :key="t.name" class="tool-line">
                <label>
                  <input
                    type="checkbox"
                    :checked="store.readonlyOf(p).includes(t.name)"
                    :disabled="busy"
                    @change="onReadonly(p, t.name, $event)"
                  />
                  <code>{{ t.name }}</code>
                  <span class="tool-desc">{{ t.description || '（无描述）' }}</span>
                </label>
                <span class="lbl" :class="store.readonlyOf(p).includes(t.name) ? 'ok' : ''">
                  {{ store.readonlyOf(p).includes(t.name) ? '已确认只读' : '需审批' }}
                </span>
              </div>
            </div>
          </template>
        </div>
        <div v-else-if="!store.error" class="list-empty">{{ store.loading ? '正在加载…' : '还没有安装插件，点右上角「从本地安装」开始' }}</div>
      </div>
    </div>

    <Teleport to="body">
      <Transition name="pl-fade">
        <div v-if="installOpen" class="pl-overlay" @click.self="closeInstall">
          <div ref="dialogRef" class="pl-dialog" role="dialog" aria-modal="true" aria-labelledby="pl-title" tabindex="-1">
            <header class="pl-head">
              <h3 id="pl-title">从本地安装插件</h3>
              <button type="button" class="btn btn-sm" aria-label="关闭" @click="closeInstall">×</button>
            </header>
            <div class="pl-body">
              <label class="pl-field"><span>名称</span><input v-model="form.name" type="text" maxlength="80" /></label>
              <label class="pl-field"><span>slug</span><input v-model="form.slug" type="text" maxlength="64" placeholder="k8s" /><small>工具命名空间 mcp__&lt;slug&gt;__&lt;tool&gt;，创建后不可修改</small></label>
              <label class="pl-field"><span>启动程序</span><input v-model="form.command" type="text" placeholder="python" /><small>只能填可执行程序名，参数写在下一项；后端不经 shell 启动</small></label>
              <label class="pl-field"><span>参数（每行一项）</span><textarea v-model="form.args" rows="2" placeholder="-m&#10;mcp_server_k8s"></textarea></label>
              <div class="pl-grid">
                <label class="pl-field"><span>工作目录</span><input v-model="form.cwd" type="text" placeholder="留空 = 后端工作目录" /></label>
                <label class="pl-field"><span>超时（秒）</span><input v-model.number="form.timeout_s" type="number" min="5" max="120" /></label>
              </div>
              <label class="pl-field"><span>环境变量（每行 KEY=VALUE）</span><textarea v-model="form.env" rows="2" placeholder="KUBECONFIG=C:/path/to/config"></textarea><small>仅注入显式声明项，后端凭据不会继承；Windows 走 DPAPI 加密，其他平台拒绝保存含 KEY/TOKEN/SECRET 的变量</small></label>
              <p v-if="formError" class="pl-err" role="alert">{{ formError }}</p>
            </div>
            <footer class="pl-foot">
              <button type="button" class="btn" @click="closeInstall">取消</button>
              <button type="button" class="btn btn-primary" :disabled="busy" @click="onInstall">{{ busy ? '保存中…' : '保存（保持停用）' }}</button>
            </footer>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
/** PluginsPanel — 插件安装、工具发现、同意流与只读确认。居中弹窗 + 共享焦点管理。 */
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import type { Plugin } from '@/api/core'
import InfoTip from '@/components/common/InfoTip.vue'
import { usePluginsStore } from '@/stores/plugins'
import { useConfirm } from '@/composables/useConfirm'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'

const store = usePluginsStore()
const { confirm } = useConfirm()
const installOpen = ref(false)
const busy = ref(false)
const formError = ref('')
const expanded = ref<Set<string>>(new Set())
const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef, { manageTab: false })
watch(installOpen, v => { if (v) activate(); else deactivate() })

/** 行内唯一的状态字：同意流走到哪一步，只说一步（不再「未启用 + 待确认 + r1」三枚胶囊并排） */
function statusOf(p: Plugin): { text: string; cls: string } {
  if (p.consented_at) return p.enabled ? { text: '已启用', cls: 'ok' } : { text: '已停用', cls: '' }
  return { text: store.toolsOf(p).length ? '待确认' : '待验证', cls: 'warn' }
}

const form = reactive({
  name: '', slug: '', command: '', args: '', cwd: '', env: '', timeout_s: 30,
})

function load() { return store.fetchPlugins() }
function toggleTools(id: string) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}
function onKey(e: KeyboardEvent) { if (e.key === 'Escape' && installOpen.value) closeInstall() }
onMounted(() => { void load(); window.addEventListener('keydown', onKey) })
onUnmounted(() => window.removeEventListener('keydown', onKey))

function openInstall() {
  formError.value = ''
  Object.assign(form, { name: '', slug: '', command: '', args: '', cwd: '', env: '', timeout_s: 30 })
  installOpen.value = true
}
function closeInstall() { installOpen.value = false }

function parseEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const s = line.trim()
    if (!s) continue
    const i = s.indexOf('=')
    if (i <= 0) throw new Error(`环境变量格式错误：${s}`)
    out[s.slice(0, i).trim()] = s.slice(i + 1).trim()
  }
  return out
}

async function run(action: () => Promise<unknown>) {
  busy.value = true
  store.error = ''
  try {
    await action()
  } catch (e) {
    store.error = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onInstall() {
  formError.value = ''
  if (!form.name.trim() || !form.slug.trim() || !form.command.trim()) {
    formError.value = '名称、slug、启动程序不能为空'
    return
  }
  let env: Record<string, string>
  try {
    env = parseEnv(form.env)
  } catch (e) {
    formError.value = e instanceof Error ? e.message : String(e)
    return
  }
  await run(async () => {
    await store.create({
      slug: form.slug.trim(), name: form.name.trim(), command: form.command.trim(),
      args: form.args.split('\n').map(s => s.trim()).filter(Boolean),
      cwd: form.cwd.trim() || null,
      env,
      timeout_s: Number(form.timeout_s) || 30,
    })
    closeInstall()
  })
}

async function onValidate(plugin: Plugin) {
  await run(() => store.validate(plugin.id))
}

async function onConsent(plugin: Plugin) {
  const ok = await confirm({
    title: '确认启用插件',
    message: `「${plugin.name}」将在本机启动 ${plugin.command}，并把 ${store.toolsOf(plugin).length} 个工具暴露给 Agent。确认后每次调用仍需审批。`,
    confirmText: '确认启用',
  })
  if (!ok) return
  await run(() => store.consent(plugin.id))
}

async function onReadonly(plugin: Plugin, name: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const current = store.readonlyOf(plugin)
  const next = checked ? [...new Set([...current, name])] : current.filter(n => n !== name)
  await run(() => store.setReadonly(plugin.id, next))
}

async function onToggle(plugin: Plugin) {
  if (plugin.enabled) {
    const ok = await confirm({
      title: '停用插件',
      message: `停用「${plugin.name}」会立刻关闭它的进程，后续 run 不再看到这些工具。`,
      confirmText: '停用',
      danger: true,
    })
    if (!ok) return
  }
  await run(() => store.setEnabled(plugin.id, !plugin.enabled))
}

async function onDelete(plugin: Plugin) {
  const ok = await confirm({
    title: '删除插件',
    message: `确定删除「${plugin.name}」？其进程会立即关闭，历史审计记录仍保留。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  await run(() => store.remove(plugin.id))
}
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped src="./settings-list.css"></style>
<style scoped>
.tool-line { display: flex; align-items: center; gap: var(--spacing-8); padding: var(--spacing-4) 0; font-size: 0.8462rem; color: var(--text-secondary); }
.tool-line label { flex: 1; min-width: 0; display: flex; align-items: center; gap: var(--spacing-8); cursor: pointer; }
.tool-line code { flex-shrink: 0; font-family: var(--font-mono); color: var(--text-primary); }
.tool-desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-tertiary); }

.pl-overlay { position: fixed; inset: 0; z-index: 2000; display: flex; align-items: center; justify-content: center; background: var(--scrim); backdrop-filter: blur(2px); }
.pl-dialog { display: flex; flex-direction: column; width: min(560px, 100vw - 48px); max-height: min(640px, 100vh - 96px); background: var(--bg-elevated); border: 1px solid var(--border-default); border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); }
.pl-head { display: flex; align-items: center; justify-content: space-between; padding: var(--spacing-12) var(--spacing-16); border-bottom: 1px solid var(--border-subtle); }
.pl-head h3 { font-size: 1rem; font-weight: 600; color: var(--text-primary); }
.pl-body { flex: 1; overflow: auto; padding: var(--spacing-16); display: flex; flex-direction: column; gap: var(--spacing-12); }
.pl-field { display: flex; flex-direction: column; gap: var(--spacing-4); }
.pl-field > span { color: var(--text-secondary); }
.pl-field small { color: var(--text-tertiary); }
.pl-err { margin: 0; font-size: 0.8846rem; line-height: 1.5; color: var(--status-error-text); }
.pl-field input, .pl-field textarea { width: 100%; padding: var(--spacing-6) var(--spacing-8); color: var(--text-primary); background: var(--bg-input); border: 1px solid var(--border-input); border-radius: var(--radius-sm); font: inherit; }
.pl-grid { display: grid; grid-template-columns: 2fr 1fr; gap: var(--spacing-12); }
.pl-foot { display: flex; justify-content: flex-end; gap: var(--spacing-8); padding: var(--spacing-12) var(--spacing-16); border-top: 1px solid var(--border-subtle); }
.pl-fade-enter-active, .pl-fade-leave-active { transition: opacity 150ms; }
.pl-fade-enter-from, .pl-fade-leave-to { opacity: 0; }
</style>
