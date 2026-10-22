<template>
  <!-- TLS 接入网关面板：原先是一枚独立「安全接入」页（自带页头/通道页签/状态条），
       现并入远程管理页作为「TLS 网关」分段。通道页签随断层摘除 —— via='gateway'
       的隧道规则 UI 还造不出来，mux 通道表没有真实数据源；等 TunnelModal 支持
       网关后再以卡片展开区的形式回来。 -->
  <div class="access-view">
    <svg class="wm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/>
    </svg>

    <div class="card-grid" :class="{ 'is-empty': !filteredGateways.length }">
      <div v-for="gw in filteredGateways" :key="gw.id" class="conn-card">
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

      <div v-if="!filteredGateways.length" class="empty">
        <!-- 三种空不是一回事：搜索没命中、库里确实空的、IPC 没读到 —— 各说各的 -->
        <div class="t">{{ emptyText }}</div>
      </div>
    </div>

    <AccessModal v-if="modalOpen" :rec="editing" @close="modalOpen = false" @saved="onSaved" />

    <!-- TOFU 指纹确认。注意与 SSH 那侧**不是**同一套交互：SSH 首见指纹是自动信任 + 一条
         toast（stores/ssh.ts 的 onHostKeyNew），网关这侧是阻断式弹窗、点了才落盘。
         差异是有意的 —— 网关是持凭据的中继，放行一次的爆炸半径比一台目标机大。 -->
    <Teleport to="body">
    <div v-if="pending" class="overlay show">
      <div class="modal" role="dialog" aria-modal="true">
        <h2>确认网关身份</h2>
        <p class="lede">{{ pending.host }}:{{ pending.port }} · 本机第一次见到这张证书，确认后才建立会话。</p>
        <div class="spki">
          <span class="lbl">本次收到的服务端证书 SPKI / SHA-256</span>
          {{ pending.spki }}
        </div>
        <button class="btn btn-sm copy-spki" @click="copySpki">复制指纹</button>
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
import { computed, defineAsyncComponent, ref, onMounted, onUnmounted } from 'vue'
import { useAccessStore } from '@/stores/access'
import { useTunnelStore } from '@/stores/tunnel'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { toFriendlyMessage } from '@/api/client'

const AccessModal = defineAsyncComponent(() => import('./AccessModal.vue'))

const props = defineProps<{
  /** 远程管理工具栏的搜索词在此复用：按名称/主机/用户名过滤网关卡片 */
  search?: string
}>()

const accessStore = useAccessStore()
// 删网关要联动刷新引用它的隧道规则展示（TunnelView 的「网关未连接」档）
const tunnelStore = useTunnelStore()
const toast = useToastStore()
const { confirm } = useConfirm()

const modalOpen = ref(false)
const editing = ref<GatewayRecord | undefined>(undefined)
const busyId = ref('')

const pending = computed(() => accessStore.pendingHostKeys[0])

const filteredGateways = computed(() => {
  const q = (props.search || '').toLowerCase().trim()
  if (!q) return accessStore.gateways
  return accessStore.gateways.filter(g =>
    g.name.toLowerCase().includes(q) ||
    g.host.toLowerCase().includes(q) ||
    g.user.toLowerCase().includes(q))
})

const emptyText = computed(() => {
  if (!accessStore.loaded) return '没读到网关列表'
  if (!accessStore.gateways.length) return '还没有接入网关'
  return '没有匹配的网关'
})

function stateOf(id: string): GatewayStatus['state'] {
  return accessStore.stateOf(id)
}

/** 状态 → 徽章类名/文案：整张表按 GatewayStatus['state'] 键控，新增状态漏项即编译错 */
const ST_CLASS: Record<GatewayStatus['state'], string> = {
  idle: 'off',
  connecting: 'warn',
  online: 'on',
  reconnecting: 'warn',
  error: 'err',
}
const ST_LABEL: Record<GatewayStatus['state'], string> = {
  idle: '未连接',
  connecting: '连接中',
  online: '在线',
  reconnecting: '重连中',
  error: '失败',
}

/** 登录模式：口令是底座，配了 CA 才谈得上「证书」那一档 */
function loginModeOf(gw: GatewayRecord): string {
  return gw.caPem ? 'TLS + CA 校验' : 'TLS'
}

function stClass(id: string): string {
  return ST_CLASS[stateOf(id)]
}

function stLabel(id: string): string {
  const st = accessStore.statusOf(id)
  const state = st?.state ?? 'idle'
  // reconnecting 单独带 attempt；用函数表而不是把 switch 再抄一遍，
  // Record<GatewayState, …> 让「新增一个状态却忘了在这里挂」变成编译错。
  return state === 'reconnecting' ? `重连中 ${(st?.attempt ?? 0) || 1}` : ST_LABEL[state]
}

