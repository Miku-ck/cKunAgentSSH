<template>
  <div class="panel">
    <header class="panel-header">
      <h3>运维</h3>
      <p class="panel-desc">出站代理、上游超时与重试、后端日志级别和诊断快照 —— 这一页的值只存在后端，改完即时生效，不需要重启</p>
    </header>

    <div class="panel-body">
      <!-- loading 此前在 composable 里维护却没有任何消费方：后端冷启动那几秒里
           这一页是"什么都没发生"的空态，与"读失败"（有红条）和"读到了"（有值）
           两种状态都分不清。给它一个出口，读失败时它已被置回 false。 -->
      <p v-if="loading" class="hint">正在读取运维设置…</p>
      <p v-if="loadError" class="panel-error">{{ loadError }}</p>

      <section class="section">
        <h4 class="section-title">出站网络</h4>

        <SettingRow label="出站代理" desc="作用于模型调用、embedding 与网络搜索；web_fetch 按已校验的 IP 直连，刻意不走代理">
          <span class="state-text">
            <template v-if="proxySet">
              <code class="mono">{{ proxyMasked }}</code>
              <span class="badge" :class="proxySource === 'env' ? 'badge-warn' : 'badge-ok'">
                {{ proxySource === 'env' ? '来自环境变量' : '已设置' }}
              </span>
            </template>
            <span v-else class="muted">直连（未配置）</span>
          </span>
        </SettingRow>

        <div class="inline-form">
          <input
            v-model="proxyDraft"
            class="form-input grow"
            type="text"
            spellcheck="false"
            placeholder="http://user:pass@proxy:3128 或 socks5h://127.0.0.1:1080"
            :disabled="savingProxy"
            @keydown.enter.prevent="saveProxy()"
          />
          <button class="btn" :disabled="savingProxy" @click="saveProxy()">应用代理</button>
          <button class="btn btn-ghost" :disabled="!proxySet || savingProxy" @click="clearProxy()">清除</button>
        </div>
        <p class="hint">
          代理地址只单向提交，页面读回的是脱敏值（不含口令）。保存后已建立的模型连接会按新策略重建；
          库里有值时它压过环境变量 CKUN_HTTP_PROXY。「清除」是把库里那行留空——
          若部署注入了该变量，重启后会重新走它，要彻底直连得两边都清。
        </p>

        <SettingRow label="上游请求超时" desc="0 = 各端点沿用自己的默认（模型读 300s、连接测试 10s）；非 0 统一按此值，连接阶段仍不超过 10s">
          <div class="num-row">
            <input
              v-model="timeoutText"
              class="form-input num-input"
              :class="{ 'input-error': timeoutInvalid }"
              type="text"
              inputmode="numeric"
              :placeholder="String(TIMEOUT_MIN)"
            />
            <span class="unit">秒</span>
          </div>
        </SettingRow>

        <SettingRow label="失败重试次数" desc="只对 LLM 调用生效（走 openai SDK 的 max_retries，重传输层错误与 429/5xx）。embedding、网页抓取与「测试连接」用的是裸 httpx 客户端，不重试。">
          <select v-model.number="retryDraft" class="setting-select">
            <option v-for="n in retryOptions" :key="n" :value="n">{{ n }} 次</option>
          </select>
        </SettingRow>

        <div class="row-actions">
          <button
            class="btn"
            :disabled="savingHttp || timeoutInvalid || !httpDirty"
            @click="saveHttp()"
          >{{ savingHttp ? '保存中…' : '保存超时与重试' }}</button>
          <span v-if="timeoutInvalid" class="hint error">请输入 {{ TIMEOUT_MIN }}–{{ TIMEOUT_MAX }} 之间的整数秒</span>
        </div>
      </section>

      <section class="section">
        <h4 class="section-title">后端日志</h4>
        <SettingRow label="日志级别" desc="默认 warning（引入本设置前的实际输出）；debug 非常吵，排查完建议调回">
          <select
            :value="logLevel"
            class="setting-select"
            :disabled="savingLevel"
            @change="onLevelChange"
          >
            <option v-for="lv in logLevels" :key="lv" :value="lv">{{ lv }}</option>
          </select>
        </SettingRow>
        <p class="hint">改的是后端进程内 root logger 的级别，即时生效且落库；uvicorn 自身的访问日志不受影响。</p>
      </section>

      <section class="section">
        <h4 class="section-title">桌面偏好</h4>
        <p v-if="prefsError" class="panel-error">{{ prefsError }}</p>
        <SettingRow label="开机自启" desc="登录后自动启动本应用；由系统登录项登记，Linux 下不支持">
          <ToggleSwitch
            :model-value="prefs.autoLaunch"
            :disabled="!prefsSupported || pendingKey === 'autoLaunch'"
            @update:model-value="togglePref('autoLaunch')"
          />
        </SettingRow>
        <SettingRow label="关窗最小化到托盘" desc="点关闭只隐藏窗口、进程继续跑；托盘图标右键可退出。需要 resources/icon.png 存在">
          <ToggleSwitch
            :model-value="prefs.minimizeToTray"
            :disabled="!prefsSupported || pendingKey === 'minimizeToTray'"
            @update:model-value="togglePref('minimizeToTray')"
          />
        </SettingRow>
        <SettingRow label="桌面通知" desc="窗口未聚焦时的系统通知：巡检任务结束、命令等待审批">
          <ToggleSwitch
            :model-value="prefs.desktopNotifications"
            :disabled="!prefsSupported || pendingKey === 'desktopNotifications'"
            @update:model-value="togglePref('desktopNotifications')"
          />
        </SettingRow>
        <p v-if="!prefsSupported" class="hint">桌面偏好仅在桌面应用内可改（浏览器模式下这些是主进程行为，这里没有可写的对端）。</p>
      </section>

      <section class="section">
        <h4 class="section-title">实时告警监控</h4>
        <p class="hint">
          告警诊断不再默认开启。必须先在此显式新建「监控任务」并指定主机，应用才会在该主机
          内存/CPU/磁盘持续越限时自动发起只读诊断；没有任何任务时，监控会话完全不会触发告警。
        </p>

        <div class="inline-form">
          <input
            v-model="newHost"
            class="form-input grow"
            type="text"
            spellcheck="false"
            placeholder="监控主机（需与监控会话中的主机标识一致，如 192.168.150.40）"
            @keydown.enter.prevent="createTask()"
          />
          <input
            v-model="newName"
            class="form-input"
            type="text"
            spellcheck="false"
            placeholder="名称（可选）"
            @keydown.enter.prevent="createTask()"
          />
          <button class="btn" :disabled="!canCreate" @click="createTask()">从模板新建</button>
        </div>
        <p v-if="hostExists" class="hint warn">该主机已有监控任务，请勿重复创建。</p>

        <div v-if="tasks.length" class="alert-task-list">
          <div v-for="task in tasks" :key="task.id" class="alert-task">
            <div class="alert-task-head">
              <span class="alert-task-name">{{ task.name }}</span>
              <ToggleSwitch
                :model-value="task.enabled"
                @update:model-value="(v) => toggle(task.id, v)"
              />
            </div>
            <div class="alert-task-meta mono">主机 {{ task.host }}</div>
            <div class="alert-task-rules">{{ rulesSummary(task) }}</div>
            <div class="alert-task-foot">
              <label class="cooldown-label">
                冷却
                <input
                  class="form-input num-input"
                  type="number"
                  min="1"
                  max="1440"
                  :value="task.cooldownMinutes"
                  @change="onCooldownInput(task, $event)"
                />
                分钟
              </label>
              <button class="btn btn-ghost btn-sm" @click="remove(task.id)">删除</button>
            </div>
          </div>
        </div>
        <p v-else class="hint">暂无告警监控任务。新建后才会对指定主机生效。</p>
      </section>

      <section class="section">
        <div class="diag-head">
          <h4 class="section-title">诊断快照</h4>
          <button class="btn btn-ghost" :disabled="diagLoading" @click="refreshDiagnostics()">
            {{ diagLoading ? '读取中…' : '刷新' }}
          </button>
        </div>
        <p v-if="diagError" class="panel-error">{{ diagError }}</p>
        <p v-else-if="!diagnostics" class="hint">
          显示后端实际连的库文件、各表行数、意识 tick 与 Bridge 连接状态。只读，密钥一律脱敏。
        </p>

        <dl v-else class="diag-grid">
          <template v-for="group in diagGroups" :key="group.title">
            <div class="diag-group-title">{{ group.title }}</div>
            <template v-for="item in group.items" :key="item.k">
              <dt class="diag-k">{{ item.k }}</dt>
              <dd class="diag-v" :class="{ mono: item.mono }">{{ item.v }}</dd>
            </template>
          </template>
        </dl>
      </section>

      <!-- 诊断包：与上面的"诊断快照"分工不同 —— 快照问的是后端（走 HTTP），
           这里收的是主进程自己的现场（userData 下的日志与崩溃转储），出问题时
           用户和我们都得有一份能直接看的文件。 -->
      <section class="section">
        <h4 class="section-title">诊断包</h4>
        <p class="hint">
          把主进程日志（各留尾部 4MB、凭据已抹）、最近的崩溃转储与 system.json 打成一个 tar.gz，
          落在应用数据目录的 diagnostics/ 里。只为现场排障生成，不会上传到任何地方。
        </p>
        <div class="row-actions">
          <button class="btn" :disabled="!bundleSupported || bundleExporting" @click="exportBundle()">
            {{ bundleExporting ? '导出中…' : '导出诊断包' }}
          </button>
          <button class="btn btn-ghost" :disabled="!bundleSupported" @click="revealBundle()">在文件夹中显示</button>
        </div>
        <p v-if="!bundleSupported" class="hint">浏览器模式下没有主进程日志与崩溃转储，诊断包只能在桌面应用内导出。</p>
        <p v-if="bundleError" class="panel-error">{{ bundleError }}</p>
        <p v-else-if="bundleResult?.ok" class="hint">
          已导出 <code class="mono">{{ bundleResult.path }}</code>
          （{{ formatBytes(bundleResult.bytes) }}，{{ bundleResult.logs.length }} 份日志，
          {{ bundleResult.crashDumps }} 份崩溃转储）
        </p>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * G4/G5/G8/G9/G10：运维面板。状态与保存动作全在 composable（可单测），
 * 面板只做渲染与展示派生，不直接碰 api / IPC。
 */
