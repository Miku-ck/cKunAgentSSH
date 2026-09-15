<template>
  <div class="panel">
    <header class="panel-header">
      <h3>Agent 治理</h3>
      <p class="panel-desc">工具授权、审批白名单与自演化策略 —— 让"能真正执行运维"的 Agent 可见、可控、可回收</p>
    </header>

    <div class="panel-body">
      <!-- ── 工具开关 ── -->
      <section class="grp">
        <button class="grp-head" @click="open.tools = !open.tools">
          <span class="grp-caret" :class="{ open: open.tools }">▸</span>
          <span class="grp-title">工具开关</span>
          <span class="grp-badge">{{ enabledToolCount }}/{{ tools.length }} 启用</span>
        </button>
        <div v-show="open.tools" class="grp-body">
          <p v-if="toolsLoading" class="hint">加载中…</p>
          <p v-else-if="toolsError" class="hint err">{{ toolsError }}</p>
          <template v-else>
            <p class="hint">关闭后该工具不再暴露给模型执行。危险档（dangerous）命令级授权仍需逐条审批。</p>
            <div v-for="grp in toolGroups" :key="grp.risk" class="risk-group">
              <div class="risk-label" :class="grp.risk">{{ riskText[grp.risk] }}（{{ grp.list.length }}）</div>
              <div v-for="t in grp.list" :key="t.name" class="tool-row">
                <div class="tool-info">
                  <span class="tool-name">{{ t.name }}</span>
                  <span class="tool-desc" :title="t.description">{{ t.description }}</span>
                </div>
                <div class="tool-flags">
                  <span v-if="t.reversible" class="flag ok">可撤销</span>
                  <span v-if="t.cacheable" class="flag">可缓存</span>
                  <span class="flag">{{ t.timeout }}s</span>
                </div>
                <ToggleSwitch
                  :model-value="t.enabled"
                  @update:model-value="(v: boolean) => onToggleTool(t, v)"
                />
              </div>
            </div>
          </template>
        </div>
      </section>

      <!-- ── 审批白名单 ── -->
      <section class="grp">
        <button class="grp-head" @click="open.perm = !open.perm">
          <span class="grp-caret" :class="{ open: open.perm }">▸</span>
          <span class="grp-title">审批授权</span>
          <span class="grp-badge">{{ permTools.length }} 个工具已永久授权</span>
        </button>
        <div v-show="open.perm" class="grp-body">
          <p class="hint">
            审批弹窗点"始终允许"后写入此白名单（持久化，进程重启仍生效）。撤销立即生效。
            <button class="link-btn" @click="loadPermissions">刷新</button>
          </p>
          <p v-if="!permTools.length" class="hint muted">暂无永久授权</p>
          <div v-for="p in permTools" :key="p.tool" class="perm-row">
            <div class="tool-info">
              <span class="tool-name">{{ p.tool }}</span>
              <span class="tool-desc">{{ p.keys.length }} 条授权键（{{ p.keys.map(k => k.split(':')[1] || '工具级').slice(0, 3).join('、') }}{{ p.keys.length > 3 ? ' …' : '' }}）</span>
            </div>
            <button class="btn danger" :disabled="revoking === p.tool" @click="onRevoke(p.tool)">
              {{ revoking === p.tool ? '撤销中…' : '全部撤销' }}
            </button>
          </div>
        </div>
      </section>

      <!-- ── 策略与演化 ── -->
      <section class="grp">
        <button class="grp-head" @click="open.strat = !open.strat">
          <span class="grp-caret" :class="{ open: open.strat }">▸</span>
          <span class="grp-title">策略与自演化</span>
          <span class="grp-badge" :class="{ live: tickStatus?.running }">
            {{ tickStatus ? (tickStatus.running ? '意识循环运行中' : '意识循环已停') : '加载中…' }}
          </span>
        </button>
        <div v-show="open.strat" class="grp-body">
          <p class="hint">
            Agent 每次运行后自动反思并演化配置策略，<b>下一轮运行即刻读回生效</b>（当前立场：自动应用 + 全程可见）。
            这里可逐条停用或淘汰不再合理的演化，无需重启。
            <button class="link-btn" @click="loadStrategies">刷新</button>
          </p>
          <p v-if="stratError" class="hint err">{{ stratError }}</p>
          <p v-else-if="!strategies.length" class="hint muted">尚无演化策略（运行若干任务后自动产生）</p>
          <div v-for="s in strategies" :key="s.id" class="strat-row" :class="{ off: !s.enabled }">
            <div class="tool-info">
              <span class="strat-name">{{ s.name }}</span>
              <span class="tool-desc" :title="s.content">{{ s.content }}</span>
              <span class="strat-meta">
                <span class="flag" :class="s.strategy_type === 'evolved_config' ? 'vio' : 'info'">
                  {{ s.strategy_type === 'evolved_config' ? '超参演化' : '反思洞察' }}
                </span>
                <span class="flag">评分 {{ s.score.toFixed(2) }}</span>
                <span class="flag">{{ fmtDate(s.updated_at) }}</span>
              </span>
            </div>
            <ToggleSwitch
              :model-value="!!s.enabled"
              @update:model-value="(v: boolean) => onToggleStrategy(s, v)"
            />
            <button class="btn danger sm" @click="onDeleteStrategy(s)">淘汰</button>
          </div>
        </div>
      </section>

      <section class="grp static">
        <div class="grp-head">
          <span class="grp-title">运行历史与 Trace 回放</span>
        </div>
        <div class="grp-body">
          <p class="hint">
            每次 Agent 运行的完整轨迹（每步命令、耗时、成本、状态流转）已内联在对话气泡的执行卡中，
            点卡片上的「轨迹」即可就地回放。全局任务列表页将随「运维审计」后续版本提供。
          </p>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * AgentPanel — 设置内 Agent 治理面板
 *
 * 对接后端已闭环、此前前端零引用的管理端点（src/api/client.ts agentAdminApi）：
 *   工具开关 GET /agent/tools · PATCH /agent/tools/{name}/enabled
 *   审批授权 GET /agent/permission/list · POST /agent/permission/revoke
 *   策略演化 GET /agent/strategies · PATCH .../enabled · DELETE · GET /agent/tick/status
 * 产品立场（2026-09 与 owner 确认）：自演化保持自动应用 + 全程可见可停用。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  agentAdminApi,
  toFriendlyMessage,
  type AgentToolInfo,
  type AgentStrategyItem,
  type TickStatusResponse,
} from '@/api/client'
import { useToastStore } from '@/stores/toast'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

