<template>
  <div class="access-view">
    <!-- 标签条：复刻 HSC 的 active 下划线形态。它的 日志/设置 两页本版没有对应数据面，
         不放空标签（本仓已有先例：侧栏那枚永灰的「插件」行是被拆掉的，不是留着） -->
    <div class="hsc-tabs">
      <button class="hsc-tab" :class="{ on: tab === 'conn' }" @click="tab = 'conn'">连接</button>
      <button class="hsc-tab" :class="{ on: tab === 'chan' }" @click="openChanTab">通道</button>
      <span class="spacer"></span>
      <button class="btn btn-primary" @click="openCreate">＋ 添加连接</button>
    </div>

    <div class="hsc-body">
      <svg class="wm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/>
      </svg>

      <div v-if="tab === 'conn'" class="card-grid" :class="{ 'is-empty': !accessStore.gateways.length }">
        <div v-for="gw in accessStore.gateways" :key="gw.id" class="conn-card">
          <div class="conn-head">
            <span class="conn-ico">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/></svg>
            </span>
            <span class="conn-title" :title="gw.host">{{ gw.name }}</span>
          </div>
          <div class="kv"><span class="k">服务器：</span><span class="v mono">{{ gw.host }}</span></div>
          <div class="kv"><span class="k">端口：</span><span class="v mono">{{ gw.port }}</span></div>
          <div class="kv"><span class="k">用户名：</span><span class="v">{{ gw.user }}</span></div>
          <div class="kv"><span class="k">登录模式：</span><span class="v">{{ loginModeOf(gw) }}</span></div>
          <div class="conn-rule"></div>
          <div class="conn-foot">
            <span class="st" :class="stClass(gw.id)">{{ stLabel(gw.id) }}</span>
            <button
              class="btn btn-sm"
              :class="stateOf(gw.id) === 'online' ? 'btn-danger' : 'btn-primary'"
              :disabled="busyId === gw.id || stateOf(gw.id) === 'connecting' || stateOf(gw.id) === 'reconnecting'"
              @click="toggle(gw.id)"
            >{{ actionLabel(gw.id) }}</button>
            <button class="icon-btn" title="修改连接" @click="openEdit(gw)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4Z"/></svg>
            </button>
            <button class="icon-btn" title="删除" @click="askDelete(gw.id)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            </button>
          </div>
          <!-- 在线时的通道数与角色：这两项只有真会话能给出，写死就是假数据 -->
          <div v-if="extra(gw.id)" class="conn-meta">{{ extra(gw.id) }}</div>
          <div v-if="stateOf(gw.id) === 'error'" class="conn-err">{{ accessStore.statusOf(gw.id)?.message || '连接失败' }}</div>
        </div>

        <div v-if="!accessStore.gateways.length" class="empty">
          <div class="t">还没有接入网关</div>
        </div>
      </div>

      <div v-else class="chan-pane">
        <select v-if="onlineIds.length" v-model="chanGw" class="gw-pick">
          <option v-for="id in onlineIds" :key="id" :value="id">{{ nameOf(id) }}</option>
        </select>
        <table v-if="rows.length" class="chan">
          <thead><tr><th>chan</th><th>方向</th><th>目标</th><th>↑</th><th>↓</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="r in rows" :key="r.chan" :class="{ fin: r.state === 'peer-fin' }">
              <td>{{ r.chan }}</td>
              <td :class="r.fromPeer ? 'dir-g2c' : 'dir-c2g'">{{ r.fromPeer ? '网关→本机' : '本机→网关' }}</td>
              <td>{{ r.dstHost }}:{{ r.dstPort }}</td>
              <td>{{ fmt(r.bytesUp) }}</td>
              <td>{{ fmt(r.bytesDown) }}</td>
              <td>{{ r.state === 'peer-fin' ? '已 FIN' : '开放' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="pane-note">{{ onlineIds.length ? '当前没有活动通道。' : '没有在线的网关。' }}</p>
      </div>
    </div>

    <!-- 底部状态条：与 HSC 同口径（左状态、右「已连接至 …｜共 N 个连接」） -->
    <div class="statusbar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>
      <span>{{ accessStore.onlineCount ? '在线' : '未连接' }}</span>
      <span class="right">
        <span>{{ connectedText }}</span>
        <span class="sep">|</span>
        <span>共 {{ accessStore.gateways.length }} 个连接</span>
      </span>
    </div>

    <AccessModal v-if="modalOpen" :rec="editing" @close="modalOpen = false" @saved="onSaved" />

    <!-- TOFU 指纹确认：与 SSH 首次连接同一套交互，两个信任面必须长一样 -->
    <Teleport to="body">
    <div v-if="pending" class="overlay show">
      <div class="modal" role="dialog" aria-modal="true">
        <h2>确认网关身份</h2>
        <p class="lede">{{ pending.host }}:{{ pending.port }} · 本机第一次见到这张证书，或它与记录不符。</p>
        <div class="spki">
          <span class="lbl">本次收到的服务端证书 SPKI / SHA-256</span>
          {{ pending.spki }}
        </div>
        <div class="modal-foot">
          <button class="btn btn-ghost" @click="rejectKey">取消</button>
          <button class="btn btn-primary" @click="acceptKey">信任并连接</button>
        </div>
      </div>
    </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, ref, onMounted, onUnmounted, watch } from 'vue'
import { useAccessStore } from '@/stores/access'
import { useTunnelStore } from '@/stores/tunnel'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { toFriendlyMessage } from '@/api/client'

const AccessModal = defineAsyncComponent(() => import('./AccessModal.vue'))

const accessStore = useAccessStore()
const tunnelStore = useTunnelStore()
const toast = useToastStore()
const { confirm } = useConfirm()

const tab = ref<'conn' | 'chan'>('conn')
const modalOpen = ref(false)
const editing = ref<GatewayRecord | undefined>(undefined)
const busyId = ref('')
const chanGw = ref('')
const rows = ref<GatewayChannelRow[]>([])

const pending = computed(() => accessStore.pendingHostKeys[0])
const onlineIds = computed(() =>
  accessStore.gateways.filter(g => accessStore.stateOf(g.id) === 'online').map(g => g.id),
)
const connectedText = computed(() =>
  accessStore.onlineNames.length ? `已连接至 ${accessStore.onlineNames.join('、')}` : '未建立连接',
)

function nameOf(id: string): string {
  const g = accessStore.gateways.find(x => x.id === id)
  return g ? g.name || g.host : id
}

function stateOf(id: string): GatewayStatus['state'] {
  return accessStore.stateOf(id)
}

/** 登录模式：口令是底座，配了 CA 才谈得上「证书」那一档 */
function loginModeOf(gw: GatewayRecord): string {
  return gw.caPem ? 'TLS + CA 校验' : 'TLS'
}

function stClass(id: string): string {
  const s = stateOf(id)
  if (s === 'online') return 'on'
  if (s === 'error') return 'err'
  if (s === 'connecting' || s === 'reconnecting') return 'warn'
  return 'off'
}

function stLabel(id: string): string {
  const st = accessStore.statusOf(id)
  switch (st?.state) {
    case 'online': return '在线'
    case 'connecting': return '连接中'
    case 'reconnecting': return `重连中 ${st.attempt || 1}`
    case 'error': return '失败'
    default: return '未连接'
  }
}

function actionLabel(id: string): string {
  return stateOf(id) === 'online' ? '断开' : '连接'
}

function extra(id: string): string {
  const st = accessStore.statusOf(id)
  if (st?.state !== 'online') return st?.state === 'reconnecting' ? st.message || '' : ''
  const bits = [`会话 ${st.sessionId || '-'}`, `角色 ${st.role || '-'}`, `心跳 30 s`]
  if (st.channels) bits.push(`${st.channels} 通道`)
  return bits.join(' · ')
}

function fmt(n: number): string {
  if (!n) return '0'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

async function toggle(id: string): Promise<void> {
  busyId.value = id
  try {
    if (stateOf(id) === 'online') await accessStore.disconnect(id)
    else await accessStore.connect(id)
  } catch (err) {
    toast.error(toFriendlyMessage(err, '网关操作失败'))
  } finally {
    busyId.value = ''
  }
}

function openCreate(): void {
  editing.value = undefined
  modalOpen.value = true
}

function openEdit(gw: GatewayRecord): void {
  editing.value = { ...gw }
  modalOpen.value = true
}

async function onSaved(saved: GatewayRecord): Promise<void> {
  modalOpen.value = false
  // 填了口令就顺手连一次：HSC 的「确定」之后用户预期的是这张卡变在线
  if (saved.password !== undefined) void accessStore.connect(saved.id).catch(err => {
    toast.error(toFriendlyMessage(err, '网关连接失败'))
  })
}

/** 删除网关：走全局确认框二次确认（与隧道页同一条交互，不再用内联两段确认） */
async function askDelete(id: string): Promise<void> {
  const gw = accessStore.gateways.find(g => g.id === id)
  const ok = await confirm({
    title: '删除连接',
    message: `确定删除连接「${gw?.name ?? id}」？引用它的隧道规则将变为「网关未连接」。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await accessStore.remove(id)
    // 规则里引用它的条目会变成「网关未连接」，这里把列表拉一次让 UI 跟上
    void tunnelStore.refreshRules()
  } catch (err) {
    toast.error(toFriendlyMessage(err, '删除失败'))
  }
}

function openChanTab(): void {
  tab.value = 'chan'
  // 这里刚赋值时 watch 会自己取一次，别再显式取第二遍；只有下拉本来就有值（回到本页）
  // 才需要这条显式调用，否则 watch 不触发。
  if (!chanGw.value && onlineIds.value.length) chanGw.value = onlineIds.value[0]
  else void loadChannels()
}

async function loadChannels(): Promise<void> {
  if (!chanGw.value) { rows.value = []; return }
  rows.value = await accessStore.channels(chanGw.value)
}

/** 通道表原先只有 openChanTab() 取一次，切网关的下拉不重取 —— 那枚「刷新」是唯一的补救
 *  入口。按批注去掉按钮后，这条 watch 把重取挂回下拉本身（选谁 = 要看谁的通道）。 */
watch(chanGw, () => void loadChannels())

async function acceptKey(): Promise<void> {
  if (!pending.value) return
  const { id, spki } = pending.value
  try {
    await accessStore.trustHostKey(id, spki)
    await accessStore.connect(id)
  } catch (err) {
    toast.error(toFriendlyMessage(err, '信任后连接仍失败'))
  }
}

function rejectKey(): void {
  if (pending.value) accessStore.dismissHostKey(pending.value.id, pending.value.spki)
}

// Escape 关闭 TOFU 指纹确认（等同取消）
function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && pending.value) rejectKey()
}
onMounted(() => window.addEventListener('keydown', handleKeydown))
onUnmounted(() => window.removeEventListener('keydown', handleKeydown))
</script>

<style scoped>
/* 全量消费 variables.css 令牌，零硬编码色值（对齐 designs/hsc-replica-mockup-v2.html 的排布）。
   按钮一律走全局 styles/button.css（.btn / .btn-primary / .btn-danger / .btn-ghost / .btn-sm），
   本组件不另声明按钮样式，尺寸与字重因此与隧道页、连接弹窗完全同源。 */
.access-view {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.hsc-tabs {
  flex: none;
  display: flex;
  align-items: flex-end;
  gap: 26px;
  padding: 0 22px;
  border-bottom: 1px solid var(--divider-primary);
}
.hsc-tab {
  padding: 13px 0 10px;
  font-size: 14px;
  color: var(--text-secondary);
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  cursor: pointer;
  font-family: inherit;
  margin-bottom: -1px;
  transition: color var(--transition-fast);
}
.hsc-tab:hover { color: var(--text-primary); }
.hsc-tab.on {
  color: var(--accent-primary);
  border-bottom-color: var(--accent-primary);
  font-weight: 600;
}
.spacer { flex: 1; }
/* 标签条右端的主操作：按钮本体完全交给全局 .btn，这里只把它按到标签基线上 */
.hsc-tabs > .btn { margin-bottom: 9px; }

.hsc-body {
  flex: 1;
  overflow: auto;
  padding: 22px;
  position: relative;
  display: flex;
  flex-direction: column;
}
/* 水印：HSC 那是他人商标，同位同透明度换成本产品 brand 图形 */
.wm {
  position: absolute;
  right: 18px;
  bottom: 6px;
  width: 300px;
  height: 300px;
  color: var(--accent-primary);
  opacity: 0.07;
  pointer-events: none;
}
.card-grid { display: flex; flex-wrap: wrap; gap: 18px; align-content: flex-start; position: relative; flex: 1; }
.card-grid.is-empty { justify-content: center; align-content: center; }

.conn-card {
  width: 282px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  padding: 14px 16px 12px;
  transition: border-color var(--transition-fast);
}
.conn-card:hover { border-color: var(--border-default); }
.conn-head { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; }
.conn-ico {
  width: 32px;
  height: 32px;
  flex: none;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  background: color-mix(in srgb, var(--accent-primary) 13%, transparent);
  color: var(--accent-primary);
}
.conn-ico svg { width: 17px; height: 17px; }
.conn-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kv { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; font-size: 13px; line-height: 25px; }
.kv .k { color: var(--text-secondary); flex: none; }
.kv .v { color: var(--text-primary); text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: var(--font-mono); }
.conn-rule { height: 1px; background: var(--divider-primary); margin: 10px 0 9px; }
.conn-foot { display: flex; align-items: center; justify-content: flex-end; gap: 10px; }
.conn-meta { margin-top: 8px; font-size: 11.5px; color: var(--text-tertiary); text-align: right; }
.conn-err {
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--danger-text);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: var(--radius-sm);
  padding: 5px 8px;
}
.st { font-size: 13px; }
.st.on { color: var(--accent-primary); }
.st.off { color: var(--text-tertiary); }
.st.err { color: var(--danger-text); }
.st.warn { color: var(--status-warn-text); }

.icon-btn {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.icon-btn svg { width: 14px; height: 14px; }
.icon-btn:hover { background: var(--bg-hover); color: var(--text-primary); }

.chan-pane { position: relative; max-width: 900px; }
.pane-note { font-size: 12px; color: var(--text-tertiary); margin: 0 0 14px; }
.gw-pick {
  height: 28px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 12.5px;
  font-family: inherit;
  padding: 0 8px;
  margin-bottom: 12px;
}
table.chan { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; }
table.chan th {
  text-align: left;
  font-weight: 500;
  color: var(--text-tertiary);
  font-size: 11.5px;
  padding: 0 10px 7px 0;
  border-bottom: 1px solid var(--divider-primary);
}
table.chan td { padding: 6px 10px 6px 0; border-bottom: 1px solid var(--divider-primary); color: var(--text-primary); }
table.chan tr:last-child td { border-bottom: none; }
table.chan tr.fin td { color: var(--text-tertiary); }
.dir-c2g { color: var(--tint-l); }
.dir-g2c { color: var(--tint-r); }

.statusbar {
  flex: none;
  height: 30px;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 14px;
  border-top: 1px solid var(--border-subtle);
  font-size: 12px;
  color: var(--text-secondary);
}
.statusbar svg { width: 13px; height: 13px; }
.statusbar .right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.statusbar .sep { color: var(--border-default); }

.empty {
  width: min(420px, 100%);
  padding: 44px 20px;
  text-align: center;
  color: var(--text-tertiary);
}
.empty .t { color: var(--text-secondary); font-size: 13px; }

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; z-index: 60; }
.overlay.show { display: flex; }
.modal {
  width: 520px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  padding: 18px 20px 16px;
  box-shadow: var(--shadow-lg);
}
.modal h2 { font-size: 14px; font-weight: 600; margin-bottom: 6px; }
.modal .lede { font-size: 12px; color: var(--text-tertiary); margin-bottom: 14px; }
.spki {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  padding: 9px 10px;
  line-height: 1.6;
  word-break: break-all;
}
.spki .lbl { display: block; color: var(--text-tertiary); font-size: 10.5px; margin-bottom: 3px; }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
</style>