/**
 * 「重连中」必须给一个能停下来的动词。自动重连有上限（连续 3 次失败后 manager 停表
 * 转「失败」），但上限内的等待仍可能很长 —— 这档按钮让用户随时主动停（disconnect
 * 从任意状态都能停：清 timer + 拆会话），不必烧完 3 次预算。
 */
function actionLabel(id: string): string {
  const s = stateOf(id)
  if (s === 'online') return '断开'
  if (s === 'reconnecting') return '停止重连'
  return '连接'
}

function extra(id: string): string {
  const st = accessStore.statusOf(id)
  if (st?.state !== 'online') return st?.state === 'reconnecting' ? st.message || '' : ''
  // 心跳是网关在 HELLO_OK 里协商下来的，不能把部署上常见的默认值硬编进去 ——
  // 那会在其它部署上长期说谎。通道数为 0 是合法读数（在线但没人用），不能因为 falsy 就省掉。
  const bits = [`会话 ${st.sessionId || '-'}`, `角色 ${st.role || '-'}`, `心跳 ${st.heartbeatSec ?? '-'} s`, `${st.channels ?? 0} 通道`]
  return bits.join(' · ')
}

async function toggle(id: string): Promise<void> {
  busyId.value = id
  try {
    const s = stateOf(id)
    if (s === 'online' || s === 'reconnecting') await accessStore.disconnect(id)
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

/**
 * 保存后的动作只看「这次有没有提交口令」这个布尔，不看口令本身。
 * 明文出主进程后不该再经 store 抄回渲染层留一份（弹窗刚用完，页面并不需要你替它记着）。
 */
async function onSaved(saved: GatewayRecord, submittedPassword: boolean): Promise<void> {
  modalOpen.value = false
  // 填了口令就顺手连一次：HSC 的「确定」之后用户预期的是这张卡变在线
  if (submittedPassword) void accessStore.connect(saved.id).catch(err => {
    toast.error(toFriendlyMessage(err, '网关连接失败'))
  })
}

/** 删除网关：走全局确认框二次确认（与 SSH 连接删除同一条交互） */
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
    // 删除会影响「引用它的规则」那一档展示，刷新一次让隧道页跟上。
    // 原先是 `void refreshRules()`：tunnelList 走 IPC，失败即未处理拒绝（控制台里冒
    // Unhandled promise rejection，用户看不到任何提示）。它也不能并进上面的 catch —
    // 删除已经成功，把刷新失败报成「删除失败」是另一层谎。
    await tunnelStore.refreshRules().catch(() => {
      toast.error('隧道规则刷新失败，切到隧道页会重新加载')
    })
  } catch (err) {
    toast.error(toFriendlyMessage(err, '删除失败'))
  }
}

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

async function rejectKey(): Promise<void> {
  if (!pending.value) return
  const { id, spki } = pending.value
  try {
    await accessStore.dismissHostKey(id, spki)
  } catch (err) {
    toast.error(toFriendlyMessage(err, '取消确认失败'))
  }
}

/** 指纹要能被抄去和网关那边对一眼，否则这条弹窗只是「信 or 不信」二选一 */
function copySpki(): void {
  const spki = pending.value?.spki
  if (!spki) return
  navigator.clipboard.writeText(spki)
    .then(() => toast.success('已复制指纹'))
    .catch(() => toast.error('复制失败'))
}

// Escape 关闭 TOFU 指纹确认（等同取消）
function handleKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && pending.value) void rejectKey()
}

// 进入本面板先自证一次数据：app 挂载时那一次 init 若失败，这里说破，而不是让空态装成「还没有」
onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  if (accessStore.loaded) return
  void accessStore.refresh().then(ok => {
    if (!ok) toast.error('网关列表读取失败，请稍后重试')
  })
})
onUnmounted(() => window.removeEventListener('keydown', handleKeydown))

/** RemoteManager 头部的「新建网关」按钮经此拉起弹窗（新建入口只在头部画一处） */
defineExpose({ openCreate })
</script>

<style scoped>
/* 全量消费 variables.css 令牌，零硬编码色值。
   按钮一律走全局 styles/button.css（.btn / .btn-primary / .btn-danger / .btn-ghost / .btn-sm），
   本组件不另声明按钮样式。 */
/* 面板根：占满远程管理页列表区（父容器 .remote-manager 是 flex column），
   自身即滚动体 —— 原独立页的 .hsc-body 内滚随页头一起摘掉了 */
.access-view {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px;
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
  color: var(--accent-text);
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
  color: var(--accent-text);
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
.kv .k { color: var(--text-secondary); flex: none; text-align: left; }
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
.st.on { color: var(--accent-text); }
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

.empty {
  width: min(420px, 100%);
  padding: 44px 20px;
  text-align: center;
  color: var(--text-tertiary);
}
.empty .t { color: var(--text-secondary); font-size: 13px; }

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; z-index: 1000; }
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
.copy-spki { margin-top: 8px; }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
</style>
