<template>
  <Teleport to="body">
    <div class="nc-overlay">
      <div class="nc-dialog" role="dialog" aria-modal="true" aria-label="新建连接">
        <div class="nc-head">
          <h2>新建连接</h2>
          <button class="nc-x" aria-label="关闭" @click="requestClose">✕</button>
        </div>
        <div class="nc-tabs" role="tablist" aria-label="连接类型">
          <button
            type="button"
            role="tab"
            class="nc-tab"
            :class="{ on: tab === 'ssh' }"
            :aria-selected="tab === 'ssh'"
            @click="tab = 'ssh'"
          >SSH 连接</button>
          <button
            type="button"
            role="tab"
            class="nc-tab"
            :class="{ on: tab === 'tls' }"
            :aria-selected="tab === 'tls'"
            @click="tab = 'tls'"
          >TLS 网关</button>
        </div>

        <!-- 两套表单常驻、只切显隐：来回换标签不丢已填内容 -->
        <div class="nc-body">
          <div v-show="tab === 'ssh'" class="nc-pane">
            <ConnectionModal ref="sshRef" embedded @close="close" @saved="onSshSaved" />
          </div>
          <div v-show="tab === 'tls'" class="nc-pane">
            <AccessModal ref="tlsRef" embedded @close="close" @saved="onTlsSaved" />
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import ConnectionModal from './ConnectionModal.vue'
import AccessModal from './AccessModal.vue'

const emit = defineEmits<{
  close: []
  'saved-ssh': [payload: { savedId: string; connectNow: boolean }]
  'saved-tls': [payload: { rec: GatewayRecord; submittedPassword: boolean }]
}>()

type FormHandle = { requestClose(): void }

const tab = ref<'ssh' | 'tls'>('ssh')
const sshRef = ref<FormHandle | null>(null)
const tlsRef = ref<FormHandle | null>(null)

/** 关闭前先问当前表单：保存/测试在途时它自己会拒绝 */
function requestClose(): void {
  ;(tab.value === 'ssh' ? sshRef.value : tlsRef.value)?.requestClose()
}

function close(): void {
  emit('close')
}

function onSshSaved(payload: { savedId: string; connectNow: boolean }): void {
  emit('saved-ssh', payload)
  close()
}

function onTlsSaved(rec: GatewayRecord, submittedPassword: boolean): void {
  emit('saved-tls', { rec, submittedPassword })
  close()
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') requestClose()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.nc-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(2px);
}

.nc-dialog {
  width: 560px;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}

.nc-head {
  display: flex;
  align-items: center;
  padding: 16px 18px 0;
  flex-shrink: 0;
}

.nc-head h2 {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.nc-x {
  margin-left: auto;
  border: none;
  background: none;
  color: var(--text-secondary);
  font-size: 17px;
  line-height: 1;
  cursor: pointer;
}

.nc-x:hover {
  color: var(--text-primary);
}

.nc-tabs {
  display: flex;
  gap: 34px;
  margin-top: 12px;
  padding: 0 18px;
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
}

.nc-tab {
  padding: 10px 0 9px;
  margin-bottom: -1px;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--text-secondary);
  font-family: inherit;
  font-size: 13.5px;
  cursor: pointer;
}

.nc-tab.on {
  color: var(--accent-text);
  border-bottom-color: var(--accent-primary);
  font-weight: 600;
}

.nc-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.nc-pane {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
</style>
