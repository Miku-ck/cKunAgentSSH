<template>
  <div class="panel">
    <header class="panel-header">
      <h3>运维</h3>
      <p class="panel-desc">出站代理、上游超时与重试、后端日志级别和诊断快照 —— 这一页的值只存在后端，改完即时生效，不需要重启</p>
    </header>

    <div class="panel-body">
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
          <button class="btn ghost" :disabled="!proxySet || savingProxy" @click="clearProxy()">清除</button>
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
        <div class="diag-head">
          <h4 class="section-title">诊断快照</h4>
          <button class="btn ghost" :disabled="diagLoading" @click="refreshDiagnostics()">
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
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * G4/G5/G8/G9/G10：运维面板。状态与保存动作全在 composable（可单测），
 * 面板只做渲染与展示派生，不直接碰 api / IPC。
 */
import { computed, onMounted } from 'vue'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'
import {
  RETRY_MAX,
  TIMEOUT_MAX,
  TIMEOUT_MIN,
  useOpsSettings,
} from '@/composables/useOpsSettings'
import { useDesktopPrefs } from '@/composables/useDesktopPrefs'
import type { LogLevel } from '@/api/client'

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

const retryOptions = Array.from({ length: RETRY_MAX + 1 }, (_, i) => i)

function onLevelChange(e: Event) {
  void setLogLevel((e.target as HTMLSelectElement).value as LogLevel)
}

function bytes(n: number | null): string {
  if (n === null || !Number.isFinite(n)) return '未知'
  const mb = n / (1024 * 1024)
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(n / 1024)} KB`
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

.section { margin-bottom: 24px; }
.section-title {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: .04em;
}
.state-text { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-primary); }
.muted { color: var(--text-tertiary); }
.mono { font-family: var(--font-mono, ui-monospace, monospace); font-size: 11.5px; }
.badge {
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 11px;
  border: 1px solid var(--border-subtle);
}
.badge-ok { color: var(--accent-primary); border-color: color-mix(in srgb, var(--accent-primary) 35%, transparent); }
.badge-warn { color: #d7a54a; border-color: rgba(215, 165, 74, .4); }
.inline-form { display: flex; align-items: center; gap: 8px; margin: 4px 0 6px; }
.grow { flex: 1; min-width: 0; }
.num-row { display: flex; align-items: center; gap: 6px; }
.num-input { width: 88px; }
.unit { font-size: 12px; color: var(--text-secondary); }
.row-actions { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.hint { margin: 4px 0 0; font-size: 11.5px; color: var(--text-tertiary); line-height: 1.55; }
.hint.error { color: #cf8a80; }
.panel-error {
  margin: 0 0 10px;
  padding: 6px 10px;
  font-size: 12px;
  color: #cf8a80;
  background: rgba(207, 138, 128, .08);
  border: 1px solid rgba(207, 138, 128, .25);
  border-radius: var(--radius-sm);
}
.form-input {
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
.form-input:focus { border-color: var(--accent-primary); }
.form-input.input-error { border-color: #cf8a80; }
.btn {
  height: 28px;
  padding: 0 12px;
  background: var(--bg-elevated, var(--bg-input));
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
}
.btn:hover:not(:disabled) { border-color: var(--accent-primary); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn.ghost { background: transparent; color: var(--text-secondary); }
.diag-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.diag-grid {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: 2px 12px;
  margin: 8px 0 0;
  font-size: 11.5px;
}
.diag-group-title {
  grid-column: 1 / -1;
  margin-top: 8px;
  color: var(--text-secondary);
  font-weight: 600;
}
.diag-k { color: var(--text-tertiary); }
.diag-v { margin: 0; color: var(--text-primary); word-break: break-all; }
</style>
