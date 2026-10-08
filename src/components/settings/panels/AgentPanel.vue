<template>
  <div class="panel">
    <header class="panel-header">
      <h3>Agent 治理</h3>
      <p class="panel-desc">工具授权、审批白名单与自演化策略 —— 让"能真正执行运维"的 Agent 可见、可控、可回收</p>
    </header>

    <div class="panel-body">
      <!-- ── 运行预算 ── -->
      <section class="grp">
        <button class="grp-head" @click="open.budget = !open.budget">
          <span class="grp-caret" :class="{ open: open.budget }">▸</span>
          <span class="grp-title">运行预算熔断</span>
          <span class="grp-badge">{{ budgetSummary }}</span>
        </button>
        <div v-show="open.budget" class="grp-body">
          <p class="hint">
            每次 Agent 运行的成本上限（美元）与 token 上限。超出后 run 以
            <code>budget_exceeded</code> 终止，已完成的工具效果保留。0 = 使用后端默认预算。
            这两项落在对话与终端的运行上；后台定时任务另有内置上限，不受这里约束。
          </p>
          <div class="budget-row">
            <label class="budget-field">
              <span class="budget-label">成本上限（USD）</span>
              <input
                v-model.number="budget.maxCostUsd"
                class="num-input"
                :class="{ 'input-error': budgetCostInvalid }"
                type="number"
                :min="BUDGET_LIMITS.maxCostUsd.min"
                :max="BUDGET_LIMITS.maxCostUsd.max"
                step="0.1"
                placeholder="0"
                @change="normalizeBudget"
              />
            </label>
            <label class="budget-field">
              <span class="budget-label">Token 上限</span>
              <input
                v-model.number="budget.maxTotalTokens"
                class="num-input"
                :class="{ 'input-error': budgetTokenInvalid }"
                type="number"
                :min="BUDGET_LIMITS.maxTotalTokens.min"
                :max="BUDGET_LIMITS.maxTotalTokens.max"
                step="10000"
                placeholder="0"
                @change="normalizeBudget"
              />
            </label>
          </div>
          <div class="runtime-toggle-row">
            <div class="runtime-toggle-info">
              <span class="runtime-label">关闭预算熔断</span>
              <span class="runtime-desc">
                后端把显式 <code>0</code> 解释为「不设上限」。长批量任务被默认的 $2 /
                3M token 掐断时用它；代价是这次跑飞了没有硬止损。
              </span>
            </div>
            <ToggleSwitch v-model="budget.breakerDisabled" />
          </div>
          <p class="hint muted">
            修改即时保存到本地设置，下一次运行生效。数字框里的 0 = 不覆盖后端默认；
            要真正取消熔断请勾上面那一项（它才发显式的 0）。
          </p>
        </div>
      </section>

      <!-- ── 运行控制（A3/A4/E1）── -->
      <section class="grp">
        <button class="grp-head" @click="open.runtime = !open.runtime">
          <span class="grp-caret" :class="{ open: open.runtime }">▸</span>
          <span class="grp-title">运行控制</span>
          <span class="grp-badge">{{ runtimeSummary }}</span>
        </button>
        <div v-show="open.runtime" class="grp-body">
          <p class="hint">
            迭代上限、动态步数预算与意图引导共同决定单次 Agent run 的"步数弹性"。
            三者互不冲突：动态预算接管后 <code>max_iterations</code> 作为演化异常时的兜底；
            意图引导关闭则跳过规划阶段与思考预算限制（旧行为）。
          </p>
          <div class="runtime-row">
            <label class="runtime-field">
              <span class="runtime-label">最大迭代步数</span>
              <SettingSlider v-model="runtimeCfg.maxIterations" class="slider-inline" :min="1" :max="500" unit="" />
            </label>
            <p class="hint muted runtime-desc">
              后端 <code>max_iterations</code> 允许 1-500。动态预算开启时优先使用演化习得值，越界才退回此上限。
              这一项与上面的预算都只约束对话/终端里的运行：后台定时任务是 <code>schedule_engine</code>
              固定的 8 步加内置上限，不读设置页这几个值。
            </p>
          </div>
          <div class="runtime-toggle-row">
            <div class="runtime-toggle-info">
              <span class="runtime-label">动态步数预算</span>
              <span class="runtime-desc">允许演化模块按 <code>max_iterations</code>/温度/输出长度自学习覆盖</span>
            </div>
            <ToggleSwitch v-model="runtimeCfg.dynamicBudget" />
          </div>
          <div class="runtime-toggle-row">
            <div class="runtime-toggle-info">
              <span class="runtime-label">意图识别 + 思考引导</span>
              <span class="runtime-desc">关闭后 run 跳过规划阶段、无思考预算限制、无引导语注入（旧行为）</span>
            </div>
            <ToggleSwitch v-model="runtimeCfg.enableIntentionGuide" />
          </div>
        </div>
      </section>

      <!-- ── 运维策略（E3/E4/E5）── -->
      <section class="grp">
        <button class="grp-head" @click="open.ops = !open.ops">
          <span class="grp-caret" :class="{ open: open.ops }">▸</span>
          <span class="grp-title">运维策略</span>
          <span class="grp-badge">{{ opsSummary }}</span>
        </button>
        <div v-show="open.ops" class="grp-body">
          <p class="hint">
            授权保留时长落在后端 <code>~/.ckunagentic/allowlist.txt</code> 的判定上，改完即刻生效（无需重启）；
            自动审批开关与审批兜底等待同样即时落到后端裁决链路。下面的默认环境等其余项是本机渲染端策略。
          </p>
          <p v-if="!policyLoaded" class="hint err">
            未能读取服务端的授权策略：下面三项显示的是本地默认值，不代表库里是什么。
            <button class="link-btn" @click="loadPermissionPolicy({ notify: true })">重试</button>
          </p>

          <div class="ops-toggle-row">
            <div class="ops-info">
              <span class="runtime-label">授权保留时长</span>
              <span class="runtime-desc">
                「始终允许」与手动登记的永久授权多久后自动失效。默认「永久」即引入该设置前的行为；
                缩短时长会立刻撤销已到期条目，prod 预登记命令因此可能当场回到「未预授权」状态。
              </span>
            </div>
            <select
              v-model.number="ttlDraft"
              class="setting-select"
              :disabled="policySaving"
              @change="onTtlChange"
            >
              <option v-for="o in ttlOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              <option v-if="!ttlOptions.some((o) => o.value === allowlistTtl)" :value="allowlistTtl">
                自定义（{{ allowlistTtl }} 小时）
              </option>
            </select>
          </div>

          <div class="ops-toggle-row">
            <div class="ops-info">
              <span class="runtime-label">自动审批</span>
              <span class="runtime-desc">
                「自动审批」档下弹卡前先让模型代审一道；关闭后模型不再代审，
                所有需授权命令逐条弹卡问人（手动审批语义）。环境变量总闸
                <code>CKUN_AGENT_DISABLE_AUTO_APPROVE=1</code> 压过本开关。
                hardline 阻断、prod 预授权、用户显式拒绝不受本开关影响。
              </span>
            </div>
            <ToggleSwitch
              :model-value="autoApprove"
              :disabled="policySaving"
              @update:model-value="onAutoApproveToggle"
            />
          </div>

          <div class="ops-toggle-row">
            <div class="ops-info">
              <span class="runtime-label">审批兜底等待</span>
              <span class="runtime-desc">
                审批卡无人响应多久后标记「已超时」（待办保留、不按拒绝处理，
                重新确认仍可放行）。选「永久等待」则卡片永不标记超时。
              </span>
            </div>
            <select
              v-model.number="fallbackDraft"
              class="setting-select"
              :disabled="policySaving"
              @change="onFallbackChange"
            >
              <option v-for="o in fallbackOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              <option v-if="!fallbackOptions.some((o) => o.value === approvalFallback)" :value="approvalFallback">
                自定义（{{ approvalFallback }} 秒）
              </option>
            </select>
          </div>

          <div class="ops-toggle-row">
            <div class="ops-info">
              <span class="runtime-label">新建连接默认环境</span>
              <span class="runtime-desc">
                只决定「添加连接」表单的初值，仍可逐条改；编辑已有连接时保留它原本的标记，不会被这里重写
              </span>
            </div>
            <select v-model="opsCfg.defaultEnvironment" class="setting-select">
              <option value="test">测试 (test)</option>
              <option value="prod">生产 (prod)</option>
            </select>
          </div>

          <div class="ops-toggle-row">
            <div class="ops-info">
              <span class="runtime-label">生产环境额外确认</span>
              <span class="runtime-desc">
                关闭后不再为 prod 目标多问一句：审批卡片的生产警示条、批量下发中「仅因目标是 prod」的确认框都会去掉。
                dangerous 逐条审批、prod 危险命令必须预登记 allowlist、hardline 无条件阻断 —— 三条都不受此开关影响
              </span>
            </div>
            <ToggleSwitch v-model="opsCfg.prodConfirm" />
          </div>
        </div>
      </section>

      <!-- ── 工具开关 ── -->
      <section class="grp">
        <button class="grp-head" @click="open.tools = !open.tools">
          <span class="grp-caret" :class="{ open: open.tools }">▸</span>
          <span class="grp-title">工具开关</span>
          <span class="grp-badge">{{ enabledToolCount }}/{{ tools.length }} 启用</span>
        </button>
        <div v-show="open.tools" class="grp-body">
          <p v-if="toolsLoading" class="hint">加载中…</p>
          <p v-else-if="toolsError" class="hint err">
            {{ toolsError }}
            <button class="link-btn" @click="loadTools({ notify: true })">重试</button>
          </p>
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
          <span class="grp-badge">{{ permSummary }}</span>
        </button>
        <div v-show="open.perm" class="grp-body">
          <p class="hint">
            审批弹窗点"始终允许"后写入此白名单（持久化，进程重启仍生效{{ allowlistTtl ? `，但按「运维策略 → 授权保留时长」${ttlText(allowlistTtl)}后自动失效` : '' }}）。撤销立即生效。
            <button class="link-btn" @click="loadPermissions({ notify: true })">刷新</button>
          </p>
          <div class="allow-row">
            <input v-model.trim="allowTool" class="text-input" placeholder="工具名（如 shell_exec）" list="perm-tool-names" />
            <datalist id="perm-tool-names">
              <option v-for="t in tools" :key="t.name" :value="t.name" />
            </datalist>
            <input v-model.trim="allowCommand" class="text-input wide" placeholder="绑定命令（可选；省略 = 工具级授权）" />
            <button class="btn btn-primary" :disabled="!allowTool || allowing" @click="onAllow">
              {{ allowing ? '登记中…' : '手动授权' }}
            </button>
          </div>
          <p class="hint muted">命令工具建议填绑定命令：授权键是命令哈希绑定（改命令即失效）；hardline 底线命令不受白名单放行。</p>
          <p v-if="permError" class="hint err">
            {{ permError }}：下面这份清单不代表库里有什么。
            <button class="link-btn" @click="loadPermissions({ notify: true })">重试</button>
          </p>
          <p v-else-if="!permTools.length" class="hint muted">暂无永久授权</p>
          <div v-for="p in permTools" :key="p.tool" class="perm-row">
            <div class="tool-info">
              <span class="tool-name">{{ p.tool }}</span>
              <span class="tool-desc">{{ p.keys.length }} 条授权键（{{ p.keys.map(k => k.split(':')[1] || '工具级').slice(0, 3).join('、') }}{{ p.keys.length > 3 ? ' …' : '' }}）</span>
            </div>
            <button class="btn btn-danger" :disabled="revoking === p.tool" @click="onRevoke(p.tool, p.keys.length)">
              {{ revoking === p.tool ? '撤销中…' : '全部撤销' }}
            </button>
          </div>
        </div>
      </section>

      <!-- ── Web 搜索密钥 ── -->
      <section class="grp">
        <button class="grp-head" @click="open.webtools = !open.webtools">
          <span class="grp-caret" :class="{ open: open.webtools }">▸</span>
          <span class="grp-title">Web 搜索密钥</span>
          <span class="grp-badge" :class="{ live: webTools.tavily_api_key_set }">
            {{ tavilyBadge }}
          </span>
        </button>
        <div v-show="open.webtools" class="grp-body">
          <p class="hint">
            web_search 工具优先走 Tavily（结构化摘要 + 答案），未配置密钥时自动降级
            360/DuckDuckGo 免费搜索。环境变量 <code>TAVILY_API_KEY</code> 优先于此处的本地存储。
          </p>
          <p v-if="webToolsError" class="hint err">
            {{ webToolsError }}：上面那枚「未配置」不代表服务端的状态。
            <button class="link-btn" @click="loadWebTools({ notify: true })">重试</button>
          </p>
          <div class="allow-row">
            <input
              v-model.trim="tavilyInput"
              class="text-input wide"
              type="password"
              :placeholder="webTools.tavily_api_key_set ? `已设置：${webTools.tavily_api_key_masked}（留空不改）` : 'tvly-…'"
              autocomplete="off"
            />
            <button class="btn" :disabled="tavilySaving || !tavilyInput" @click="onSaveTavily">保存</button>
            <button v-if="webTools.tavily_api_key_set && webTools.tavily_api_key_source === 'db'" class="btn btn-danger" :disabled="tavilySaving" @click="onClearTavily">清除</button>
          </div>
        </div>
      </section>

      <!-- ── 策略与演化 ── -->
      <section class="grp">
        <button class="grp-head" @click="open.strat = !open.strat">
          <span class="grp-caret" :class="{ open: open.strat }">▸</span>
          <span class="grp-title">策略与自演化</span>
          <span class="grp-badge" :class="{ live: tickStatus?.running }">
            {{ tickStatus ? (tickStatus.running ? '意识循环运行中' : '意识循环已停')
              : (tickError ? '循环状态未读取' : '加载中…') }}
          </span>
        </button>
        <div v-show="open.strat" class="grp-body">
          <p class="hint">
            Agent 每次运行后自动反思并演化配置策略，<b>下一轮运行即刻读回生效</b>（当前立场：自动应用 + 全程可见）。
            这里可逐条停用或淘汰不再合理的演化，无需重启。
            <button class="link-btn" @click="loadStrategies({ notify: true })">刷新</button>
          </p>
          <p v-if="stratError" class="hint err">
            {{ stratError }}
            <button class="link-btn" @click="loadStrategies({ notify: true })">重试</button>
          </p>
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
            <button class="btn btn-danger btn-sm" @click="onDeleteStrategy(s)">淘汰</button>
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
  api,
  toFriendlyMessage,
  type AgentToolInfo,
  type AgentStrategyItem,
  type TickStatusResponse,
} from '@/api/client'
import { useToastStore } from '@/stores/toast'
import { parseBackendTs } from '@/utils/format'
import { useSettingsStore, BUDGET_LIMITS } from '@/stores/settings'
import { useConfirm } from '@/composables/useConfirm'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'
import SettingSlider from '@/components/settings/fields/SettingSlider.vue'