import { computed, onMounted, ref } from 'vue'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'
import {
  RETRY_MAX,
  TIMEOUT_MAX,
  TIMEOUT_MIN,
  useOpsSettings,
} from '@/composables/useOpsSettings'
import { useDesktopPrefs } from '@/composables/useDesktopPrefs'
import { useDiagnosticsBundle } from '@/composables/useDiagnosticsBundle'
import { useAlertMonitorTasks } from '@/composables/useAlertMonitorTasks'
import { describeRule, type AlertMonitorTask } from '@/utils/alert-trigger-core'
import type { LogLevel } from '@/api/client'
import { formatBytes } from '@/utils/format'

const {
  proxyDraft,
  proxyMasked,
  proxySet,
  proxySource,
  savingProxy,
  timeoutText,
  retryDraft,
  savingHttp,
  timeoutInvalid,
  httpDirty,
  logLevel,
  logLevels,
  savingLevel,
  diagnostics,
  diagLoading,
  diagError,
  loading,
  loadError,
  load,
  saveProxy,
  clearProxy,
  saveHttp,
  setLogLevel,
  refreshDiagnostics,
} = useOpsSettings()

const {
  prefs,
  supported: prefsSupported,
  pending: pendingKey,
  error: prefsError,
  load: loadPrefs,
  toggle: togglePref,
} = useDesktopPrefs()

