<template>
  <BaseModal :title="persona ? '编辑人格' : '新增人格'" @close="emit('close')">
    <div class="persona-editor">
      <fieldset class="editor-section">
        <legend>身份</legend>
        <label class="field"><span>名称</span><input v-model.trim="form.name" maxlength="50" placeholder="例如：晚风" /></label>
        <div class="field">
          <span>头像</span>
          <AvatarPicker v-model="form.avatar" :seed="form.name" :label="form.name" />
        </div>
        <label class="field">
          <span>它怎么称呼你</span>
          <input v-model.trim="form.address_user" maxlength="20" placeholder="留空就叫「你」；填「小林」就会叫你的名字" />
        </label>
        <label class="field"><span>开场白</span><textarea v-model="form.greeting" rows="2" maxlength="2000" placeholder="新会话里它对你说的第一句话" /></label>
        <label class="field"><span>聊天模型</span><select v-model="form.model_id"><option :value="null">跟随全局默认模型</option><option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}{{ m.model_id ? `（${m.model_id}）` : '' }}</option></select></label>
      </fieldset>

      <fieldset class="editor-section">
        <legend>性格与背景</legend>
        <label class="field"><span>它是个什么样的人</span><textarea v-model="form.personality" rows="4" maxlength="5000" placeholder="话不多但接得住情绪；被夸会有点不好意思；讨厌被催…" /></label>
        <label class="field"><span>背景设定</span><textarea v-model="form.background" rows="4" maxlength="10000" placeholder="它住在哪、在做什么、你们是怎么认识的…" /></label>
        <p class="hint">这里是「它是谁」。下面那一区是「它怎么开口」——两件事分开调，改一处不会牵动另一处。</p>
      </fieldset>

      <fieldset class="editor-section">
        <legend>说话方式</legend>
        <p v-if="!options" class="hint">档位读取失败，其余设定仍可保存。</p>
        <div v-for="key in dnaKeys" :key="key" class="field">
          <span>{{ options?.[key].label || key }}</span>
          <div class="chips" role="group" :aria-label="options?.[key].label || key">
            <button
              v-for="item in options?.[key].options || []"
              :key="item.value"
              type="button"
              class="chip"
              :class="{ active: form.expression_dna[key] === item.value }"
              :aria-pressed="form.expression_dna[key] === item.value"
              @click="setDna(key, item.value)"
            >{{ item.label }}</button>
          </div>
        </div>
        <label class="field">
          <span>口头禅</span>
          <input v-model.trim="form.expression_dna.catchphrase" maxlength="40" placeholder="偶尔挂一句就好，例如：说来话长" />
        </label>
        <label class="field">
          <span>不想让它说</span>
          <input v-model="form.tabooText" maxlength="200" placeholder="逗号分隔，例如：绝绝子、家人们" />
        </label>
        <p class="hint">「作为一个AI」「希望对你有帮助」这类模型腔已内置屏蔽，不用重复填。</p>
      </fieldset>

      <fieldset class="editor-section">
        <legend>反应与底线</legend>
        <div v-for="key in styleKeys" :key="key" class="field">
          <span>{{ options?.[key].label || key }}</span>
          <div class="chips" role="group" :aria-label="options?.[key].label || key">
            <button
              v-for="item in options?.[key].options || []"
              :key="item.value"
              type="button"
              class="chip"
              :class="{ active: form[key] === item.value }"
              :aria-pressed="form[key] === item.value"
              @click="setStyle(key, item.value)"
            >{{ item.label }}</button>
          </div>
        </div>
        <label class="field"><span>回复长度</span>
          <select v-model="form.reply_style">
            <option value="concise">简洁（一两句话）</option>
            <option value="moderate">适中</option>
            <option value="long">长文（可展开）</option>
          </select>
        </label>
        <label class="field field-inline"><span>思考</span>
          <input type="checkbox" v-model="form.enable_thinking" />
          <em class="field-hint">需模型支持；模型不回思考则不显示思考块</em>
        </label>
        <p class="hint">底线部分（不编造记忆、不谄媚、不假装有身体、不执行命令）固定生效，不可编辑。</p>
      </fieldset>

      <fieldset class="editor-section">
        <legend>关系</legend>
        <div class="field">
          <span>{{ options?.relationship_speed.label || '升温速度' }}</span>
          <div class="chips" role="group" aria-label="关系升温速度">
            <button
              v-for="item in options?.relationship_speed.options || []"
              :key="item.value"
              type="button"
              class="chip"
              :class="{ active: form.relationship_speed === item.value }"
              :aria-pressed="form.relationship_speed === item.value"
              @click="form.relationship_speed = item.value as BotRelationshipSpeed"
            >{{ item.label }}</button>
          </div>
        </div>
        <p class="hint">关系从「陌生」开始，聊得越多越近；久不联系会回落一档，聊两句又回来。</p>
      </fieldset>

      <div v-if="error" class="form-error">{{ error }}</div>
    </div>

    <template #actions>
      <button class="btn" @click="emit('close')">取消</button>
      <button class="btn btn-primary" :disabled="saving || !form.name || !isImageAvatar(form.avatar)" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
    </template>
  </BaseModal>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import BaseModal from '@/components/common/BaseModal.vue'