const toast = useToastStore()
const settingsStore = useSettingsStore()
const { confirm } = useConfirm()

const open = reactive({ budget: false, runtime: true, ops: true, tools: true, perm: false, strat: false, webtools: false })

// ── 运行预算（写本地设置，ssh-ai store 在下次运行时读取并随请求传给后端） ──
const budget = computed(() => settingsStore.settings.agent)

// A3/A4/E1：迭代步数 + 动态预算 + 意图引导。同 budget，写本地设置即生效
const runtimeCfg = computed(() => settingsStore.settings.agent)
const runtimeSummary = computed(() => {
  const a = runtimeCfg.value
  const parts = [`≤${a.maxIterations} 步`]
  if (a.dynamicBudget) parts.push('动态')
  if (a.enableIntentionGuide) parts.push('意图引导')
  return parts.join(' · ')
})

// E3/E4/E5：运维策略。defaultEnvironment/prodConfirm 是本地设置（写 localStorage 即刻生效），
// allowlist TTL 落在后端 gate，单独走 API + 失败回滚。
const opsCfg = computed(() => settingsStore.settings.agent)
const ttlOptions: { value: number; label: string }[] = [
  { value: 0, label: '永久（不过期）' },
  { value: 24, label: '24 小时' },
  { value: 168, label: '7 天' },
  { value: 720, label: '30 天' },
]
// allowlistTtl = 与后端一致的基线；ttlDraft = 选择框当前值（保存失败时回滚到基线）
const allowlistTtl = ref(0)
const ttlDraft = ref(0)
/**
 * 后端策略是否真读到过。没读到过的时候 allowlistTtl/autoApprove/approvalFallback
 * 三个基线只是本地硬编码的默认值 —— 把它们印在头栏上就等于宣称"库里就是这个"。
 * 一条会消失的 toast 不是状态，这里要的是一个能一直看见的说法。
 */
