<template>
  <div class="panel">
    <header class="panel-header">
      <h3>对话</h3>
      <p class="panel-desc">配置对话行为的默认偏好</p>
    </header>
    <div class="panel-body">
      <SettingRow label="发送快捷键" desc="选择发送消息的快捷键">
        <select v-model="settings.chat.sendShortcut" class="setting-select">
          <option value="enter">Enter 发送</option>
          <option value="shiftEnter">Shift + Enter 发送</option>
        </select>
      </SettingRow>

      <SettingRow label="思考链默认" desc="推理模型的思考过程默认展开">
        <select v-model="settings.chat.thinkingExpanded" class="setting-select">
          <option :value="false">折叠</option>
          <option :value="true">展开</option>
        </select>
      </SettingRow>

      <!-- E2：三态而非开关。'inherit' 保留"不发该字段"，让后端演化配置接管；
           强制开/关覆盖演化。选 inherit 就是回到演化默认（多数模型 off） -->
      <SettingRow label="深度思考模式" desc="Agent 运行是否强制启用思考链，或交由演化配置决定">
        <select v-model="settings.chat.enableThinking" class="setting-select">
          <option value="inherit">跟随演化（默认）</option>
          <option value="on">强制开启</option>
          <option value="off">强制关闭</option>
        </select>
      </SettingRow>

      <SettingRow label="自动滚动" desc="新消息自动滚动到底部">
        <ToggleSwitch v-model="settings.chat.autoScroll" />
      </SettingRow>

      <SettingRow label="消息字号" desc="对话气泡中的文字大小">
        <select v-model.number="settings.chat.messageFontSize" class="setting-select">
          <option :value="12">12px</option>
          <option :value="13">13px</option>
          <option :value="14">14px</option>
          <option :value="15">15px</option>
        </select>
      </SettingRow>

      <SettingRow label="Markdown 渲染" desc="启用 Markdown 格式渲染">
        <ToggleSwitch v-model="settings.chat.markdown" />
      </SettingRow>

      <SettingRow label="自动技能匹配" desc="根据触发词自动调用已启用的 skill">
        <ToggleSwitch v-model="settings.chat.autoSkillMatch" />
      </SettingRow>

      <SettingRow label="上下文窗口" desc="保留最近多少条消息作为上下文">
        <!-- 下限 1 与后端 Field(ge=1) 同口径；原 min=5 让「只带最后一条」这一档
             在面板上根本选不出来（e2e 回归用例自己就用 context_window: 2） -->
        <SettingSlider v-model="settings.chat.contextWindow" :min="1" :max="100" unit=" 条" />
      </SettingRow>

      <div class="section-divider" />

      <div class="default-model-info">
        <span class="default-model-label">当前默认模型（{{ defaultModelTierText }}）</span>
        <span class="default-model-name">{{ defaultModelName }}</span>
        <span v-if="defaultModelId" class="default-model-id">{{ defaultModelId }}</span>
        <p v-if="!defaultSshModel && settingsStore.modelConfigs.length" class="hint saving">
          没有任何模型被标为默认 —— 在「模型」页点「★ SSH 默认」指定一个。
          对话里手选过模型时按那一个跑；两者都缺时后端会以「未找到默认模型」拒绝这次运行。
        </p>
      </div>

      <div class="section-divider" />

      <h4 class="section-title">后处理</h4>

      <SettingRow label="后处理总开关" desc="单轮对话完成后执行标题生成、记忆提取、技能生成">
        <ToggleSwitch v-model="settings.postProcess.enabled" />
      </SettingRow>

      <SettingRow label="自动生成标题" desc="首轮对话后根据内容生成会话标题">
        <ToggleSwitch v-model="settings.postProcess.generateTitle" />
      </SettingRow>

      <SettingRow label="自动提取记忆" desc="从对话中提取用户偏好和关键事实">
        <ToggleSwitch v-model="settings.postProcess.extractMemory" />
      </SettingRow>

      <SettingRow label="自动生成技能" desc="从对话中沉淀可复用技能（每 3 轮触发）">
        <ToggleSwitch v-model="settings.postProcess.generateSkill" />
      </SettingRow>

      <!-- 与 MemoryPanel 的 memoryLoaded 同一件事的另一半：后处理四项走的是同一套
           createBackendSync（只发改动过的字段，数据侧不会再抹库），但"面板显示的就是
           库里的值"这句话在第一次成功读到之前是假的 —— 启动时后端未就绪是常态，
           那时这四项是 localStorage 默认，用户拨动任一开关前有权知道。 -->
      <p v-if="!settingsStore.postProcessLoaded" class="hint saving unread">
        尚未读到服务端后处理设置，以上为本地保存值。
        <button type="button" class="retry-btn" @click="settingsStore.loadPostProcessSettings()">重试</button>
      </p>

      <div class="section-divider" />

      <h4 class="section-title">会话保留</h4>

      <SettingRow label="自动清理旧会话" desc="开启后定期删除最旧的已结束会话（进行中的会话永不删除）。默认关闭，建议开启以避免数据库长期膨胀">
        <ToggleSwitch :model-value="retention.enabled" @update:model-value="onRetentionToggle" />
      </SettingRow>

      <SettingRow
        v-if="retention.enabled"
        label="保留会话数"
        desc="按最近活跃保留的会话条数，超出部分自动清理；填 0 = 不限制，也就是一条都不删"
      >
        <input
          v-model.number="retention.keepCount"
          type="number"
          min="0"
          max="100000"
          class="setting-number"
          @change="onRetentionCountChange"
        />
      </SettingRow>
      <p v-if="retentionSaving" class="hint saving">保存中…</p>
      <!-- 读失败时说实话：现在这套是本地默认，不是库里的值。保存只发改动过的那一项，
           所以不会再抹掉库里另一项，但"显示得和库里一样"这件事仍然是假的 -->
      <p v-if="retentionUnread" class="hint saving unread">
        未能读到服务端当前保留策略，以上为本地默认值。
        <button type="button" class="retry-btn" @click="loadRetention">重试</button>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { api, toFriendlyMessage } from '@/api/client'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import SettingSlider from '@/components/settings/fields/SettingSlider.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