const toast = useToastStore()

const open = reactive({ tools: true, perm: false, strat: false })

// ── 工具开关 ──
const tools = ref<AgentToolInfo[]>([])
const toolsLoading = ref(false)
const toolsError = ref('')
const riskText: Record<string, string> = { safe: '只读', moderate: '中等', dangerous: '危险' }
const riskOrder = ['dangerous', 'moderate', 'safe'] as const

const toolGroups = computed(() =>
  riskOrder
    .map((risk) => ({ risk, list: tools.value.filter((t) => t.risk_level === risk) }))
    .filter((g) => g.list.length > 0),
)
const enabledToolCount = computed(() => tools.value.filter((t) => t.enabled).length)

async function loadTools() {
  toolsLoading.value = true
  toolsError.value = ''
  try {
    const data = await agentAdminApi.getTools('ssh')
    tools.value = data.tools
  } catch (e) {
    toolsError.value = toFriendlyMessage(e, '工具列表加载失败')
  } finally {
    toolsLoading.value = false
  }
}

async function onToggleTool(t: AgentToolInfo, next: boolean) {
  const prev = t.enabled
  t.enabled = next
  try {
    await agentAdminApi.setToolEnabled(t.name, next)
  } catch (e) {
    t.enabled = prev
    toast.error(`「${t.name}」${next ? '启用' : '禁用'}失败：${toFriendlyMessage(e)}`)
  }
}

// ── 审批白名单 ──
const permKeys = ref<string[]>([])
const revoking = ref('')
const permTools = computed(() => {
  const map = new Map<string, string[]>()
  for (const key of permKeys.value) {
    const tool = key.split(':')[0]
    if (!map.has(tool)) map.set(tool, [])
    map.get(tool)!.push(key)
  }
  return [...map.entries()].map(([tool, keys]) => ({ tool, keys }))
})

async function loadPermissions() {
  try {
    const data = await agentAdminApi.listPermissions()
    permKeys.value = data.tools
  } catch (e) {
    toast.error(`授权列表加载失败：${toFriendlyMessage(e)}`)
  }
}

async function onRevoke(tool: string) {
  revoking.value = tool
  try {
    await agentAdminApi.revokePermission(tool)
    await loadPermissions()
    toast.success(`已撤销「${tool}」的全部永久授权`)
  } catch (e) {
    toast.error(`撤销失败：${toFriendlyMessage(e)}`)
  } finally {
    revoking.value = ''
  }
}

