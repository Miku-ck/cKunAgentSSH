<template>
  <div class="panel">
    <header class="panel-header">
      <h3>技能管理</h3>
      <p class="panel-desc">Agent 的专项知识包 —— 内置运维技能开箱即用，触发词命中的技能会注入执行上下文</p>
    </header>

    <div class="panel-body">
      <section class="grp">
        <button class="grp-head" @click="open.list = !open.list">
          <span class="grp-caret" :class="{ open: open.list }">▸</span>
          <span class="grp-title">技能库</span>
          <span class="grp-badge">{{ enabledCount }}/{{ rows.length }} 启用</span>
        </button>
        <div v-show="open.list" class="grp-body">
          <p class="hint">
            按「运维模式」隔离启用状态；关闭的技能不再参与触发词匹配与目录暴露。
            <button class="link-btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
            <button class="link-btn" :disabled="syncing" @click="onSyncPackaged">{{ syncing ? '同步中…' : '同步包内技能' }}</button>
          </p>
          <p v-if="error" class="hint err">{{ error }}</p>
          <p v-else-if="!rows.length" class="hint muted">暂无技能（包内运维技能启动时自动同步；若为空可点上方「同步包内技能」）</p>
          <template v-for="r in rows" :key="r.id">
            <div class="skill-row" :class="{ off: !r.on }">
              <div class="tool-info">
                <span class="skill-name">
                  {{ r.name }}
                  <span class="flag" :class="sourceClass(r.source)">{{ sourceText[r.source] || r.source }}</span>
                </span>
                <span class="skill-desc" :title="r.description">{{ r.description || '（无描述）' }}</span>
                <span v-if="r.triggerWords.length" class="skill-triggers">
                  触发词：{{ r.triggerWords.slice(0, 6).join('、') }}{{ r.triggerWords.length > 6 ? ' …' : '' }}
                </span>
              </div>
              <button class="btn sm" @click="r.expanded = !r.expanded">{{ r.expanded ? '收起' : '正文' }}</button>
              <ToggleSwitch :model-value="r.on" @update:model-value="(v: boolean) => onToggle(r, v)" />
              <button v-if="r.source !== 'builtin'" class="btn danger sm" @click="onDelete(r)">删除</button>
            </div>
            <div v-if="r.expanded" class="skill-content">
              <pre>{{ r.system_prompt.slice(0, 4000) }}{{ r.system_prompt.length > 4000 ? `\n… [共 ${r.system_prompt.length} 字符]` : '' }}</pre>
            </div>
          </template>
        </div>
      </section>

      <section class="grp static">
        <div class="grp-head">
          <span class="grp-title">技能如何生效</span>
        </div>
        <div class="grp-body">
          <p class="hint">
            每轮运行时后端按触发词智能匹配（词边界 + 短语加权），命中的技能以目录形式暴露给模型，
            模型判断相关时调 <code>load_skill</code> 拉取正文（最多 12000 字符）在后续轮次遵循执行。
            对话里的技能选择器可显式挂载技能，优先级高于自动匹配。
          </p>
        </div>
      </section>
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
import { computed, onMounted, reactive, ref } from 'vue'
import { api, toFriendlyMessage } from '@/api/client'
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
const open = reactive({ list: true })
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
<style scoped src="./settings-groups.css"></style>
<style scoped>
.skill-row {
  display: flex; align-items: center; gap: 10px; padding: 7px 4px;
  border-top: 1px solid var(--border-subtle);
}
.skill-row.off { opacity: .55; }
.skill-name { font-size: 12.5px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.skill-desc { font-size: 11px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.skill-triggers { font-size: 10.5px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.skill-content {
  border: 1px dashed var(--border-default); border-radius: 4px; padding: 8px; margin: 2px 4px 8px;
  max-height: 240px; overflow-y: auto;
}
.skill-content pre { margin: 0; font-size: 11px; font-family: var(--font-mono); color: var(--text-secondary); white-space: pre-wrap; word-break: break-word; }
</style>