const policyLoaded = ref(false)
// 审批策略新两字段：同款「基线 + 草稿 + 失败回滚」。autoApprove=false 时
// auto 档不再问模型、逐条弹卡（env kill switch 在后端仍压过这里的开关）。
const approvalFallback = ref(600)
const fallbackDraft = ref(600)
const autoApprove = ref(true)
const autoApproveBaseline = ref(true)
/**
 * 三项写操作（TTL / 自动审批 / 兜底等待）共用这一个「在途」标记，不是偷懒：
 * 每次 PUT 的回包是整行权威值，面板拿它收敛。两条并发 PUT 的响应乱序到达时，
 * 后到的那份可能是"前一次改动提交之前"读到的整行 —— 于是库里已是新值、面板显示旧值。
 * 一次只放行一条写，这条路径就不存在了（原实现 TTL 用独立的 ttlSaving，正好留出这个缝）。
 */
const policySaving = ref(false)
const fallbackOptions: { value: number; label: string }[] = [
  { value: 0, label: '永久等待（不标记超时）' },
  { value: 600, label: '10 分钟（默认）' },
  { value: 1800, label: '30 分钟' },
  { value: 3600, label: '1 小时' },
  { value: 86400, label: '1 天' },
]

function ttlText(hours: number): string {
  const hit = ttlOptions.find((o) => o.value === hours)
  if (hit) return hit.label.replace('（不过期）', '')
  return `${hours} 小时`
}