import AvatarPicker from './AvatarPicker.vue'
import { isImageAvatar } from '@/utils/persona-avatar'
import { toFriendlyMessage } from '@/api/base'
import {
  botApi,
  type BotExpressionDna,
  type BotPersona,
  type BotPersonaOptions,
  type BotPersonaPayload,
  type BotReactionStyle,
  type BotRefusalStyle,
  type BotRelationshipSpeed,
} from '@/api/bot'

const props = defineProps<{
  persona: BotPersona | null
  models: { id: string; name: string; model_id?: string | null }[]
  options: BotPersonaOptions | null
}>()
const emit = defineEmits<{ close: []; saved: [] }>()

const DNA_DEFAULTS: BotExpressionDna = {
  sentence: 'mixed', particles: 'soft', punctuation: 'plain', emoji: 'low', catchphrase: '', taboo: [],
}

const dnaKeys = ['sentence', 'particles', 'punctuation', 'emoji'] as const
const styleKeys = ['reaction_style', 'refusal_style'] as const

const saving = ref(false)
const error = ref('')

const source = props.persona
const form = reactive({
  name: source?.name || '',
  avatar: source?.avatar || '',
  personality: source?.personality || '',
  background: source?.background || '',
  greeting: source?.greeting || '',
  model_id: source?.model_id ?? null,
  reply_style: source?.reply_style || 'moderate',
  enable_thinking: source?.enable_thinking === 1,
  // 旧数据可能缺新字段：整体兜底，避免 v-model 绑到 undefined 后控件变哑
  expression_dna: { ...DNA_DEFAULTS, ...(source?.expression_dna || {}) } as BotExpressionDna,
  reaction_style: (source?.reaction_style || 'warm') as BotReactionStyle,
  refusal_style: (source?.refusal_style || 'soft') as BotRefusalStyle,
  address_user: source?.address_user || '',
  relationship_speed: (source?.relationship_speed || 'normal') as BotRelationshipSpeed,
  // 禁用词用一行文本编辑，比 tag 编辑省事；保存时拆成数组
  tabooText: (source?.expression_dna?.taboo || []).join('、'),
})

function setDna(key: (typeof dnaKeys)[number], value: string) {
  // 档位取值由服务端字典约束，这里断言只为满足联合类型，不会越界
  form.expression_dna[key] = value as never
}

function setStyle(key: (typeof styleKeys)[number], value: string) {
  form[key] = value as never
}

function buildPayload(): BotPersonaPayload {
  const taboo = form.tabooText
    .split(/[,，、;；\s]+/)
    .map(word => word.trim())
    .filter(Boolean)
    .slice(0, 12)
  return {
    name: form.name,
    // 头像只收上传图片：旧数据里的表情/文本在保存时被清理，不再回写
    avatar: isImageAvatar(form.avatar) ? form.avatar : '',
    personality: form.personality,
    background: form.background,
    greeting: form.greeting,
    model_id: form.model_id,
    reply_style: form.reply_style as BotPersonaPayload['reply_style'],
    enable_thinking: form.enable_thinking,
    expression_dna: { ...form.expression_dna, taboo },
    reaction_style: form.reaction_style,
    refusal_style: form.refusal_style,
    address_user: form.address_user,
    relationship_speed: form.relationship_speed,
  }
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const payload = buildPayload()
    if (props.persona) await botApi.updatePersona(props.persona.id, payload)
    else await botApi.createPersona(payload)
    emit('saved')
  } catch (e) {
    error.value = toFriendlyMessage(e, '人格保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.persona-editor { display: flex; flex-direction: column; gap: var(--spacing-16); }
.editor-section {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-14);
  margin: 0;
  padding: 0 0 var(--spacing-14);
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
}
.editor-section:last-of-type { border-bottom: 0; padding-bottom: 0; }
.editor-section legend {
  padding: 0 0 var(--spacing-10);
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-weight: 600;
  letter-spacing: .2px;
}
.field { display: flex; flex-direction: column; gap: 7px; color: var(--text-secondary); font-size: 0.9231rem; }
.field input, .field select, .field textarea {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  padding: 9px var(--spacing-10);
  font: inherit;
  resize: vertical;
}
.field input:focus, .field select:focus, .field textarea:focus { border-color: var(--accent-primary); outline: none; }
.field-inline { flex-direction: row; align-items: center; gap: var(--spacing-10); }
.field-inline span { flex: 0 0 auto; }
.field-inline input { width: auto; }
.field-hint { color: var(--text-tertiary); font-size: 0.8462rem; font-style: normal; }
.chips { display: flex; flex-wrap: wrap; gap: var(--spacing-6); }
.chip {
  padding: 5px var(--spacing-12);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  background: var(--bg-base);
  color: var(--text-secondary);
  font: inherit;
  font-size: 0.9231rem;
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast), background var(--transition-fast);
}
.chip:hover { color: var(--text-primary); border-color: var(--border-strong, var(--border-subtle)); }
.chip.active {
  border-color: color-mix(in srgb, var(--accent-primary) 40%, transparent);
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
  color: var(--accent-text);
}
.hint { margin: 0; color: var(--text-tertiary); font-size: 0.8462rem; line-height: 1.6; }
.form-error { color: var(--danger-text); font-size: 0.9231rem; }
</style>
