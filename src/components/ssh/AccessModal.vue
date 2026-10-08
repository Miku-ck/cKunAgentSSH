<template>
  <Teleport to="body">
  <div class="overlay show" @click.self="tryClose">
    <div class="dialog" role="dialog" aria-modal="true">
      <div class="dlg-head">
        <h2>{{ isNew ? '添加连接' : '修改连接' }}</h2>
        <button class="dlg-x" :disabled="saving" @click="tryClose">✕</button>
      </div>
      <div class="dlg-tabs">
        <span class="dlg-tab on">TLS/SSL</span>
      </div>
      <div class="dlg-body">
        <div class="frow"><label>连接名称：</label><input v-model.trim="form.name" placeholder="留空则用地址"></div>
        <div class="frow">
          <label>服务器：</label>
          <input v-model.trim="form.host" class="mono" placeholder="sg-lab.example.net">
        </div>
        <div class="frow">
          <label>端口：</label>
          <input v-model.number="form.port" class="mono" inputmode="numeric">
        </div>
        <div v-if="bad.port" class="ferr">端口需为 1–65535 的整数</div>
        <div class="frow"><label>用户名：</label><input v-model.trim="form.user"></div>
        <div class="frow">
          <label>口令：</label>
          <input v-model="form.password" type="password" :placeholder="isNew ? '' : '留空 = 保留已存口令'">
        </div>

        <button class="adv-toggle" type="button" @click="advOpen = !advOpen">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" :class="{ rot: !advOpen }"><polyline points="6 9 12 15 18 9"/></svg>
          高级配置
        </button>
        <div v-show="advOpen">
          <div class="frow">
            <label>CA 证书：</label>
            <textarea v-model.trim="form.caPem" rows="3" class="mono" placeholder="粘贴 .crt（PEM）。留空则只按下面的指纹信任"></textarea>
          </div>
          <div class="frow">
            <label>SPKI 指纹：</label>
            <span class="mono val">{{ spkiDisplay || '尚未确认 —— 首次连接时弹窗确认' }}</span>
          </div>
        </div>
      </div>
      <div class="dlg-foot">
        <button class="btn btn-primary" :disabled="!canSave || saving" @click="save">确定</button>
        <button class="btn btn-ghost" :disabled="saving" @click="tryClose">取消</button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, reactive, ref, onMounted, onUnmounted } from 'vue'
import { useAccessStore } from '@/stores/access'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

const props = defineProps<{ rec?: GatewayRecord }>()
const emit = defineEmits<{ close: []; saved: [rec: GatewayRecord, submittedPassword: boolean] }>()

const accessStore = useAccessStore()
const toast = useToastStore()
const advOpen = ref(false)
const saving = ref(false)
const isNew = computed(() => !props.rec?.id)

/** 指纹由主进程的 TOFU 确认写入，表单只读展示；父组件关掉弹窗要等在途保存落地 */
const spkiDisplay = computed(() => props.rec?.trustedSpki || '')
function tryClose(): void {
  if (!saving.value) emit('close')
}

// Escape 关闭（在途保存时不关，见 tryClose）
function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') tryClose()
}
onMounted(() => window.addEventListener('keydown', handleKeydown))
onUnmounted(() => window.removeEventListener('keydown', handleKeydown))

const form = reactive({
  id: props.rec?.id || '',
  name: props.rec?.name || '',
  host: props.rec?.host || '',
  port: props.rec?.port || 4433,
  caPem: props.rec?.caPem || '',
  user: props.rec?.user || '',
  // 编辑时留空 = 保留原口令（store 侧同一口径），所以初值必须是空而不是解密后的口令
  password: '',
})

const bad = computed(() => ({
  port: !Number.isInteger(form.port) || form.port < 1 || form.port > 65535,
}))
const canSave = computed(() => !!form.host && !!form.user && !bad.value.port)

async function save(): Promise<void> {
  if (!canSave.value || saving.value) return
  const payload: Partial<GatewayRecord> & { password?: string } = {
    ...form,
    id: form.id,
    name: form.name || form.host,
  }
  if (!payload.password) delete payload.password
  if (!payload.caPem) delete payload.caPem
  const submittedPassword = !!form.password
  saving.value = true
  try {
    const saved = await accessStore.save(payload as GatewayRecord)
    emit('saved', saved, submittedPassword)
  } catch (err) {
    toast.error(toFriendlyMessage(err, '保存失败'))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
/* 排布复刻 designs/hsc-replica-mockup-v2.html 的「修改连接」：右对齐标签 + 左对齐控件。
   页脚按钮走全局 styles/button.css，本组件不声明按钮样式。
   z-index 跟同目录的 ConnectionModal 同层：Teleport 到 body 后它已不在页面容器里。 */
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.dialog {
  width: 500px;
  max-height: 88vh;
  overflow-y: auto;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}
.dlg-head { display: flex; align-items: center; padding: 14px 18px 0; }
.dlg-head h2 { font-size: 15px; font-weight: 600; }
.dlg-x { margin-left: auto; border: none; background: none; color: var(--text-secondary); font-size: 17px; cursor: pointer; line-height: 1; }
.dlg-x:hover { color: var(--text-primary); }
.dlg-tabs { display: flex; gap: 34px; padding: 0 18px; border-bottom: 1px solid var(--divider-primary); margin-top: 8px; }
.dlg-tab {
  padding: 10px 0 9px;
  font-size: 13.5px;
  color: var(--text-secondary);
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  margin-bottom: -1px;
  font-family: inherit;
}
.dlg-tab.on { color: var(--accent-primary); border-bottom-color: var(--accent-primary); font-weight: 600; }
.dlg-body { padding: 20px 18px 4px; }
.frow { display: grid; grid-template-columns: 104px 1fr; align-items: center; gap: 14px; margin-bottom: 16px; }
.frow > label { text-align: right; font-size: 13px; color: var(--text-secondary); }
.frow input, .frow textarea {
  width: 100%;
  min-height: 34px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  padding: 6px 10px;
  font-size: 13px;
  font-family: inherit;
  outline: none;
  resize: vertical;
}
.frow input:focus, .frow textarea:focus { border-color: var(--accent-primary); }
.mono { font-family: var(--font-mono); }
.frow .val { font-size: 12px; color: var(--text-secondary); word-break: break-all; }
.ferr { margin: -10px 0 14px 118px; font-size: 11px; color: var(--danger-text); }
.adv-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--accent-primary);
  cursor: pointer;
  border: none;
  background: none;
  font-family: inherit;
  padding: 0;
  margin-bottom: 16px;
}
.adv-toggle svg { width: 13px; height: 13px; transition: transform var(--transition-fast); }
.adv-toggle svg.rot { transform: rotate(-90deg); }
.dlg-foot { display: flex; justify-content: flex-end; gap: 10px; padding: 6px 18px 18px; }
</style>