function syncPolicyFields(data: { auto_approve_enabled: boolean; approval_fallback_s: number }) {
  autoApprove.value = data.auto_approve_enabled
  autoApproveBaseline.value = data.auto_approve_enabled
  approvalFallback.value = Number.isFinite(data.approval_fallback_s) ? data.approval_fallback_s : 600
  fallbackDraft.value = approvalFallback.value
}

/**
 * @param notify 用户显式点「重试」时才弹 toast。挂载时那五路并发都失败会各弹一条，
 *   后端没就绪时一次开设置页能叠五条 toast —— 挂载路径改走一条汇总（见 onMounted）。
 */
async function loadPermissionPolicy(opts: { notify?: boolean } = {}): Promise<boolean> {
  try {
    const data = await api.getPermissionPolicy()
    const hours = Number.isFinite(data.allowlist_ttl_hours) ? data.allowlist_ttl_hours : 0
    allowlistTtl.value = hours
    ttlDraft.value = hours
    syncPolicyFields(data)
    policyLoaded.value = true
    return true
  } catch (e) {
    policyLoaded.value = false
    if (opts.notify) toast.error(`授权策略加载失败：${toFriendlyMessage(e)}`)
    return false
  }
}

async function onTtlChange() {
  const next = ttlDraft.value
  if (!Number.isFinite(next) || next === allowlistTtl.value) return
  policySaving.value = true
  try {
    const data = await api.updatePermissionPolicy({ allowlist_ttl_hours: next })
    allowlistTtl.value = Number.isFinite(data.allowlist_ttl_hours) ? data.allowlist_ttl_hours : next
    ttlDraft.value = allowlistTtl.value
    syncPolicyFields(data)
    toast.success(allowlistTtl.value
      ? `永久授权将在 ${ttlText(allowlistTtl.value)}后失效，已到期条目即刻撤销`
      : '永久授权已恢复为不过期')
    // 缩短时长可能当场清掉若干预登记条目，列表必须跟着刷新而不是留着旧行
    await loadPermissions()
  } catch (e) {
    ttlDraft.value = allowlistTtl.value
    toast.error(`授权时长保存失败：${toFriendlyMessage(e)}`)
  } finally {
    policySaving.value = false
  }
}