// 命名用 bundle* 与上面的后端诊断快照（diagnostics / diagError）区分开：两者数据源不同
const {
  supported: bundleSupported,
  exporting: bundleExporting,
  result: bundleResult,
  error: bundleError,
  exportBundle,
  reveal: revealBundle,
} = useDiagnosticsBundle()

// 实时告警监控：从 useAlertMonitorTasks 共享同一份响应式任务列表（与 useAlertTrigger 同源）
const { tasks, addFromTemplate, update, toggle, remove } = useAlertMonitorTasks()
const newHost = ref('')
const newName = ref('')
const hostExists = computed(() => tasks.value.some(t => t.host === newHost.value.trim()))
const canCreate = computed(() => !!newHost.value.trim() && !hostExists.value)
function createTask() {
  if (!canCreate.value) return
  addFromTemplate(newHost.value.trim(), newName.value.trim() || undefined)
  newHost.value = ''
  newName.value = ''
}
function onCooldownInput(task: AlertMonitorTask, e: Event) {
  const n = Number((e.target as HTMLInputElement).value)
  if (Number.isFinite(n) && n > 0) update(task.id, { cooldownMinutes: Math.round(n) })
}
function rulesSummary(task: AlertMonitorTask): string {
  const parts = task.rules.filter(r => r.enabled).map(describeRule)
  return parts.length ? parts.join('，') : '（无启用规则）'
}

