<template>
  <AccessModal v-if="modalOpen" :rec="editing" @close="modalOpen = false" @saved="onSaved" />

  <!-- TOFU 指纹确认。注意与 SSH 那侧**不是**同一套交互：SSH 首见指纹是自动信任 + 一条
       toast（stores/ssh.ts 的 onHostKeyNew），网关这侧是阻断式弹窗、点了才落盘。
       差异是有意的 —— 网关是持凭据的中继，放行一次的爆炸半径比一台目标机大。 -->
  <Teleport to="body">
    <div v-if="pending" class="overlay show">
      <div class="modal" role="dialog" aria-modal="true" aria-label="确认网关身份">
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
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import AccessModal from './AccessModal.vue'
import { useAccessStore } from '@/stores/access'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

const accessStore = useAccessStore()
const toast = useToastStore()

const modalOpen = ref(false)
const editing = ref<GatewayRecord | undefined>(undefined)
const pending = computed(() => accessStore.pendingHostKeys[0])

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
  // 填了口令就顺手连一次：HSC 的「确定」之后用户预期的是这条记录变在线
  if (submittedPassword) void accessStore.connect(saved.id).catch(err => {
    toast.error(toFriendlyMessage(err, '网关连接失败'))
  })
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

// Escape 关闭 TOFU 确认（等同取消）
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

defineExpose({ openEdit })
</script>

<style scoped>
/* 弹窗与 TOFU 确认；列表行由 GatewayRow 渲染，新建走 NewConnectionDialog，本组件只管编辑与首次信任。 */
.overlay { position: fixed; inset: 0; background: var(--scrim); backdrop-filter: blur(2px); display: none; align-items: center; justify-content: center; z-index: 1000; }
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
