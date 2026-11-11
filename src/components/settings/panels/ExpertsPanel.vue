<template>
  <div class="panel">
    <header class="panel-header">
      <h3>专家管理</h3>
      <p class="panel-desc">运行策略配置包 —— 收窄模型、工具、技能、权限、迭代与记忆；不新增可执行能力</p>
    </header>

    <div class="panel-body">
      <section class="grp">
        <button class="grp-head" @click="openList = !openList">
          <span class="grp-caret" :class="{ open: openList }">▸</span>
          <span class="grp-title">专家库</span>
          <span class="grp-badge">{{ store.experts.length }} 个</span>
        </button>
        <div v-show="openList" class="grp-body">
          <p class="hint">
            内置「通用运维」全继承，选中它等同于不选专家。自定义专家只能收紧，不能放宽安全边界。
            <button class="link-btn" :disabled="store.loading" @click="load">{{ store.loading ? '加载中…' : '刷新' }}</button>
            <button class="link-btn" @click="openEditor(null)">新建专家</button>
          </p>
          <p v-if="store.error" class="hint err">{{ store.error }}</p>
          <div
            v-for="e in store.experts"
            :key="e.id"
            class="tool-info expert-row"
            :class="{ off: !e.enabled }"
          >
            <span class="expert-name">
              {{ e.name }}
              <span class="flag" :class="e.is_builtin ? 'ok' : ''">{{ e.is_builtin ? '内置' : `r${e.revision}` }}</span>
              <span v-if="!e.enabled" class="flag vio">已停用</span>
            </span>
            <span class="expert-desc" :title="e.description || ''">{{ e.description || e.slug }}</span>
            <div class="expert-actions">
              <button class="btn btn-sm" :disabled="!!e.is_builtin" @click="openEditor(e)">编辑</button>
              <button class="btn btn-sm" @click="onDuplicate(e)">复制</button>
              <button class="btn btn-sm" :disabled="!!e.is_builtin" @click="onToggle(e)">{{ e.enabled ? '停用' : '启用' }}</button>
              <button class="btn btn-danger btn-sm" :disabled="!!e.is_builtin" @click="onDelete(e)">删除</button>
            </div>
          </div>
          <p v-if="!store.experts.length" class="hint muted">还没有专家，点「新建专家」创建</p>
        </div>
      </section>

      <section class="grp static">
        <div class="grp-head"><span class="grp-title">三层边界</span></div>
        <div class="grp-body">
          <p class="hint">
            技能 = 怎么做（过程知识）；专家 = 以什么策略跑（配置）；插件 = 能做什么（新工具，必须单独授权）。
            工具集与技能集留空表示继承现状，填 <code>[]</code> 表示显式全禁；两者语义不同。
          </p>
        </div>
      </section>
    </div>

    <Teleport to="body">
      <Transition name="ex-fade">
        <div v-if="editorOpen" class="ex-overlay" @click.self="closeEditor">
          <div ref="dialogRef" class="ex-dialog" role="dialog" aria-modal="true" aria-labelledby="ex-title" tabindex="-1">
            <header class="ex-head">
              <h3 id="ex-title">{{ editing ? '编辑专家' : '新建专家' }}</h3>
              <button type="button" class="btn btn-sm" aria-label="关闭" @click="closeEditor">×</button>
            </header>
            <div class="ex-body">
              <label class="ex-field">
                <span>名称</span>
                <input v-model="form.name" type="text" maxlength="80" />
              </label>
              <label v-if="!editing" class="ex-field">
                <span>slug</span>
                <input v-model="form.slug" type="text" maxlength="64" placeholder="db-ops" />
                <small>小写字母 / 数字 / - / _，创建后不可修改</small>
              </label>
              <label class="ex-field">
                <span>描述</span>
                <input v-model="form.description" type="text" maxlength="500" />
              </label>
              <label class="ex-field">
                <span>身份设定</span>
                <textarea v-model="form.identity_prompt" rows="4" maxlength="8000" placeholder="留空表示不追加任何角色设定"></textarea>
              </label>
              <label class="ex-field">
                <span>模型 id</span>
                <input v-model="form.model_id" type="text" placeholder="留空 = 沿用默认模型" />
                <small>填写后本次运行锁定该模型，首页模型选择器不再生效</small>
              </label>
              <div class="ex-grid">
                <label class="ex-field">
                  <span>权限上限</span>
                  <select v-model="form.permission_ceiling">
                    <option value="inherit">继承（不改变）</option>
                    <option value="normal">手动审批（压住 auto）</option>
                    <option value="plan">只读计划（最严格）</option>
                  </select>
                </label>
                <label class="ex-field">
                  <span>迭代上限</span>
                  <input v-model="form.max_iterations" type="number" min="1" max="500" placeholder="留空 = 继承" />
                </label>
                <label class="ex-field">
                  <span>记忆范围</span>
                  <select v-model="form.memory_scope">
                    <option value="inherit">继承</option>
                    <option value="global">仅全局</option>
                    <option value="host">全局 + 当前主机</option>
                    <option value="none">不读记忆</option>
                  </select>
                </label>
              </div>
              <label class="ex-field">
                <span>工具集（JSON 数组）</span>
                <input v-model="form.tool_names" type="text" placeholder='留空 = 继承，[] = 全部禁用，["file_read"] = 只允许这些' />
              </label>
              <label class="ex-field">
                <span>技能集（JSON 数组）</span>
                <input v-model="form.skill_ids" type="text" placeholder="留空 = 继承，[] = 不挂任何技能" />
              </label>
              <label class="ex-field">
                <span>插件集（JSON 数组）</span>
                <input v-model="form.plugin_ids" type="text" placeholder="[] = 不授予任何插件" />
              </label>
              <p v-if="formError" class="hint err">{{ formError }}</p>
            </div>
            <footer class="ex-foot">
              <button type="button" class="btn" @click="closeEditor">取消</button>
              <button type="button" class="btn btn-primary" :disabled="saving" @click="save">
                {{ saving ? '保存中…' : '保存' }}
              </button>
            </footer>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