const retryOptions = Array.from({ length: RETRY_MAX + 1 }, (_, i) => i)

function onLevelChange(e: Event) {
  void setLogLevel((e.target as HTMLSelectElement).value as LogLevel)
}

// 「未知」是后端 stat 失败（size_bytes 为 null），与 0 字节的空库是两回事，必须保住这个区分；
// 数值→单位本身不再自己实现，交给 utils/format 的 formatBytes 单一源。
function bytes(n: number | null): string {
  return n === null || !Number.isFinite(n) ? '未知' : formatBytes(n)
}

const diagGroups = computed(() => {
  const d = diagnostics.value
  if (!d) return []
  return [
    {
      title: '后端',
      items: [
        { k: '版本', v: d.backend.version, mono: false },
        { k: 'Python', v: d.backend.python, mono: false },
        { k: '平台 / PID', v: `${d.backend.platform} / ${d.backend.pid}`, mono: false },
        { k: '日志级别', v: d.logging.level, mono: false },
      ],
    },
    {
      title: '数据库',
      items: [
        { k: '文件', v: d.database.path || '未知', mono: true },
        { k: '大小', v: bytes(d.database.size_bytes), mono: false },
        ...Object.entries(d.database.counts).map(([t, n]) => ({
          k: `${t} 行数`, v: String(n), mono: false,
        })),
      ],
    },
    {
      title: '运行状态',
      items: [
        { k: '意识 tick', v: d.tick.running ? '运行中' : '未启动', mono: false },
        { k: '最近 tick', v: d.tick.last_result ? JSON.stringify(d.tick.last_result) : '无', mono: false },
        {
          k: 'Bridge',
          v: d.bridge.present ? (d.bridge.connected ? '已连接' : '未连接') : '未创建',
          mono: false,
        },
        {
          k: '授权白名单',
          v: d.permissions.permanent_entries === null
            ? 'gate 未初始化'
            : `${d.permissions.permanent_entries} 条 · ${
                d.permissions.ttl_hours ? `${d.permissions.ttl_hours}h 后失效` : '不过期'}`,
          mono: false,
        },
        { k: '出站代理', v: d.http.proxy || '直连', mono: true },
        {
          k: '超时 / 重试',
          v: `${d.http.timeout_seconds ? `${d.http.timeout_seconds}s` : '各端点默认'} / ${d.http.retry_count} 次`,
          mono: false,
        },
      ],
    },
    {
      title: '设置项（脱敏）',
      items: Object.entries(d.app_settings).map(([k, v]) => ({ k, v, mono: false })),
    },
  ]
})

