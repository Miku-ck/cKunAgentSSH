<template>
  <div class="persona-editor">
    <header class="editor-head">
      <slot name="brand" />
      <span class="head-spacer"></span>
      <span class="head-stat">{{ saving ? '保存中…' : dirty ? '有未保存的改动' : '没有改动' }}</span>
      <button type="button" class="btn" :disabled="saving" @click="emit('close')">取消</button>
      <button type="button" class="btn btn-primary" :disabled="saving || !form.name || needsAvatar" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
    </header>

    <div class="editor-shell">
      <nav class="editor-rail" aria-label="人格分区">
        <button
          v-for="item in rail"
          :key="item.key"
          type="button"
          class="rail-item"
          :class="{ active: active === item.key }"
          @click="jump(item.key)"
        >{{ item.title }}</button>
      </nav>

      <div ref="scrollRef" class="editor-scroll" @scroll.passive="onScroll">
        <section class="editor-section" data-section="identity">
          <h3>身份</h3>
          <label class="field"><span>名称</span><input v-model.trim="form.name" maxlength="50" placeholder="例如：晚风" /></label>
          <div class="field"><span>头像</span><AvatarPicker v-model="form.avatar" /></div>
          <label class="field">
            <span>它怎么称呼你</span>
            <input v-model.trim="form.address_user" maxlength="20" placeholder="留空就叫「你」；填「小林」就会叫你的名字" />
          </label>
          <label class="field"><span>开场白</span><textarea v-model="form.greeting" rows="2" maxlength="2000" placeholder="新会话里它对你说的第一句话" /></label>
          <label class="field"><span>聊天模型</span><select v-model="form.model_id"><option :value="null">跟随全局默认模型</option><option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}{{ m.model_id ? `（${m.model_id}）` : '' }}</option></select></label>
        </section>

        <section class="editor-section" data-section="personality">
          <h3 id="persona-personality">性格特征</h3>
          <div class="field"><textarea v-model="form.personality" rows="5" maxlength="5000" placeholder="话不多但接得住情绪；被夸会有点不好意思；讨厌被催…" aria-labelledby="persona-personality" /></div>
        </section>

        <section class="editor-section" data-section="background">
          <h3 id="persona-background">背景设定</h3>
          <div class="field"><textarea v-model="form.background" rows="5" maxlength="10000" placeholder="它住在哪、在做什么、你们是怎么认识的…" aria-labelledby="persona-background" /></div>
        </section>

        <section class="editor-section" data-section="voice">
          <h3>说话风格</h3>
          <p v-if="!options" class="form-error">档位读取失败，其余设定仍可保存。</p>
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
          <label class="field field-inline"><span>主动找你</span>
            <input type="checkbox" v-model="form.proactive_enabled" />
            <em class="field-hint">她会在想找你时主动开口（有静音时段、频率上限和冷场冷却守着分寸）</em>
          </label>
        </section>

        <section v-for="item in textSections" :key="item.key" class="editor-section" :data-section="item.key">
          <h3 :id="`persona-${item.key}`">{{ item.title }}</h3>
          <div class="field">
            <textarea v-model="form[item.key]" :rows="item.rows" :maxlength="item.max" :placeholder="item.placeholder" :aria-labelledby="`persona-${item.key}`" />
          </div>
        </section>

        <section class="editor-section" data-section="play">
          <h3 id="persona-play">进阶玩法</h3>
          <div class="field">
            <span>{{ options?.relationship_speed.label || '关系升温' }}</span>
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
          <div class="field">
            <textarea v-model="form.advanced_play" rows="5" maxlength="2000" placeholder="可以：树洞模式（只听不建议）、一起推方案、互相打卡。&#10;不可以：扮演别人、替我做决定。" aria-labelledby="persona-play" />
          </div>
        </section>

        <div v-if="error" class="form-error">{{ error }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
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

// 三个纯文本分区按清单渲染，placeholder 就是唯一的填写指引；目录与长卷共用这份顺序。
const textSections = [
  {
    key: 'likes_landmines',
    title: '喜好雷区',
    rows: 4,
    max: 2000,
    placeholder: '喜欢：夜里散步、老唱片、把一件事讲清楚；雷区：被催、被叫「宝贝」',
  },
  {
    key: 'memory_directive',
    title: '记忆设定',
    rows: 4,
    max: 2000,
    placeholder: '记住我在准备面试，偶尔问一句进展；他说「别记」时立刻收手',
  },
  {
    key: 'dialogue_samples',
    title: '互动示例',
    rows: 7,
    max: 4000,
    placeholder: '用户：今天好累啊\n晚风：又熬到几点？\n\n用户：帮我写下周报\n晚风：这个我不做，但你念我帮你捋。',
  },
] as const satisfies readonly {
  key: 'likes_landmines' | 'memory_directive' | 'dialogue_samples'
  title: string
  rows: number
  max: number
  placeholder: string
}[]

const rail = computed(() => [
  { key: 'identity', title: '身份' },
  { key: 'personality', title: '性格特征' },
  { key: 'background', title: '背景设定' },
  { key: 'voice', title: '说话风格' },
  ...textSections.map(item => ({ key: item.key as string, title: item.title })),
  { key: 'play', title: '进阶玩法' },
])

const saving = ref(false)
const error = ref('')
const scrollRef = ref<HTMLElement | null>(null)
const active = ref('identity')

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
  proactive_enabled: source?.proactive_enabled === 1,
  // 旧数据可能缺新字段：整体兜底，避免 v-model 绑到 undefined 后控件变哑
  expression_dna: { ...DNA_DEFAULTS, ...(source?.expression_dna || {}) } as BotExpressionDna,
  reaction_style: (source?.reaction_style || 'warm') as BotReactionStyle,
  refusal_style: (source?.refusal_style || 'soft') as BotRefusalStyle,
  address_user: source?.address_user || '',
  relationship_speed: (source?.relationship_speed || 'normal') as BotRelationshipSpeed,
  // 人格内核四件套：旧记录可能没有，缺省读空串
  likes_landmines: source?.likes_landmines || '',
  memory_directive: source?.memory_directive || '',
  dialogue_samples: source?.dialogue_samples || '',
  advanced_play: source?.advanced_play || '',
  // 禁用词用一行文本编辑，比 tag 编辑省事；保存时拆成数组
  tabooText: (source?.expression_dna?.taboo || []).join('、'),
})
const pristine = JSON.stringify({ ...form })
const dirty = computed(() => JSON.stringify({ ...form }) !== pristine)