// ── 策略与演化 ──
const strategies = ref<AgentStrategyItem[]>([])
const stratError = ref('')
const tickStatus = ref<TickStatusResponse | null>(null)

async function loadStrategies() {
  stratError.value = ''
  try {
    const [sData, tick] = await Promise.all([
      agentAdminApi.listStrategies({ mode: 'ssh' }),
      agentAdminApi.getTickStatus().catch(() => null),
    ])
    strategies.value = sData.strategies
    tickStatus.value = tick
  } catch (e) {
    stratError.value = toFriendlyMessage(e, '策略列表加载失败')
  }
}

async function onToggleStrategy(s: AgentStrategyItem, next: boolean) {
  const prev = s.enabled
  s.enabled = next ? 1 : 0
  try {
    await agentAdminApi.setStrategyEnabled(s.id, next)
  } catch (e) {
    s.enabled = prev
    toast.error(`策略${next ? '启用' : '停用'}失败：${toFriendlyMessage(e)}`)
  }
}

async function onDeleteStrategy(s: AgentStrategyItem) {
  try {
    await agentAdminApi.deleteStrategy(s.id)
    strategies.value = strategies.value.filter((x) => x.id !== s.id)
    toast.success(`已淘汰策略「${s.name}」`)
  } catch (e) {
    toast.error(`淘汰失败：${toFriendlyMessage(e)}`)
  }
}

// ── 工具函数 ──
function fmtDate(iso: string): string {
  try {
    return new Date(iso.replace(' ', 'T') + (/[Z+]/.test(iso) ? '' : 'Z')).toLocaleDateString('zh-CN')
  } catch {
    return iso
  }
}

onMounted(() => {
  void loadTools()
  void loadPermissions()
  void loadStrategies()
})
</script>

<style scoped>
.panel { height: 100%; display: flex; flex-direction: column; }
.panel-header { margin-bottom: 16px; flex-shrink: 0; }
.panel-header h3 { margin: 0 0 4px; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.panel-desc { margin: 0; font-size: 12px; color: var(--text-secondary); }
.panel-body { flex: 1; overflow-y: auto; padding-right: 4px; }

.grp {
  border: 1px solid var(--border-subtle, var(--border-default));
  border-radius: 6px;
  margin-bottom: 10px;
  background: var(--bg-input, transparent);
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
.grp-badge.live { color: var(--accent-primary, #6aa97f); }
.grp.static .grp-badge { display: none; }
.grp-body { padding: 2px 12px 12px; }

.hint { margin: 0 0 8px; font-size: 11.5px; line-height: 1.5; color: var(--text-secondary); }
.hint.err { color: #d98f86; }
.hint.muted { color: var(--text-tertiary); }
.link-btn {
  background: none; border: none; color: var(--accent-primary, #7aa2d0);
  cursor: pointer; font-size: 11.5px; font-family: inherit; padding: 0; text-decoration: underline;
}

.risk-group { margin-bottom: 10px; }
.risk-label {
  font-size: 11px; color: var(--text-tertiary); margin: 6px 0 4px;
  text-transform: none; font-weight: 600;
}
.risk-label.dangerous { color: #cf8a80; }
.risk-label.moderate { color: #c8a25e; }

.tool-row, .perm-row, .strat-row {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 4px; border-top: 1px solid var(--border-subtle, rgba(255,255,255,.05));
}
.strat-row.off { opacity: .55; }
.tool-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.tool-name { font-size: 12.5px; font-family: ui-monospace, Consolas, monospace; color: var(--text-primary); }
.strat-name { font-size: 12.5px; font-weight: 600; color: var(--text-primary); }
.tool-desc {
  font-size: 11px; color: var(--text-secondary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tool-flags { display: flex; gap: 4px; flex-shrink: 0; }
.flag {
  font-size: 10px; padding: 1px 6px; border-radius: 8px; white-space: nowrap;
  border: 1px solid var(--border-default); color: var(--text-tertiary);
}
.flag.ok { color: #7fbc92; border-color: rgba(127,188,146,.35); }
.flag.vio { color: #a98fd0; border-color: rgba(169,143,208,.35); }
.flag.info { color: #7aa2d0; border-color: rgba(122,162,208,.35); }
.strat-meta { display: flex; gap: 5px; margin-top: 3px; }

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