onMounted(() => {
  void load()
  void loadPrefs()
})
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
/* 面板骨架与 .setting-select 见 settings-panel-base.css */

.section { margin-bottom: var(--spacing-24); }
.section-title {
  margin: 0 0 var(--spacing-6);
  font-size: 0.9231rem;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: .04em;
}
.state-text { display: flex; align-items: center; gap: var(--spacing-8); font-size: 0.9231rem; color: var(--text-primary); }
.muted { color: var(--text-tertiary); }
.mono { font-family: var(--font-mono); font-size: 0.8846rem; }
.badge {
  padding: 1px var(--spacing-6);
  border-radius: var(--radius-pill);
  font-size: 0.8462rem;
  border: 1px solid var(--border-subtle);
}
.badge-ok { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent-primary) 35%, transparent); }
.badge-warn { color: var(--status-warn-text); border-color: color-mix(in srgb, var(--status-warn-text) 40%, transparent); }
.inline-form { display: flex; align-items: center; gap: var(--spacing-8); margin: var(--spacing-4) 0 var(--spacing-6); }
.grow { flex: 1; min-width: 0; }
.num-row { display: flex; align-items: center; gap: var(--spacing-6); }
.num-input { width: 88px; }
.unit { font-size: 0.9231rem; color: var(--text-secondary); }
.row-actions { display: flex; align-items: center; gap: var(--spacing-10); margin-top: var(--spacing-8); }
.hint { margin: var(--spacing-4) 0 0; font-size: 0.8846rem; color: var(--text-tertiary); line-height: 1.55; }
.hint.error { color: var(--status-error-text); }
.panel-error {
  margin: 0 0 var(--spacing-10);
  padding: var(--spacing-6) var(--spacing-10);
  font-size: 0.9231rem;
  color: var(--status-error-text);
  background: color-mix(in srgb, var(--status-error-text) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--status-error-text) 25%, transparent);
  border-radius: var(--radius-sm);
}
.form-input {
  height: 28px;
  padding: 0 var(--spacing-8);
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-family: inherit;
  outline: none;
}
.form-input:focus { border-color: var(--accent-primary); }
.form-input.input-error { border-color: var(--status-error-text); }
.diag-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--spacing-6); }
.diag-grid {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: var(--spacing-2) var(--spacing-12);
  margin: var(--spacing-8) 0 0;
  font-size: 0.8846rem;
}
.diag-group-title {
  grid-column: 1 / -1;
  margin-top: var(--spacing-8);
  color: var(--text-secondary);
  font-weight: 600;
  text-align: left;
}
.diag-k { color: var(--text-tertiary); text-align: left; }
.diag-v { margin: 0; color: var(--text-primary); word-break: break-all; text-align: left; }

/* 实时告警监控任务 */
.hint.warn { color: var(--status-warn-text); }
.alert-task-list { display: flex; flex-direction: column; gap: var(--spacing-10); margin-top: var(--spacing-10); }
.alert-task { padding: var(--spacing-10); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); }
.alert-task-head { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-8); }
.alert-task-name { font-weight: 600; color: var(--text-primary); font-size: 0.9231rem; }
.alert-task-meta { margin-top: var(--spacing-4); font-size: 0.8462rem; color: var(--text-tertiary); }
.alert-task-rules { margin-top: var(--spacing-4); font-size: 0.8846rem; color: var(--text-secondary); line-height: 1.5; }
.alert-task-foot { display: flex; align-items: center; justify-content: space-between; margin-top: var(--spacing-8); }
.cooldown-label { display: inline-flex; align-items: center; gap: var(--spacing-6); font-size: 0.8846rem; color: var(--text-secondary); }
.btn-sm { height: 24px; padding: 0 var(--spacing-8); font-size: 0.8462rem; }
</style>
