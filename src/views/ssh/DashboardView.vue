<template>
  <div class="dashboard-view">
    <!-- 连接状态条：仿 SFTP 风格。页面标题"监控仪表盘"；
         按钮随连接态切换，任一时刻只有一枚：未连接给「连接远程」，
         已连接给「断开远程」（不再并排出现连接/断开）。 -->
    <div class="dash-conn-bar">
      <div class="conn-left">
        <div class="conn-dot" :class="hasRemoteSession ? 'online' : 'offline'"></div>
        <span class="conn-name">监控仪表盘</span>
        <span v-if="hasRemoteSession && monitor.current" class="conn-host">{{ monitor.current.host }}</span>
      </div>
      <div class="conn-right">
        <span v-if="monitor.lastError && !monitor.polling && monitor.current" class="conn-stopped" :title="monitor.lastError.message">
          监控已中断：{{ monitor.lastError.message }}
          <span v-if="monitor.lastError.retryable" class="conn-stopped-hint">（连接恢复后自动重连）</span>
        </span>
        <button v-if="hasRemoteSession" class="btn btn-danger" @click="handleDisconnect">断开远程</button>
        <button v-else class="btn btn-primary" @click="showConnPicker = true">连接远程</button>
      </div>
    </div>

     <!-- 已连接但还没拿到指标 → 简单等待态（轮询超时或异常会由 store 写入 lastError 上浮到条上） -->
     <div v-if="!monitor.current && (monitor.polling || hasRemoteSession)" class="dash-empty">
       <template v-if="monitor.lastError">
         <p class="dash-empty-err">{{ monitor.polling ? '采集失败（自动重试中）' : '监控已中断' }}</p>
         <p class="dash-empty-sub">{{ monitor.lastError.message }}</p>
         <p v-if="monitor.lastError.retryable" class="dash-empty-sub dash-empty-sub-hint">连接恢复后将自动继续采集</p>
       </template>
       <template v-else>
         <p>正在采集服务器指标…</p>
       </template>
     </div>

     <!-- 无远程会话 → 连接引导态：有保存的连接才可连 -->
     <div v-else-if="!monitor.current" class="dash-empty">
       <p>未连接远程服务器</p>
     </div>

     <div v-else class="dash-body">
       <!-- 指标卡 -->
       <div class="dash-cards">
        <div class="card" :class="toneCls(monitor.current.cpu.used)">
          <div class="card-head">
            <span class="card-label">CPU</span>
            <span class="card-val">{{ monitor.current.cpu.used.toFixed(1) }}%</span>
          </div>
          <Sparkline :values="monitor.cpuHistory" :width="180" :height="36" />
          <div class="card-sub">iowait {{ monitor.current.cpu.iowait.toFixed(1) }}%</div>
        </div>

        <div class="card" :class="toneCls(monitor.current.mem.percent)">
          <div class="card-head">
            <span class="card-label">内存</span>
            <span class="card-val">{{ monitor.current.mem.percent.toFixed(1) }}%</span>
          </div>
          <Sparkline :values="monitor.memHistory" :width="180" :height="36" />
          <div class="card-sub">{{ (monitor.current.mem.used / 1024).toFixed(1) }}G / {{ (monitor.current.mem.total / 1024).toFixed(1) }}G</div>
        </div>

        <div class="card" :class="loadToneCls()">
          <div class="card-head">
            <span class="card-label">负载</span>
            <span class="card-val">{{ monitor.current.load[0].toFixed(2) }}</span>
          </div>
          <Sparkline :values="monitor.loadHistory" :width="180" :height="36" :max="loadMax" />
          <div class="card-sub">1 / 5 / 15 min：{{ monitor.current.load.map(v => v.toFixed(1)).join(' · ') }}</div>
        </div>
      </div>

      <!-- 磁盘 -->
      <div class="dash-section">
        <div class="section-title">磁盘</div>
        <div v-if="monitor.current.disks.length === 0" class="section-empty">未检测到磁盘挂载信息</div>
        <div v-else class="disk-list">
          <div v-for="d in monitor.current.disks" :key="d.mount" class="disk-row">
            <span class="disk-mount" :title="d.mount">{{ d.mount }}</span>
            <div class="disk-bar">
              <div class="disk-bar-fill" :class="toneCls(d.percent)" :style="{ width: Math.min(100, d.percent) + '%' }"></div>
            </div>
            <span class="disk-pct" :class="toneCls(d.percent)">{{ d.percent }}%</span>
            <span class="disk-size">{{ (d.used / 1024).toFixed(0) }}G / {{ (d.total / 1024).toFixed(0) }}G</span>
          </div>
        </div>
      </div>

      <!-- GPU（nvidia-smi 采集；无驱动/无卡时显示提示）
           无外层 dash-section 边框：GpuNvtopPanel 自带终端风格边框，
           双层框会产生"框中框"的割裂感。无 GPU 时降级为普通 section -->
      <div v-if="monitor.current.gpus.length === 0" class="dash-section">
        <div class="section-title">
          <span>GPU<span class="section-sub">· nvidia-smi</span></span>
        </div>
        <div class="section-empty">
          <template v-if="monitor.current.gpuError">GPU 采集异常：{{ monitor.current.gpuError }}</template>
          <template v-else>未检测到 NVIDIA GPU（无 GPU，或远端无驱动）</template>
        </div>
      </div>
      <div v-else class="gpu-embed">
        <div class="gpu-embed-head">
          <span>GPU<span class="section-sub">· nvidia-smi</span></span>
        </div>
        <GpuNvtopPanel
          :metrics="monitor.current"
          :gpu-procs="monitor.gpuProcs"
          :gpu-hist="monitor.gpuHist"
          :cpu-hist="monitor.cpuHistory"
          :mem-hist="monitor.memHistory"
          :load-hist="monitor.loadHistory"
        />
      </div>

      <!-- 进程（CPU）：ps 按 CPU 降序；GPU 占用进程已剔除至下方「进程（GPU）」表 -->
      <div class="dash-section">
        <div class="section-title">
          <span>进程（CPU）<span class="section-sub">共 {{ procList.length }} 个 · 每 60 秒刷新</span></span>
          <span v-if="totalProcPages > 1" class="port-pager">
            <button class="port-page-btn" :disabled="procPage === 0" title="上一页" @click="procPage--">‹</button>
            <span class="port-page-ind">{{ procPage + 1 }}/{{ totalProcPages }}</span>
            <button class="port-page-btn" :disabled="procPage >= totalProcPages - 1" title="下一页" @click="procPage++">›</button>
          </span>
        </div>
        <!-- 计数与空态都跟表体同源（procList）：procs 全是 GPU 进程时这张表确实一行不渲染，
             但那不是「没检测到进程」，两件事别写成同一句话 -->
        <div v-if="procList.length === 0" class="section-empty">{{ monitor.procs.length ? 'CPU 前列进程均已归入下方「进程（GPU）」表' : '未检测到进程信息' }}</div>
        <table v-else class="proc-table">
          <thead>
            <tr>
              <!-- 列宽重排：PID/用户/状态 原本各分到 1/7，短内容大量留白，把宽度让给名称列
                   （名称要容纳命令行副行）。table-layout 已固定，列宽只由表头这一行决定 -->
              <th class="col-pid">PID</th><th class="col-name">名称</th><th class="col-user">用户</th><th class="col-cpu">CPU %</th><th class="col-mem">内存</th><th class="col-etime">运行</th><th class="col-stat">状态</th><th class="proc-act">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in pagedProcs" :key="p.pid">
              <td class="mono">{{ p.pid }}</td>
              <td class="proc-name">
                <div class="proc-name-box">
                  <span class="proc-name-text mono" :title="procTitle(p)">{{ p.name }}</span>
                  <!-- 副行放命令行：comm 只有可执行名（内核限 15 字节），uvicorn/gunicorn/celery
                       起的进程全叫 python，不给参数就分不出是哪个服务 -->
                  <span v-if="p.args" class="proc-cmd mono" :title="procTitle(p)">{{ shortCmd(p.args) }}</span>
                </div>
              </td>
              <td class="mono">{{ p.user }}</td>
              <td :class="{ hot: p.cpu > PCT_HOT }">{{ p.cpu.toFixed(1) }}</td>
              <td>{{ formatRss(p.rss) }}</td>
              <td class="proc-etime">{{ p.etime }}</td>
              <td><span class="proc-stat" :title="statFull(p.stat)">{{ p.stat }}</span></td>
              <td class="proc-act"><button class="proc-kill" title="终止进程" @click="confirmKill(p)">终止</button></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 进程（GPU）：nvidia-smi 采集的显存占用进程。数据随 5s 快轮询采回，但表按 60s 换快照
           （server-monitor.ts 的 GPU_PROCS_INTERVAL_MS）——所以这里写 60 秒是实话。无 GPU 机器不渲染此卡片 -->
      <div v-if="monitor.current.gpus.length > 0" class="dash-section">
        <div class="section-title">
          <span>进程（GPU）<span class="section-sub">共 {{ gpuProcList.length }} 个 · 每 60 秒刷新</span></span>
          <span v-if="totalGpuPages > 1" class="port-pager">
            <button class="port-page-btn" :disabled="gpuPage === 0" title="上一页" @click="gpuPage--">‹</button>
            <span class="port-page-ind">{{ gpuPage + 1 }}/{{ totalGpuPages }}</span>
            <button class="port-page-btn" :disabled="gpuPage >= totalGpuPages - 1" title="下一页" @click="gpuPage++">›</button>
          </span>
        </div>
        <div v-if="gpuProcList.length === 0" class="section-empty">暂无进程占用 GPU 显存</div>
        <table v-else class="proc-table">
          <thead>
            <tr><th>显卡</th><th>PID</th><th>名称</th><th>用户</th><th>显存</th><th>CPU %</th><th>运行</th><th class="proc-act">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="gp in pagedGpuProcs" :key="`${gp.gpuIndex}:${gp.pid}`">
              <td class="mono">GPU {{ gp.gpuIndex }}</td>
              <td class="mono">{{ gp.pid }}</td>
              <td class="proc-name"><span class="proc-name-text mono" :title="gp.name">{{ gp.name }}</span></td>
              <td class="mono">{{ gp.user }}</td>
              <td>{{ formatRss(gp.memMB * 1024) }}</td>
              <td>{{ gp.cpu === null ? '—' : gp.cpu.toFixed(1) }}</td>
              <td class="proc-etime">{{ gp.etime || '—' }}</td>
              <td class="proc-act"><button class="proc-kill" title="终止进程" @click="confirmGpuProcKill(gp)">终止</button></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 监听端口 -->
      <div class="dash-section">
        <div class="section-title">
          <span>监听端口<span class="section-sub">共 {{ monitor.current.ports.length }} 个{{ portCappedHint }}</span></span>
          <span v-if="totalPortPages > 1" class="port-pager">
            <button class="port-page-btn" :disabled="portPage === 0" title="上一页" @click="portPage--">‹</button>
            <span class="port-page-ind">{{ portPage + 1 }}/{{ totalPortPages }}</span>
            <button class="port-page-btn" :disabled="portPage >= totalPortPages - 1" title="下一页" @click="portPage++">›</button>
          </span>
        </div>
        <div v-if="monitor.current.ports.length === 0" class="section-empty">未检测到监听端口（ss 不可用时为空）</div>
        <table v-else class="port-table">
          <thead>
            <tr><th>协议</th><th>端口</th><th>监听地址</th><th>Backlog</th><th>Recv</th><th>接口</th><th>进程</th><th title="按 PID 在 CPU TOP 50 进程内关联；休眠监听进程（sshd/nginx 等）常不入榜，空值不代表无属主">属主</th><th class="port-act">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="pt in pagedPorts" :key="`${pt.proto}:${pt.addr}:${pt.port}`">
              <td>
                <span class="port-proto-tag" :class="{ udp: pt.proto === 'udp' }" :title="secondaryProtoTitle(pt)">{{ pt.proto.toUpperCase() }}</span>
              </td>
              <td class="mono port-num">
                {{ pt.port }}<span v-if="svcName(pt.port)" class="port-svc">{{ svcName(pt.port) }}</span>
                <span v-if="hasAltAddr(pt) || hasAltProto(pt)" class="port-alt-hint" :title="altAddrTitle(pt)">⇄</span>
              </td>
              <td class="mono port-addr" :title="pt.addr">{{ pt.addr }}</td>
              <td class="mono port-backlog"><span v-if="pt.proto === 'udp'" class="port-proc-none" title="UDP 无连接，不存在 listen backlog 语义">—</span><span v-else-if="pt.sendq !== undefined" :title="`listen backlog 上限：${pt.sendq}`">{{ pt.sendq }}</span><span v-else class="port-proc-none">—</span></td>
              <td class="mono port-recvq"><span v-if="pt.proto === 'udp'" class="port-proc-none" title="UDP 的 Recv-Q 是排队字节数而非等待 accept 的连接数，不按连接口径展示">—</span><span v-else-if="pt.recvq !== undefined" :class="{ 'port-recvq-warn': pt.recvq > 0 }" :title="pt.recvq > 0 ? `当前积压 ${pt.recvq} 个连接等待 accept` : '接收队列空闲'">{{ pt.recvq }}</span><span v-else class="port-proc-none">—</span></td>
              <td class="mono port-iface"><span v-if="pt.iface" :title="`监听接口：${pt.iface}`">{{ pt.iface }}</span><span v-else class="port-proc-none" title="全局监听（无特定接口）">—</span></td>
              <td class="port-proc">
                <template v-if="pt.proc">
                  <span class="port-proc-name" :title="pt.proc">{{ pt.proc }}</span>
                  <span v-if="pt.pid" class="port-pid mono">PID {{ pt.pid }}</span>
                </template>
                <span v-else class="port-proc-none">—</span>
              </td>
              <td class="mono port-user"><span v-if="pt.user" :title="`进程属主：${pt.user}`">{{ pt.user }}</span><span v-else class="port-proc-none" title="属主仅在 CPU TOP 50 进程内按 PID 关联，休眠监听进程常不入榜">—</span></td>
              <td class="port-act">
                <button v-if="pt.pid && (!pt.secondaryPids || pt.secondaryPids.length === 0)" class="proc-kill" title="终止监听进程" @click="confirmPortKill(pt)">终止</button>
                <span v-else-if="pt.secondaryPids && pt.secondaryPids.length > 0" class="port-proc-none" title="该端口由多个进程分别监听（TCP/UDP 不同 PID），合并行不具备一键终止条件；请到进程表分别处置">—</span>
                <span v-else class="port-proc-none" title="无权限获取监听进程 PID（需 root 或进程属主）">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 连接选择模态 -->
    <Teleport to="body">
      <div v-if="showConnPicker" class="dc-overlay" @click.self="showConnPicker = false">
        <div ref="connPickerRef" class="dc-dialog" role="dialog" aria-modal="true" aria-label="选择 SSH 连接" tabindex="-1">
          <div class="dc-header">
            <span>选择 SSH 连接</span>
            <button class="dc-close" title="关闭" aria-label="关闭" @click="showConnPicker = false">×</button>
          </div>
          <div class="dc-list">
            <div v-if="sshStore.connections.length === 0" class="dc-empty">
              暂无 SSH 连接配置，请先在「远程管理」中新增连接
            </div>
            <div
              v-for="conn in sshStore.connections"
              :key="conn.id"
              class="dc-item"
              :class="{ disabled: connecting }"
              role="button"
              tabindex="0"
              @click="handleConnect(conn)"
              @keydown.enter.prevent="handleConnect(conn)"
              @keydown.space.prevent="handleConnect(conn)"
            >
              <div class="dc-info">
                <span class="dc-name">{{ conn.name }}</span>
                <span class="dc-host">{{ conn.username }}@{{ conn.host }}:{{ conn.port }}<span v-if="connNote(conn)" class="dc-note" :title="connNote(conn)">{{ connNote(conn) }}</span></span>
              </div>
              <svg v-if="connecting && pendingConnId === conn.id" class="dc-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 终止进程确认模态 -->
    <Teleport to="body">
      <div v-if="killDialog.visible" class="dc-overlay" @click.self="killDialog.visible = false">
        <div ref="killDialogRef" class="dc-dialog dc-dialog-sm" role="dialog" aria-modal="true" aria-label="确认终止进程" tabindex="-1">
          <div class="dc-header">
            <span>确认终止进程</span>
            <button class="dc-close" title="关闭" aria-label="关闭" @click="killDialog.visible = false">×</button>
          </div>
          <div class="dc-kill-body">
            <p class="dc-kill-line"><span class="dc-kill-label">PID</span><span class="mono dc-kill-val">{{ killDialog.target?.pid }}</span></p>
            <p class="dc-kill-line"><span class="dc-kill-label">名称</span><span class="dc-kill-val">{{ killDialog.target?.name }}</span></p>
            <p v-if="killDialog.target?.user" class="dc-kill-line"><span class="dc-kill-label">用户</span><span class="mono dc-kill-val">{{ killDialog.target.user }}</span></p>
            <p class="dc-kill-hint">SIGTERM 通知进程优雅退出；若进程未响应可使用 SIGKILL 强制终止。此操作不可撤销。</p>
          </div>
          <div class="dc-kill-actions">
            <button class="dc-btn" @click="killDialog.visible = false">取消</button>
            <button class="dc-btn dc-btn-danger" :disabled="killing" @click="handleKill(15)">
              <svg v-if="killing && killDialog.signal === 15" class="dc-btn-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
              终止（SIGTERM）
            </button>
            <button class="dc-btn dc-btn-warn" :disabled="killing" @click="handleKill(9)">
              <svg v-if="killing && killDialog.signal === 9" class="dc-btn-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
              强制（SIGKILL）
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { useModalFocusTrap } from '../../composables/useModalFocusTrap'
import { useServerMonitorStore } from '@/stores/server-monitor'
import { connNote, useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'
import { LOAD_HOT, LOAD_WARM, PCT_HOT, PCT_WARM, loadChartMax } from '@/utils/nvtop-scale'
import GpuNvtopPanel from '@/components/ssh/GpuNvtopPanel.vue'
import type { SSHConnection } from '@/stores/ssh'
import type { ProcessTopEntry, PortEntry, GpuProcessEntry } from '@/stores/server-monitor-core'
import { PORT_SERVICE_NAMES } from '@/stores/server-monitor-core'

const monitor = useServerMonitorStore()
const sshStore = useSSHStore()

const showConnPicker = ref(false)
const connecting = ref(false)
const pendingConnId = ref('')

/** 当前是否有活跃的远程会话（判断依据：store.sessions 中是否存在 type='remote'） */
const hasRemoteSession = computed(() =>
  sshStore.sessions.some(s => s.type === 'remote'),
)

/**
 * 监控目标会话：与 useSshNavigation 的 monitoredTarget 选择规则一致
 * （活动远程会话优先，否则首个远程会话）；已有采样时直接按 sshId 回查，
 * 保证「断开」按钮操作的就是正在被监控的那台（多台远程时不再错指）。
 */
const monitoredSession = computed(() => {
  const sshId = monitor.current?.sshId
  if (sshId) {
    const s = sshStore.sessions.find(x => x.id === sshId)
    if (s) return s
  }
  const remotes = sshStore.sessions.filter(s => s.type === 'remote')
  return remotes.find(s => s.id === sshStore.activeSessionId) || remotes[0] || null
})

async function handleConnect(conn: SSHConnection): Promise<void> {
  if (connecting.value) return
  connecting.value = true
  pendingConnId.value = conn.id
  try {
    // 与 DockerView 同一条口径：用 connectRemote 返回的会话 id 切过去，不按 connectionId
    // 回查 —— 同一主机已有会话时 find 命中的是最早那条，采样会挂到旧会话上，
    // 新开这条白占一个远端 PTY。
    const newId = await sshStore.connectRemote(conn.id, 80, 24)
    sshStore.switchSession(newId)
    showConnPicker.value = false
    useToastStore().success(`已连接 ${conn.name}`)
  } catch (err: unknown) {
    // S0-24：握手/认证失败抛的是 SSHError transport 或 Electron 信封，
    // 原样 err.message 会把 getaddrinfo、Cannot parse privateKey 这类技术串端给用户。
    useToastStore().error(toFriendlyMessage(err, 'SSH 连接失败'))
  } finally {
    connecting.value = false
    pendingConnId.value = ''
  }
}

function handleDisconnect(): void {
  const target = monitoredSession.value
  if (target) {
    sshStore.closeSession(target.id)
  }
}

// ── 终止进程 ──
const killDialog = ref({ visible: false, target: null as { pid: number; name: string; user?: string } | null, signal: 15 })
const killing = ref(false)

// 两个弹窗都自绘遮罩：Tab 环形与关闭还原焦点走共享件，Esc 走同一个出口
const connPickerRef = ref<HTMLElement | null>(null)
const killDialogRef = ref<HTMLElement | null>(null)
const connPickerTrap = useModalFocusTrap(connPickerRef)
const killTrap = useModalFocusTrap(killDialogRef)
watch(showConnPicker, (v) => { if (v) connPickerTrap.activate(); else connPickerTrap.deactivate() })
watch(() => killDialog.value.visible, (v) => { if (v) killTrap.activate(); else killTrap.deactivate() })
function onDialogKey(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return
  if (killDialog.value.visible) killDialog.value.visible = false
  else if (showConnPicker.value) showConnPicker.value = false
}
onMounted(() => window.addEventListener('keydown', onDialogKey))
onUnmounted(() => window.removeEventListener('keydown', onDialogKey))

function confirmKill(proc: ProcessTopEntry): void {
  killDialog.value = { visible: true, target: { pid: proc.pid, name: proc.name, user: proc.user }, signal: 15 }
}

/** 名称单元格的悬停说明：同名进程靠命令行区分，短名本身没有信息量；顺带给出父子关系。
    这里给完整命令行（含解释器绝对路径），副行展示的是省略版 */
function procTitle(p: ProcessTopEntry): string {
  const head = p.ppid ? `PID ${p.pid} · 父进程 ${p.ppid}` : `PID ${p.pid}`
  return p.args ? `${head}\n${p.args}` : head
}

/**
 * 副行的展示形态：首段若是绝对路径，只留 basename（/usr/local/bin/python3 → python3）。
 * 同机两份 python 的公共前缀正是这条路径，它占掉近一半列宽却分不出服务；
 * 真正有用的是解释器之后的模块名/脚本名。完整命令行仍在 title 里。
 */
function shortCmd(args: string): string {
  const m = /^(\/\S+)\s+(.*)$/.exec(args)
  if (!m) return args
  return `${m[1].slice(m[1].lastIndexOf('/') + 1)} ${m[2]}`
}

/** 端口行终止：杀的是监听该端口的进程（pid 来自 ss -p） */
function confirmPortKill(pt: PortEntry): void {
  if (!pt.pid) return
  killDialog.value = { visible: true, target: { pid: pt.pid, name: `端口 ${pt.port}（${pt.proc ?? '未知进程'}）` }, signal: 15 }
}

/** GPU 进程行终止：目标说明带卡号与显存，便于确认杀的是哪张卡的进程 */
function confirmGpuProcKill(gp: GpuProcessEntry): void {
  killDialog.value = { visible: true, target: { pid: gp.pid, name: `${gp.name}（GPU${gp.gpuIndex} · 显存 ${formatRss(gp.memMB * 1024)}）`, user: gp.user }, signal: 15 }
}

async function handleKill(signal: number): Promise<void> {
  const target = killDialog.value.target
  const sshId = monitor.current?.sshId
  if (!target || !sshId) return
  killDialog.value.signal = signal
  killing.value = true
  try {
    const r = await window.electronAPI!.sshKillProcess(sshId, target.pid, signal)
    if (r.code === 0) {
      useToastStore().success(`已发送 SIG${signal} 到 PID ${target.pid}`)
      killDialog.value.visible = false
    } else {
      // 远端 kill 的 stderr 是英文技术串（`kill: (1234): Operation not permitted`），
      // 原样塞进 toast 违渲染侧隐藏技术信息的口径（S0-24 同一族）。而这一类失败有确定成因：
      // 端口表里的 pid 可能是 `sudo -n ss -ltnp` 才看得见的 root 监听进程，kill 却走登录
      // 用户的 exec 通道 ⇒ 必然 EPERM。报成因，不翻译原文。
      const raw = (r.stderr || r.stdout || `exit code ${r.code}`).split('\n')[0].slice(0, 120)
      const denied = /not permitted|permission denied|EPERM/i.test(raw)
      useToastStore().error(
        denied
          ? '终止失败：该进程不属于当前登录用户（常见于 root 持有的监听进程），需 root 权限处理'
          : '终止失败',
        6000,
        import.meta.env.DEV ? { details: raw } : undefined,
      )
    }
  } catch (err: unknown) {
    // 抛的是 SSHError transport（或 Electron 信封），原样 err.message 等于把内部通道名端给用户
    useToastStore().error(toFriendlyMessage(err, '终止进程失败'))
  } finally {
    killing.value = false
  }
}

/** RSS（KB）→ MB/GB 友好格式 */
function formatRss(kb: number): string {
  if (!kb || kb <= 0) return '—'
  if (kb >= 1048576) return (kb / 1048576).toFixed(1) + 'G'
  if (kb >= 1024) return (kb / 1024).toFixed(0) + 'M'
  return kb + 'K'
}

/** 进程状态码 → 完整提示 */
function statFull(stat: string): string {
  const MAP: Record<string, string> = {
    R: 'R — 运行中',
    S: 'S — 可中断睡眠',
    D: 'D — 不可中断睡眠（通常 IO）',
    Z: 'Z — 僵尸进程',
    T: 'T — 已停止',
    t: 't — 被调试器停止',
    X: 'X — 已消亡',
    I: 'I — 内核空闲线程',
  }
  const base = stat.charAt(0)
  return MAP[base] || stat
}

/** 百分比取色：阈值与 nvtop-scale 的 bandCls 同源（PCT_HOT/PCT_WARM），类名是本页 CSS 的一套 */
function toneCls(v?: number): string {
  if (v == null) return ''
  if (v > PCT_HOT) return 'tone-danger'
  if (v >= PCT_WARM) return 'tone-warn'
  return 'tone-cool'
}

/** 负载卡片配色：按 1min 负载值（LOAD_WARM 告警 / LOAD_HOT 危险，单机经验阈值，与 nvtop-scale.loadBandCls 同源）。正常档也归入 tone-cool（绿），与百分比口径一致 */
function loadToneCls(): string {
  const l = monitor.current?.load[0]
  if (l == null) return ''
  if (l >= LOAD_HOT) return 'tone-danger'
  if (l >= LOAD_WARM) return 'tone-warn'
  return 'tone-cool'
}

/** 负载 sparkline 上限：与 NvGauge 同源（nvtop-scale.loadChartMax），不再两份实现 */
const loadMax = computed(() => loadChartMax(monitor.loadHistory))

/**
 * 通用表格分页工厂：页码钳制（集合刷新时不重置，避免闪烁），
 * 仅当前页越界时回退到末页。端口表与进程表共用。
 */
function makePager<T>(source: Ref<T[]>, size: number) {
  const page = ref(0)
  const total = computed(() => Math.max(1, Math.ceil(source.value.length / size)))
  const paged = computed(() => source.value.slice(page.value * size, (page.value + 1) * size))
  watch(total, (n) => {
    if (page.value > n - 1) page.value = n - 1
  })
  return { page, total, paged }
}

/**
 * 监听端口表分页：每页 10 行。采集端上限 100（server-monitor-core），
 * 达上限时在标题尾部提示，避免静默截断。
 * 排序：TCP 在前 UDP 在后，按端口号升序 —— 分页跨页顺序稳定。
 */
const PORT_PAGE_SIZE = 10
const allPorts = computed(() => {
  const list = monitor.current?.ports ?? []
  return [...list].sort((a, b) =>
    a.proto === b.proto ? a.port - b.port : a.proto === 'tcp' ? -1 : 1,
  )
})
const { page: portPage, total: totalPortPages, paged: pagedPorts } = makePager(allPorts, PORT_PAGE_SIZE)
const portCappedHint = computed(() => (allPorts.value.length >= 100 ? '（已达采集上限）' : ''))

/** 常见端口的静态服务名（如 22→ssh、6379→redis）；映射表收拢在 server-monitor-core */
const svcName = (port: number): string => PORT_SERVICE_NAMES[port] ?? ''

/** 该端口是否有备选地址（通配符折叠后收进 secondaryAddrs 的 IPv4/IPv6 地址） */
function hasAltAddr(pt: PortEntry): boolean {
  return !!pt.secondaryAddrs && pt.secondaryAddrs.length > 0
}

/** 该端口是否有备选协议（TCP+UDP 同端口合并后收进 secondaryProtos） */
function hasAltProto(pt: PortEntry): boolean {
  return !!pt.secondaryProtos && pt.secondaryProtos.length > 0
}

/** 备选协议的提示文案（协议标签与 ⇄ 标记两处悬浮提示共用） */
function secondaryProtoTitle(pt: PortEntry): string {
  if (!hasAltProto(pt)) return ''
  return '也' + pt.secondaryProtos!.map(p => p.toUpperCase()).join('/') + '协议'
}

/** ⇄ 标记的悬浮提示：列出所有备选地址 + 备选协议 */
function altAddrTitle(pt: PortEntry): string {
  const parts: string[] = []
  if (pt.secondaryAddrs && pt.secondaryAddrs.length > 0) {
    parts.push('也监听：' + pt.secondaryAddrs.join('  '))
  }
  const protoTitle = secondaryProtoTitle(pt)
  if (protoTitle) parts.push(protoTitle)
  return parts.join('  •  ')
}

/** GPU 占用进程 PID 集合（nvidia-smi 采集）：进程（CPU）表据此剔除 GPU 进程 */
const gpuPidSet = computed(() => new Set(monitor.gpuProcs.map(gp => gp.pid)))

/** 进程（CPU）表：剔除 GPU 占用进程（它们移至独立的进程（GPU）表），每页 10 行 */
const PROC_PAGE_SIZE = 10
const procList = computed(() => {
  const gpu = gpuPidSet.value
  return monitor.procs.filter(p => !gpu.has(p.pid))
})
const { page: procPage, total: totalProcPages, paged: pagedProcs } = makePager(procList, PROC_PAGE_SIZE)

/** 进程（GPU）表：按显存降序，每页 10 行（采集端上限 20）。
 *  数据源是 store 里独立的 gpuProcs（每 60 秒换一次），不是每 5s 替换的 current ——
 *  否则表头写着 60 秒，实际每 5s 就在重算 */
const gpuProcList = computed(() =>
  [...monitor.gpuProcs].sort((a, b) => b.memMB - a.memMB),
)
const { page: gpuPage, total: totalGpuPages, paged: pagedGpuProcs } = makePager(gpuProcList, PROC_PAGE_SIZE)

// Sparkline 与状态条同构（手搓 SVG，避免引图表库）
const Sparkline = defineComponent({
  name: 'Sparkline',
  props: {
    values: { type: Array as () => number[], default: () => [] },
    width: { type: Number, default: 180 },
    height: { type: Number, default: 36 },
    max: { type: Number, default: 100 },
  },
  setup(p) {
    const path = computed(() => {
      const vals = p.values
      if (vals.length < 2) return ''
      const w = p.width
      const h = p.height
      const step = w / Math.max(1, vals.length - 1)
      const pts = vals.map((v, i) => {
        const x = i * step
        const y = h - Math.max(0, Math.min(p.max, v)) / p.max * (h - 4) - 2
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      return `M${pts.join(' L')}`
    })
    return () =>
      h('svg', {
        class: 'sparkline-lg',
        width: p.width,
        height: p.height,
        viewBox: `0 0 ${p.width} ${p.height}`,
        'aria-hidden': 'true',
      }, [
        h('line', { x1: 0, y1: p.height - 2, x2: p.width, y2: p.height - 2, class: 'spark-baseline' }),
        path.value
          ? h('path', { d: path.value, class: 'sparkline-lg-path', fill: 'none', 'stroke-width': 1.5 })
          : null,
      ])
  },
})
</script>

<style scoped src="./dashboard-view.css"></style>

<style src="./dashboard-view-global.css"></style>