/**
 * ExpertsPanel — 设置内专家管理面板。
 *
 * 编辑器用居中弹窗（DESIGN.md 禁抽屉）、Esc 关闭并在卸载时清理监听；
 * 删除走全局 ConfirmDialog。集合字段直接以 JSON 数组文本编辑：与后端
 * tool_names_json 等字段同构，避免 UI 侧再维护一份「继承 / 空集」转换表。
 */
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import type { Expert, ExpertPayload } from '@/api/core'
import { useExpertsStore } from '@/stores/experts'
import { useConfirm } from '@/composables/useConfirm'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'

const store = useExpertsStore()
const { confirm } = useConfirm()

const openList = ref(true)
const editorOpen = ref(false)
const editing = ref<Expert | null>(null)
const saving = ref(false)
const formError = ref('')

/** 设置弹窗自持 Tab 路由（DESIGN.md）：面板内嵌弹窗只管初始焦点与关闭后还原。 */
const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef, { manageTab: false })
watch(editorOpen, (v) => { if (v) activate(); else deactivate() })

const form = reactive({
  slug: '', name: '', description: '', identity_prompt: '', model_id: '',
  permission_ceiling: 'inherit' as ExpertPayload['permission_ceiling'],
  max_iterations: '',
  memory_scope: 'inherit' as ExpertPayload['memory_scope'],
  tool_names: '', skill_ids: '', plugin_ids: '[]',
})

function load() {
  return store.fetchExperts()
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && editorOpen.value) closeEditor()
}