async function onFallbackChange() {
  const next = fallbackDraft.value
  if (!Number.isFinite(next) || next === approvalFallback.value) return
  policySaving.value = true
  try {
    const data = await api.updatePermissionPolicy({ approval_fallback_s: next })
    syncPolicyFields(data)
    toast.success(data.approval_fallback_s
      ? `审批卡片 ${data.approval_fallback_s}s 无响应后标记过期，待办保留`
      : '审批卡片不再标记超时（永久等待）')
  } catch (e) {
    fallbackDraft.value = approvalFallback.value
    toast.error(`审批兜底时长保存失败：${toFriendlyMessage(e)}`)
  } finally {
    policySaving.value = false
  }
}

async function onAutoApproveToggle(v: boolean) {
  // 先乐观翻转再落库，失败回滚到基线 —— ToggleSwitch 没有异步 slot，
  // 等 PUT 返回再改 model-value 会让按钮看起来点了没反应。
  autoApprove.value = v
  policySaving.value = true
  try {
    const data = await api.updatePermissionPolicy({ auto_approve_enabled: v })
    syncPolicyFields(data)
    toast.success(data.auto_approve_enabled
      ? '自动审批已开启：auto 档弹卡前先问模型'
      : '自动审批已关闭：auto 档逐条弹卡，不再问模型')
  } catch (e) {
    autoApprove.value = autoApproveBaseline.value
    toast.error(`自动审批开关保存失败：${toFriendlyMessage(e)}`)
  } finally {
    policySaving.value = false
  }
}

const opsSummary = computed(() => {
  const a = opsCfg.value
  const parts: string[] = []
  // 读失败时不许把本地硬编码的基线（0 = 永久、autoApprove = 开）当成服务端的确定值报头：
  // 那正是"面板写着什么、库里跑什么"两回事的来源，而唯一线索是一条会消失的 toast。
  if (!policyLoaded.value) parts.push('授权策略未读取')
  else parts.push(`授权 ${ttlText(allowlistTtl.value)}`, autoApprove.value ? '自动审批开' : '自动审批关')
  parts.push(`新连接 ${a.defaultEnvironment === 'prod' ? 'prod' : 'test'}`)
  parts.push(a.prodConfirm ? 'prod 确认开' : 'prod 确认关')
  return parts.join(' · ')
})