const settingsStore = useSettingsStore()
const settings = settingsStore.settings
const toast = useToastStore()

// A1：默认模型显示改为响应式派生自 store.modelConfigs。
// 原来是 onMounted 里 api.getDefaultModel 一次性拉 → 用户切了默认后
// 本面板仍显示旧值。改派生后，只要 fetchModelConfigs 刷新过一次
// （SettingsDialog 打开时 ModelsPanel 会拉），卡片 star 与这里就同步。
// 这条 ssh → global 的两级链必须与运行时的取档顺序一致（routes/agent.py、
// terminal、schedule_engine、reconcile 都是 ssh 优先再回落全局档），少一级就会
// 出现「面板写着当前默认是 B，实际跑的是 A」。
const defaultSshModel = computed(() => {
  const list = settingsStore.modelConfigs
  // 只认打了标记的行。原来还有第三级 ?? list[0]，抄的是 GET /settings/models/default
  // 的兜底，而运行时（routes/agent.py、terminal、schedule_engine）只有 ssh → global
  // 两级、都没有就 400「未找到默认模型」。把没标记的第一行报成"当前默认"，等于替
  // ModelsPanel 的星标撒谎：那一页一个星都没亮，这一页却写着默认是 B。
  return list.find((m) => m.is_default_ssh) ?? list.find((m) => m.is_default) ?? null
})
// 档位说真话：命中的是全局档（⊕）时仍标（ssh）就是假的 —— ModelsPanel 里
// 那颗星是亮在⊕上而不是★上，两处说法得和兜底链的同一条口径一致。
const defaultModelTierText = computed(() =>
  defaultSshModel.value?.is_default_ssh ? 'ssh' : '全局档回落')
const defaultModelName = computed(() => {
  const m = defaultSshModel.value
  if (!m) return '未配置'
  return m.name || m.model_id || m.id
})
const defaultModelId = computed(() => defaultSshModel.value?.model_id || '')

// ── 会话保留（服务端设置，默认关闭；删除用户数据，故默认不启用）──
const retention = ref<{ enabled: boolean; keepCount: number }>({ enabled: false, keepCount: 1000 })
const retentionSaving = ref(false)
// 读失败必须说破：这时面板上显示的是本地默认值，不是库里的真值。
// 原实现的 catch 是空的，用户看到的 "{关闭, 1000}" 与库里实际的 "{开启, 20}" 毫无关系。
const retentionUnread = ref(false)