onMounted(() => {
  void load()
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => window.removeEventListener('keydown', onKey))

function openEditor(expert: Expert | null) {
  editing.value = expert
  formError.value = ''
  form.slug = ''
  form.name = expert?.name ?? ''
  form.description = expert?.description ?? ''
  form.identity_prompt = expert?.identity_prompt ?? ''
  form.model_id = expert?.model_id ?? ''
  form.permission_ceiling = expert?.permission_ceiling ?? 'inherit'
  form.max_iterations = expert?.max_iterations ? String(expert.max_iterations) : ''
  form.memory_scope = expert?.memory_scope ?? 'inherit'
  form.tool_names = expert?.tool_names_json ?? ''
  form.skill_ids = expert?.skill_ids_json ?? ''
  form.plugin_ids = expert?.plugin_ids_json ?? '[]'
  editorOpen.value = true
}

function closeEditor() {
  editorOpen.value = false
}

/** 空串 = null（继承）；'[]' = 空集；其余必须是合法 JSON 数组。 */
function parseList(raw: string): string[] | null {
  const text = raw.trim()
  if (!text) return null
  const parsed: unknown = JSON.parse(text)
  if (!Array.isArray(parsed) || !parsed.every(x => typeof x === 'string')) {
    throw new Error('必须是字符串 JSON 数组')
  }
  return parsed as string[]
}

function buildPayload(): ExpertPayload {
  const toolNames = parseList(form.tool_names)
  const skillIds = parseList(form.skill_ids)
  const pluginIds = parseList(form.plugin_ids)
  return {
    name: form.name.trim(),
    description: form.description.trim() || null,
    identity_prompt: form.identity_prompt,
    // 空串不传该字段：后端 Partial 更新把 null 当作「不改」，避免误清空
    ...(form.model_id.trim() ? { model_id: form.model_id.trim() } : {}),
    permission_ceiling: form.permission_ceiling,
    max_iterations: form.max_iterations ? Number(form.max_iterations) : null,
    memory_scope: form.memory_scope,
    tool_names: toolNames,
    skill_ids: skillIds,
    plugin_ids: pluginIds ?? [],
  }
}

async function save() {
  formError.value = ''
  if (!form.name.trim()) {
    formError.value = '名称不能为空'
    return
  }
  if (!editing.value && !form.slug.trim()) {
    formError.value = 'slug 不能为空'
    return
  }
  let payload: ExpertPayload
  try {
    payload = buildPayload()
  } catch (e) {
    formError.value = `工具集 / 技能集 / 插件集格式错误：${e instanceof Error ? e.message : String(e)}`
    return
  }
  saving.value = true
  try {
    if (editing.value) await store.updateExpert(editing.value.id, payload)
    else await store.createExpert({ ...payload, slug: form.slug.trim() })
    closeEditor()
  } catch (e) {
    formError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function onDuplicate(expert: Expert) {
  const slug = `${expert.slug}-copy-${Date.now().toString(36).slice(-4)}`
  try {
    await store.duplicateExpert(expert.id, slug, `${expert.name} 副本`)
  } catch (e) {
    store.error = e instanceof Error ? e.message : String(e)
  }
}

async function onToggle(expert: Expert) {
  try {
    await store.setEnabled(expert.id, !expert.enabled)
  } catch (e) {
    store.error = e instanceof Error ? e.message : String(e)
  }
}

async function onDelete(expert: Expert) {
  const ok = await confirm({
    title: '删除专家',
    message: `确定删除「${expert.name}」？已发出的历史消息仍保留当时的专家快照。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await store.removeExpert(expert.id)
  } catch (e) {
    store.error = e instanceof Error ? e.message : String(e)
  }
}
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped src="./settings-groups.css"></style>
<style scoped>
.expert-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  padding: var(--spacing-8) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.expert-row.off { opacity: 0.6; }
.expert-name { font-weight: 600; color: var(--text-primary); }
.expert-desc {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}
.expert-actions { display: flex; gap: var(--spacing-4); }

.ex-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--scrim);
  backdrop-filter: blur(2px);
}
.ex-dialog {
  display: flex;
  flex-direction: column;
  width: min(560px, 100vw - 48px);
  max-height: min(640px, 100vh - 96px);
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}
.ex-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-12) var(--spacing-16);
  border-bottom: 1px solid var(--border-subtle);
}
.ex-head h3 { font-size: 1rem; font-weight: 600; color: var(--text-primary); }
.ex-body {
  flex: 1;
  overflow: auto;
  padding: var(--spacing-16);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-12);
}
.ex-field { display: flex; flex-direction: column; gap: var(--spacing-4); }
.ex-field > span { color: var(--text-secondary); }
.ex-field small { color: var(--text-tertiary); }
.ex-field input,
.ex-field textarea,
.ex-field select {
  width: 100%;
  padding: var(--spacing-6) var(--spacing-8);
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  font: inherit;
}
.ex-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--spacing-12); }
.ex-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-8);
  padding: var(--spacing-12) var(--spacing-16);
  border-top: 1px solid var(--border-subtle);
}
.ex-fade-enter-active, .ex-fade-leave-active { transition: opacity 150ms; }
.ex-fade-enter-from, .ex-fade-leave-to { opacity: 0; }
</style>