// 后端 /agent/run 对预算字段校验为 ge=0、token 为 int：清空（v-model.number 得到 ''）
// 或输入负数/小数原样发出，会让整次运行以 422 失败 —— 用户只看到"跑不起来"。
// 落库前归一化：清空/非法 = 0（= 用后端默认预算）。
// R4：归零本身是"改了没生效"，必须让用户看到 —— 只静默折 0，budgetSummary
// 又显示"后端默认"，用户以为保存了 -5 或 1e9 实际被吞。toast + 边框双通道。
//
// 上界与 store 的 asNumber 闸门共用 BUDGET_LIMITS（同一份，见 settings.ts）：
// 面板原来只管 lo，于是 $50 能填进去、下次启动被闸门静默折回 0 —— 用户以为自己
// 设过预算。越界同样走"toast + 红框"这条已建成的通道，不新开一套说法。
// 红框判据与 normalizeBudget 共用这一对 computed：两处各写一遍区间就是第二份事实源，
// 上一轮它只写了下界（<0），于是越上界的值框不红、却会被闸门静默折成 0。
const budgetCostInvalid = computed(() => {
  const v = budget.value.maxCostUsd
  return !Number.isFinite(v) || v < BUDGET_LIMITS.maxCostUsd.min || v > BUDGET_LIMITS.maxCostUsd.max
})
const budgetTokenInvalid = computed(() => {
  const v = budget.value.maxTotalTokens
  return !Number.isFinite(v) || v < BUDGET_LIMITS.maxTotalTokens.min
    || v > BUDGET_LIMITS.maxTotalTokens.max || !Number.isInteger(Number(v))
})

function normalizeBudget() {
  const a = settingsStore.settings.agent
  const rawCost = a.maxCostUsd
  const rawTok = a.maxTotalTokens
  const badCost = budgetCostInvalid.value
  const badTok = budgetTokenInvalid.value
  const clamp = (v: unknown, lo: number, hi: number) =>
    Number.isFinite(v) ? Math.min(hi, Math.max(lo, v as number)) : lo
  a.maxCostUsd = clamp(rawCost, BUDGET_LIMITS.maxCostUsd.min, BUDGET_LIMITS.maxCostUsd.max)
  a.maxTotalTokens = Math.floor(clamp(rawTok, BUDGET_LIMITS.maxTotalTokens.min, BUDGET_LIMITS.maxTotalTokens.max))
  if (badCost || badTok) {
    toast.warning('预算输入超出可保存的范围，已回退到最近的合法值（0 = 后端默认）', 4000)
  }
}
const budgetSummary = computed(() => {
  const { maxCostUsd, maxTotalTokens, breakerDisabled } = budget.value
  // 勾选关闭时，两个数字框里的值不再随请求发出，徽标若仍显示它们就是撒谎。
  if (breakerDisabled) return '熔断已关'
  const parts: string[] = []
  if (maxCostUsd > 0) parts.push(`$${maxCostUsd}`)
  if (maxTotalTokens > 0) parts.push(`${(maxTotalTokens / 1000).toFixed(0)}K tok`)
  return parts.length ? parts.join(' / ') : '后端默认'
})

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