async function loadRetention() {
  try {
    const data = await api.getConvRetention()
    // ?? 而不是 ||：0 是后端允许写入的合法值，用 || 会把它显示成 1000。
    // 注意 0 的含义是「不限制 = 一条都不删」（prune_conversations 的 <=0 早退），
    // 不是「清空全部」—— 旧注释与旧文案都反着写，用户会以为开了却不清库。
    retention.value = { enabled: !!data.enabled, keepCount: data.keep_count ?? 1000 }
    retentionUnread.value = false
  } catch {
    // 加载失败保持默认关闭（安全方向），但把"没读到"标出来并给重试入口
    retentionUnread.value = true
  }
}

async function saveRetention(patch: Partial<{ enabled: boolean; keepCount: number }>) {
  const prev = { ...retention.value }
  // 只发被改动的那一项。后端 ConversationRetentionRequest 两字段都是 Optional，注释
  // 就写着「只发 enabled 的调用不该把用户调过的 keep_count 改回去」，而原实现恒发整包：
  // GET 没成功时本地带着 {false,1000}，拨一下开关就把库里真实的 keep_count 抹成 1000,
  // 而这条设置打开后是会真删会话的。
  const body: Partial<{ enabled: boolean; keep_count: number }> = {}
  if (patch.enabled !== undefined) body.enabled = patch.enabled
  if (patch.keepCount !== undefined) body.keep_count = patch.keepCount
  const next = {
    enabled: patch.enabled ?? prev.enabled,
    keepCount: patch.keepCount ?? prev.keepCount,
  }
  // 乐观更新
  retention.value = { ...next }
  retentionSaving.value = true
  try {
    // 回包是整份权威值，拿它收敛：连带把之前没读到的另一半也对齐
    const data = await api.updateConvRetention(body)
    retention.value = { enabled: !!data.enabled, keepCount: data.keep_count ?? next.keepCount }
    retentionUnread.value = false
  } catch (e: unknown) {
    retention.value = { ...prev }
    toast.error(toFriendlyMessage(e, '会话保留设置保存失败'))
  } finally {
    retentionSaving.value = false
  }
}

function onRetentionToggle(v: boolean) {
  void saveRetention({ enabled: v })
}

function onRetentionCountChange(ev?: Event) {
  // 钳制区间与后端 Field(ge=0, le=100000) 一致。输入框被清空时 Number('') 塌成 0，
  // 而 0 = 「不限制 = 一条都不删」：清空一下就把刚打开的清理又关掉了，所以空/NaN
  // 回弹默认值但不保存。原 min=10 会把用户显式输入的 0 静默改成 10。
  const raw = ev?.target ? (ev.target as HTMLInputElement).value : null
  if (raw === null || raw.trim() === '') {
    retention.value.keepCount = 1000
    return
  }
  const n = Math.max(0, Math.min(100000, Number(raw)))
  if (!Number.isFinite(n)) {
    retention.value.keepCount = 1000
    return
  }
  retention.value.keepCount = n
  void saveRetention({ keepCount: n })
}

onMounted(async () => {
  void loadRetention()
  // 与 MemoryPanel 的 loadMemorySettings 同型：本面板挂载时补拉一次后处理，
  // 覆盖「启动时后端未就绪 → store 静默降级到 localStorage 默认值」。缺这一步时
  // 面板显示的四项可能与库里相反，而用户第一次拨动任一开关就是整包 PUT 四项 ——
  // 会把没碰过的三个开关按本地默认值写回去。
  void settingsStore.loadPostProcessSettings()
  // 首次进对话面板时若还没拉过模型列表，这里补一次；有数据后 computed 会自动填
  if (!settingsStore.modelConfigs.length) void settingsStore.fetchModelConfigs()
})
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
/* 面板骨架与 .setting-select 见 settings-panel-base.css */

.setting-number {
  width: 100px;
  height: 28px;
  padding: 0 8px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 12px;
  font-family: inherit;
  outline: none;
}
.setting-number:focus { border-color: var(--accent-primary); }
.hint.saving { margin: 8px 0 0; font-size: 11px; color: var(--text-tertiary); }
/* .unread 必须单独上色：模板里 class 写了 unread 却没有任何规则接它时，这条
   「尚未读到服务端」的提示会跟普通灰字一个亮度 —— 与它在讲的事（显示的是本地
   值，不是库里的）正好相反。MemoryPanel 的 .foot-hint.unread 是同一条规则。 */
.hint.saving.unread { color: var(--status-warn-text); }

.section-divider {
  height: 1px;
  background: var(--border-subtle);
  margin: 20px 0 16px;
}

.section-title {
  margin: 0 0 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.default-model-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 4px;
}

.default-model-label {
  font-size: 10px;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.default-model-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}

.default-model-id {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}
</style>
