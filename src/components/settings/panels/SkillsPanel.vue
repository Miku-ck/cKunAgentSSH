<template>
  <div class="panel">
    <header class="panel-header">
      <h3>技能管理 <InfoTip text="Agent 的专项知识包：每轮运行按触发词匹配，命中的技能以目录形式暴露给模型，模型判断相关时再加载正文执行。关闭的技能不参与匹配；包内技能随应用更新，也可手动同步。" /></h3>
    </header>

    <div class="panel-body">
      <div class="list-body">
        <div class="list-bar">
          <span class="list-count">{{ loading && !rows.length ? '加载中…' : `已启用 ${enabledCount} / ${rows.length}` }}</span>
          <div class="list-acts">
            <button type="button" class="btn btn-sm btn-ghost" :disabled="loading" @click="load">刷新</button>
            <button type="button" class="btn btn-sm btn-ghost" :disabled="syncing" @click="onSyncPackaged">{{ syncing ? '同步中…' : '同步包内技能' }}</button>
          </div>
        </div>
        <p v-if="error" class="list-msg err">{{ error }}</p>
        <div v-if="rows.length" class="list">
          <template v-for="r in rows" :key="r.id">
            <div class="list-row" :class="{ off: !r.on }">
              <div class="row-main">
                <div class="row-title">
                  <span class="row-name">{{ r.name }}</span>
                  <span class="lbl" :class="sourceClass(r.source)">{{ sourceText[r.source] || r.source }}</span>
                </div>
                <span class="row-desc" :title="r.description">{{ r.description || '（无描述）' }}</span>
              </div>
              <div class="row-acts">
                <button type="button" class="btn btn-sm btn-ghost" :aria-expanded="r.expanded" @click="r.expanded = !r.expanded">{{ r.expanded ? '收起' : '详情' }}</button>
                <ToggleSwitch :model-value="r.on" :aria-label="`启用技能 ${r.name}`" @update:model-value="(v: boolean) => onToggle(r, v)" />
                <template v-if="r.source !== 'builtin'">
                  <span class="sep" />
                  <button type="button" class="btn btn-sm btn-danger" @click="onDelete(r)">删除</button>
                </template>
              </div>
            </div>
            <div v-if="r.expanded" class="row-detail">
              <p v-if="r.triggerWords.length" class="detail-line"><span class="lbl">触发词</span>{{ r.triggerWords.join('、') }}</p>
              <pre>{{ promptPreview(r) }}</pre>
            </div>
          </template>
        </div>
        <div v-else-if="!error" class="list-empty">{{ loading ? '正在加载…' : '还没有技能' }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * SkillsPanel — 设置内技能管理面板
 *
 * 数据源是后端 /skills（含包内 builtin、手工 manual、自演化 auto 技能），
 * 启用状态走 per-mode（?mode=ssh 写 skill_mode_config）。包内技能内容随 app
 * 分发，启动时由 backend/builtin_skills 同步；「同步包内技能」按钮供升级后手动刷新。
 */
import { computed, onMounted, ref } from 'vue'
import { api, toFriendlyMessage } from '@/api/client'
import InfoTip from '@/components/common/InfoTip.vue'
import { useToastStore } from '@/stores/toast'
import { useSkillStore } from '@/stores/skills'
import { useConfirm } from '@/composables/useConfirm'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

interface SkillRow {
  id: string
  name: string
  description: string
  source: string
  system_prompt: string
  triggerWords: string[]
  on: boolean
  expanded: boolean
}

const MODE = 'ssh'
const toast = useToastStore()
const skillStore = useSkillStore()
const { confirm } = useConfirm()
const rows = ref<SkillRow[]>([])
const loading = ref(false)
const syncing = ref(false)
const error = ref('')

const sourceText: Record<string, string> = { builtin: '内置', manual: '手工', auto: '自演化', upload: '导入' }
function sourceClass(source: string): string {
  if (source === 'builtin') return 'ok'
  if (source === 'auto') return 'vio'
  return ''
}

const enabledCount = computed(() => rows.value.filter((r) => r.on).length)

/** 详情区的正文预览：封顶 4000 字符，超出标明总长 */
function promptPreview(r: SkillRow): string {
  const body = r.system_prompt
  return body.length > 4000 ? `${body.slice(0, 4000)}\n… [共 ${body.length} 字符]` : body
}

function parseTriggerWords(raw: unknown): string[] {
  try {
    const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw
    return Array.isArray(parsed) ? parsed.filter((w): w is string => typeof w === 'string') : []
  } catch {
    return []
  }
}

/**
 * 写操作序号：一次 GET 的响应可能晚于期间的 PATCH/DELETE 才到（网络抖动、后端慢），
 * 那份快照记的是改动前的启用状态，照抄回行列表就是把用户刚拨的开关按回去，且不报错。
 * 序号变了就丢弃这份过期快照。
 */
let mutationSeq = 0
/** 仍在途的开关（技能 id）：响应没回来前，刷新按本地乐观值显示这一行 */
const pendingToggles = ref(new Set<string>())

async function load() {
  loading.value = true
  error.value = ''
  const seq = mutationSeq
  try {
    const data = await api.listSkills(MODE)
    if (seq !== mutationSeq) return
    rows.value = data.map((s) => {
      const prev = rows.value.find((r) => r.id === s.id)
      const serverOn = s.mode_enabled === undefined ? s.enabled === 1 : s.mode_enabled
      return {
        id: s.id,
        name: s.name,
        description: s.description ?? '',
        source: s.source,
        system_prompt: s.system_prompt ?? '',
        triggerWords: parseTriggerWords(s.trigger_words),
        on: prev && pendingToggles.value.has(s.id) ? prev.on : serverOn,
        expanded: false,
      }
    })
  } catch (e) {
    error.value = toFriendlyMessage(e, '技能列表加载失败')
  } finally {
    loading.value = false
  }
}

async function onToggle(r: SkillRow, next: boolean) {
  const prev = r.on
  r.on = next
  pendingToggles.value.add(r.id)
  try {
    await api.updateSkillEnabled(r.id, next, MODE)
    // 对话侧 "/" 菜单读的是 skillStore 缓存，不回灌就一直是改动前的列表
    void skillStore.loadRemoteSkills()
  } catch (e) {
    r.on = prev
    toast.error(`技能「${r.name}」${next ? '启用' : '停用'}失败：${toFriendlyMessage(e)}`)
  } finally {
    pendingToggles.value.delete(r.id)
    mutationSeq++
  }
}

async function onDelete(r: SkillRow) {
  const ok = await confirm({
    title: '删除技能',
    message: `删除技能「${r.name}」？库里只有这一份，删除不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await api.deleteSkill(r.id)
    rows.value = rows.value.filter((x) => x.id !== r.id)
    mutationSeq++
    void skillStore.loadRemoteSkills()
    toast.success(`已删除技能「${r.name}」`)
  } catch (e) {
    toast.error(`删除失败：${toFriendlyMessage(e)}`)
  }
}

async function onSyncPackaged() {
  syncing.value = true
  try {
    const result = await api.syncPackagedSkills()
    mutationSeq++
    await load()
    void skillStore.loadRemoteSkills()
    toast.success(`包内技能同步完成（${result.upserted} 条）`)
  } catch (e) {
    toast.error(`同步失败：${toFriendlyMessage(e)}`)
  } finally {
    syncing.value = false
  }
}

onMounted(load)
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped src="./settings-list.css"></style>
<style scoped>
.row-detail pre {
  margin: 0; max-height: 240px; overflow-y: auto;
  font-family: var(--font-mono); font-size: 0.8462rem; line-height: 1.5;
  color: var(--text-secondary); white-space: pre-wrap; word-break: break-word;
}
</style>