async function loadTools(opts: { notify?: boolean } = {}): Promise<boolean> {
  toolsLoading.value = true
  toolsError.value = ''
  try {
    const data = await agentAdminApi.getTools()
    tools.value = data.tools
    return true
  } catch (e) {
    toolsError.value = toFriendlyMessage(e, '工具列表加载失败')
    if (opts.notify) toast.error(toolsError.value)
    return false
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
// 与 policyLoaded 同一件事的另一半：读失败时 permKeys 是空数组，而空数组既可能是
// "库里确实没有永久授权"，也可能是"根本没读到"。先前只有后者会发生的 toast（一条会
// 消失的提示），头栏徽标却照旧写着「0 个工具已永久授权」。
const permLoaded = ref(false)
const permError = ref('')
const permTools = computed(() => {
  const map = new Map<string, string[]>()
  for (const key of permKeys.value) {
    const tool = key.split(':')[0]
    if (!map.has(tool)) map.set(tool, [])
    map.get(tool)!.push(key)
  }
  return [...map.entries()].map(([tool, keys]) => ({ tool, keys }))
})

async function loadPermissions(opts: { notify?: boolean } = {}): Promise<boolean> {
  permError.value = ''
  try {
    const data = await agentAdminApi.listPermissions()
    permKeys.value = data.tools
    permLoaded.value = true
    return true
  } catch (e) {
    permError.value = toFriendlyMessage(e, '授权列表加载失败')
    permLoaded.value = false
    if (opts.notify) toast.error(`授权列表加载失败：${permError.value}`)
    return false
  }
}

const permSummary = computed(() => [
  permLoaded.value ? `${permTools.value.length} 个工具已永久授权` : '授权列表未读取',
  policyLoaded.value ? ttlText(allowlistTtl.value) : '授权时长未读取',
].join(' · '))

async function onRevoke(tool: string, keyCount: number) {
  const ok = await confirm({
    title: '撤销永久授权',
    message: `撤销「${tool}」的全部 ${keyCount} 条永久授权键？此后该工具每条命令都需重新审批。`,
    confirmText: '全部撤销',
    danger: true,
  })
  if (!ok) return
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

const allowTool = ref('')
const allowCommand = ref('')
const allowing = ref(false)

async function onAllow() {
  // 与后端 AllowPermissionRequest 的 Field 区间同口径（tool 1..64 / command ≤4096）。
  // 提交前自己拦：不这么做的话，要么 pydantic 原文进 toast，要么给输入框加
  // maxlength 静默截断 —— 授权键 = sha256(命令)，截断后的命令与实际执行的对不上，
  // 授权永不命中，用户只会反复被审批弹窗骚扰且无从排查。
  if (!allowTool.value) {
    toast.error('请填写工具名')
    return
  }
  if (allowTool.value.length > 64) {
    toast.error('工具名不能超过 64 字符')
    return
  }
  if (allowCommand.value.length > 4096) {
    toast.error('绑定命令不能超过 4096 字符')
    return
  }
  allowing.value = true
  try {
    await agentAdminApi.allowPermission(allowTool.value, allowCommand.value || undefined)
    toast.success(`已登记「${allowTool.value}」永久授权`)
    allowTool.value = ''
    allowCommand.value = ''
    await loadPermissions()
  } catch (e) {
    toast.error(`授权登记失败：${toFriendlyMessage(e)}`)
  } finally {
    allowing.value = false
  }
}

// ── Tavily 密钥 ──
const webTools = reactive({
  tavily_api_key_set: false,
  tavily_api_key_masked: '',
  tavily_api_key_source: '' as 'env' | 'db' | '',
})
const tavilyInput = ref('')
const tavilySaving = ref(false)
// 徽标先前只有「已配置 / 未配置」两档：GET 失败时 tavily_api_key_set 停在本地默认
// false，于是面板把"没读到"报成"确认没配"，用户会去重设一个本来就存在的密钥。
const webToolsError = ref('')
const tavilyBadge = computed(() => {
  if (webToolsError.value) return '密钥状态未读取'
  return webTools.tavily_api_key_set
    ? `已配置（${webTools.tavily_api_key_source === 'env' ? '环境变量' : '本地存储'}）`
    : '未配置'
})

function applyWebTools(data: Partial<typeof webTools>) {
  Object.assign(webTools, data)
  // 写回执同样是权威状态：保存成功后还挂着"未读取"的徽标就成了假话
  webToolsError.value = ''
}

async function loadWebTools(opts: { notify?: boolean } = {}): Promise<boolean> {
  webToolsError.value = ''
  try {
    applyWebTools(await api.getWebToolsSettings())
    return true
  } catch (e) {
    webToolsError.value = toFriendlyMessage(e, 'Web 工具设置加载失败')
    if (opts.notify) toast.error(`Web 工具设置加载失败：${webToolsError.value}`)
    return false
  }
}

async function onSaveTavily() {
  if (!tavilyInput.value) return
  tavilySaving.value = true
  try {
    applyWebTools(await api.updateWebToolsSettings({ tavily_api_key: tavilyInput.value }))
    tavilyInput.value = ''
    toast.success('Tavily 密钥已保存')
  } catch (e) {
    toast.error(`保存失败：${toFriendlyMessage(e)}`)
  } finally {
    tavilySaving.value = false
  }
}

async function onClearTavily() {
  tavilySaving.value = true
  try {
    applyWebTools(await api.updateWebToolsSettings({ tavily_api_key: '' }))
    toast.success('已清除本地存储的 Tavily 密钥')
  } catch (e) {
    toast.error(`清除失败：${toFriendlyMessage(e)}`)
  } finally {
    tavilySaving.value = false
  }
}

// ── 策略与演化 ──
const strategies = ref<AgentStrategyItem[]>([])
const stratError = ref('')
const tickStatus = ref<TickStatusResponse | null>(null)
// tick 状态读失败与"还没读到"是两回事：先前 null 只被当作加载中，徽标就永远停在
// 「加载中…」，而那次 GET 已经失败过了 —— 徽标是唯一报这个状态的地方，没有别的线索。
const tickError = ref(false)

/** @returns 策略列表与循环状态是否都读到了（notify 的口径见 loadPermissionPolicy） */
async function loadStrategies(opts: { notify?: boolean } = {}): Promise<boolean> {
  stratError.value = ''
  tickError.value = false
  // 分开收：策略列表读到了，不该因为循环状态那次 GET 抖动而一起看不见
  const [sRes, tRes] = await Promise.allSettled([
    agentAdminApi.listStrategies({ mode: 'ssh' }),
    agentAdminApi.getTickStatus(),
  ])
  if (sRes.status === 'fulfilled') strategies.value = sRes.value.strategies
  else {
    stratError.value = toFriendlyMessage(sRes.reason, '策略列表加载失败')
    if (opts.notify) toast.error(stratError.value)
  }
  if (tRes.status === 'fulfilled') tickStatus.value = tRes.value
  else {
    tickStatus.value = null
    tickError.value = true
  }
  return sRes.status === 'fulfilled' && tRes.status === 'fulfilled'
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
  const ok = await confirm({
    title: '淘汰演化策略',
    message: `删除策略「${s.name}」？该条为库里唯一副本，删除不可恢复。`,
    confirmText: '淘汰',
    danger: true,
  })
  if (!ok) return
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
    return parseBackendTs(iso).toLocaleDateString('zh-CN')
  } catch {
    return iso
  }
}

// 挂载时的五路读取各有自己的内联提示位，但其中三组（审批授权 / Web 密钥 / 策略演化）
// 默认折叠 —— 折叠时内联提示看不见，所以失败仍要走 toast。原实现每路各弹一条，
// 后端没就绪时打开这页会一次叠出五条同样的"加载失败"。
const MOUNT_LOADS: { label: string; run: () => Promise<boolean> }[] = [
  { label: '工具开关', run: loadTools },
  { label: '授权列表', run: loadPermissions },
  { label: '授权策略', run: loadPermissionPolicy },
  { label: '演化策略', run: loadStrategies },
  { label: 'Web 搜索密钥', run: loadWebTools },
]

onMounted(async () => {
  const results = await Promise.all(
    MOUNT_LOADS.map(async (l) => ({ label: l.label, ok: await l.run() })),
  )
  const failed = results.filter((r) => !r.ok).map((r) => r.label)
  if (failed.length) {
    toast.error(
      `未能读取服务端的 ${failed.length} 项设置：${failed.join('、')}。`
      + '这些分组现在显示的是本地默认值，展开对应分组可重试',
      6000,
    )
  }
})
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped src="./settings-groups.css"></style>
<style scoped>
.grp-badge.live { color: var(--accent-primary); }

.risk-group { margin-bottom: 10px; }
.risk-label {
  font-size: 11px; color: var(--text-tertiary); margin: 6px 0 4px;
  text-transform: none; font-weight: 600;
}
.risk-label.dangerous { color: var(--status-error-text); }
.risk-label.moderate { color: var(--status-warn-text); }

.tool-row, .perm-row, .strat-row {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 4px; border-top: 1px solid var(--border-subtle);
}
.strat-row.off { opacity: .55; }
.tool-name { font-size: 12.5px; font-family: var(--font-mono); color: var(--text-primary); }
.strat-name { font-size: 12.5px; font-weight: 600; color: var(--text-primary); }
.tool-desc {
  font-size: 11px; color: var(--text-secondary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tool-flags { display: flex; gap: 4px; flex-shrink: 0; }
.flag.info { color: var(--status-info-text); border-color: color-mix(in srgb, var(--status-info-text) 35%, transparent); }
.strat-meta { display: flex; gap: 5px; margin-top: 3px; }

.budget-row { display: flex; gap: 16px; margin: 8px 0 4px; }
.budget-field { display: flex; flex-direction: column; gap: 4px; }
.budget-label { font-size: 11.5px; color: var(--text-secondary); }
.num-input, .text-input {
  height: 26px; padding: 0 8px; border-radius: 4px;
  background: var(--bg-input); border: 1px solid var(--border-default);
  color: var(--text-primary); font-size: 12px; font-family: inherit;
}
.num-input { width: 120px; }
.num-input.input-error { border-color: var(--status-error-text); }
.runtime-row { margin: 8px 0; }
.runtime-field { display: block; }
.runtime-label { font-size: 12px; color: var(--text-primary); font-weight: 500; }
.runtime-desc { display: block; font-size: 11.5px; color: var(--text-secondary); margin-top: 2px; }
.runtime-toggle-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 8px 0; border-top: 1px solid var(--border-subtle); margin-top: 8px;
}
.runtime-toggle-info { display: flex; flex-direction: column; gap: 2px; }
.ops-toggle-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 8px 0; border-top: 1px solid var(--border-subtle);
}
.ops-toggle-row:first-of-type { border-top: none; }
.ops-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.setting-select { flex-shrink: 0; }
.setting-select:disabled { opacity: .5; cursor: default; }
.slider-inline { margin-top: 6px; }
.slider-inline :deep(.setting-range) { flex: 1; max-width: 320px; }
.allow-row { display: flex; gap: 8px; align-items: center; margin: 8px 0 4px; }
.text-input { flex: 1; min-width: 0; }
.text-input.wide { flex: 2; }
</style>