// 只有新建才强制先传头像：内置 cKunBot 等已有人格常为空头像，拦住就一个字段都改不了
const needsAvatar = computed(() => !source && !isImageAvatar(form.avatar))

function setDna(key: (typeof dnaKeys)[number], value: string) {
  // 档位取值由服务端字典约束，这里断言只为满足联合类型，不会越界
  form.expression_dna[key] = value as never
}

function setStyle(key: (typeof styleKeys)[number], value: string) {
  form[key] = value as never
}

function onScroll() {
  const el = scrollRef.value
  if (!el) return
  const base = el.getBoundingClientRect().top
  let current = rail.value[0].key
  for (const node of Array.from(el.querySelectorAll<HTMLElement>('[data-section]'))) {
    if (node.getBoundingClientRect().top - base <= 16) current = node.dataset.section || current
  }
  active.value = current
}

function jump(key: string) {
  scrollRef.value?.querySelector<HTMLElement>(`[data-section="${key}"]`)
    ?.scrollIntoView({ block: 'start', behavior: 'smooth' })
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
    proactive_enabled: form.proactive_enabled,
    expression_dna: { ...form.expression_dna, taboo },
    reaction_style: form.reaction_style,
    refusal_style: form.refusal_style,
    address_user: form.address_user,
    relationship_speed: form.relationship_speed,
    likes_landmines: form.likes_landmines,
    memory_directive: form.memory_directive,
    dialogue_samples: form.dialogue_samples,
    advanced_play: form.advanced_play,
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

onMounted(() => {
  onScroll()
})
</script>

<style scoped>
/* 顶栏 padding 抄 bot-header（含它既成的 28px）：编辑态顶替的就是同一行，
   品牌横移会让人觉得切了页。 */
.editor-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  padding: var(--spacing-20) 28px var(--spacing-12);
  border-bottom: 1px solid var(--border-subtle);
}
.head-spacer { flex: 1; }
.head-stat { color: var(--text-tertiary); font-size: 0.8462rem; }
.persona-editor { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.editor-shell { flex: 1; min-height: 0; display: flex; }
.editor-rail {
  flex: 0 0 190px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--spacing-16) var(--spacing-8);
  border-right: 1px solid var(--divider-primary);
  overflow-y: auto;
}
.rail-item {
  padding: 6px var(--spacing-10);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font: inherit;
  font-size: 0.9231rem;
  text-align: left;
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast);
}
.rail-item:hover { color: var(--text-primary); background: var(--bg-hover); }
.rail-item.active {
  color: var(--accent-text);
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
}
.editor-scroll { flex: 1; min-width: 0; overflow-y: auto; padding: var(--spacing-20) var(--spacing-24) var(--spacing-24); }
.editor-section {
  max-width: 760px;
  margin: 0 auto var(--spacing-20);
  padding-bottom: var(--spacing-20);
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-14);
  scroll-margin-top: var(--spacing-16);
}
.editor-section:last-of-type { border-bottom: 0; }
.editor-section h3 {
  margin: 0;
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
  font-size: 0.9231rem;
  line-height: 1.6;
  resize: vertical;
}
.field input:focus, .field select:focus, .field textarea:focus {
  border-color: var(--accent-primary);
  outline: none;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-primary) 16%, transparent);
}
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
.chip:hover { color: var(--text-primary); border-color: var(--border-default); }
.chip.active {
  border-color: color-mix(in srgb, var(--accent-primary) 40%, transparent);
  background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
  color: var(--accent-text);
}
.form-error { color: var(--danger-text); font-size: 0.9231rem; }
@media (max-width: 900px) { .editor-rail { display: none; } }
</style>
