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
          <div v-for="r in rows" :key="r.id" class="skill-row" :class="{ off: !r.on }">
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
          <div v-for="r in rows.filter(x => x.expanded)" :key="'c-' + r.id" class="skill-content">
            <pre>{{ r.system_prompt.slice(0, 4000) }}{{ r.system_prompt.length > 4000 ? `\n… [共 ${r.system_prompt.length} 字符]` : '' }}</pre>
          </div>
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
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

interface SkillRow {
  id: string
  name: string
  slug: string
  description: string
  category: string
  source: string
  system_prompt: string
  triggerWords: string[]
  on: boolean
  expanded: boolean
}

const MODE = 'ssh'
const toast = useToastStore()
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

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.listSkills(MODE)
    rows.value = data.map((s) => ({
      id: s.id,
      name: s.name,
      slug: s.slug,
      description: s.description ?? '',
      category: s.category ?? '',
      source: s.source,
      system_prompt: s.system_prompt ?? '',
      triggerWords: parseTriggerWords(s.trigger_words),
      on: s.mode_enabled === undefined ? s.enabled === 1 : s.mode_enabled,
      expanded: false,
    }))
  } catch (e) {
    error.value = toFriendlyMessage(e, '技能列表加载失败')
  } finally {
    loading.value = false
  }
}

async function onToggle(r: SkillRow, next: boolean) {
  const prev = r.on
  r.on = next
  try {
    await api.updateSkillEnabled(r.id, next, MODE)
  } catch (e) {
    r.on = prev
    toast.error(`技能「${r.name}」${next ? '启用' : '停用'}失败：${toFriendlyMessage(e)}`)
  }
}

async function onDelete(r: SkillRow) {
  try {
    await api.deleteSkill(r.id)
    rows.value = rows.value.filter((x) => x.id !== r.id)
    toast.success(`已删除技能「${r.name}」`)
  } catch (e) {
    toast.error(`删除失败：${toFriendlyMessage(e)}`)
  }
}

async function onSyncPackaged() {
  syncing.value = true
  try {
    const result = await api.syncPackagedSkills()
    await load()
    toast.success(`包内技能同步完成（${result.upserted} 条）`)
  } catch (e) {
    toast.error(`同步失败：${toFriendlyMessage(e)}`)
  } finally {
    syncing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.panel { height: 100%; display: flex; flex-direction: column; }
.panel-header { margin-bottom: 16px; flex-shrink: 0; }
.panel-header h3 { margin: 0 0 4px; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.panel-desc { margin: 0; font-size: 12px; color: var(--text-secondary); }
.panel-body { flex: 1; overflow-y: auto; padding-right: 4px; }

.grp {
  border: 1px solid var(--border-subtle, var(--border-default));
  border-radius: 6px; margin-bottom: 10px; background: var(--bg-input, transparent);
}
.grp-head {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 10px 12px; background: none; border: none; cursor: pointer;
  color: var(--text-primary); font-size: 13px; font-family: inherit; text-align: left;
}
.grp.static .grp-head { cursor: default; }
.grp-caret { transition: transform .15s; font-size: 11px; color: var(--text-tertiary); }
.grp-caret.open { transform: rotate(90deg); }
.grp-title { font-weight: 600; }
.grp-badge { margin-left: auto; font-size: 11px; color: var(--text-secondary); }
.grp.static .grp-badge { display: none; }
.grp-body { padding: 2px 12px 12px; }

.hint { margin: 0 0 8px; font-size: 11.5px; line-height: 1.5; color: var(--text-secondary); }
.hint.err { color: #d98f86; }
.hint.muted { color: var(--text-tertiary); }
.hint code { font-family: ui-monospace, Consolas, monospace; font-size: 11px; }
.link-btn {
  background: none; border: none; color: var(--accent-primary, #7aa2d0);
  cursor: pointer; font-size: 11.5px; font-family: inherit; padding: 0; text-decoration: underline;
}

.skill-row {
  display: flex; align-items: center; gap: 10px; padding: 7px 4px;
  border-top: 1px solid var(--border-subtle, rgba(255,255,255,.05));
}
.skill-row.off { opacity: .55; }
.tool-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.skill-name { font-size: 12.5px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.skill-desc { font-size: 11px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.skill-triggers { font-size: 10.5px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.skill-content {
  border: 1px dashed var(--border-default); border-radius: 4px; padding: 8px; margin: 2px 4px 8px;
  max-height: 240px; overflow-y: auto;
}
.skill-content pre { margin: 0; font-size: 11px; font-family: ui-monospace, Consolas, monospace; color: var(--text-secondary); white-space: pre-wrap; word-break: break-word; }

.flag {
  font-size: 10px; padding: 1px 6px; border-radius: 8px; white-space: nowrap;
  border: 1px solid var(--border-default); color: var(--text-tertiary);
}
.flag.ok { color: #7fbc92; border-color: rgba(127,188,146,.35); }
.flag.vio { color: #a98fd0; border-color: rgba(169,143,208,.35); }

.btn {
  flex-shrink: 0; height: 24px; padding: 0 10px; border-radius: 4px;
  background: none; border: 1px solid var(--border-default);
  color: var(--text-primary); font-size: 11.5px; font-family: inherit; cursor: pointer;
}
.btn.sm { height: 22px; padding: 0 8px; font-size: 11px; }
.btn.danger { color: #cf8a80; border-color: rgba(207,138,128,.4); }
.btn.danger:hover { background: rgba(207,138,128,.08); }
.btn:disabled { opacity: .5; cursor: default; }
</style>
