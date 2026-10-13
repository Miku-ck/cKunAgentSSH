<template>
  <div class="docker-view">
    <!-- 连接状态条：仿监控仪表盘。未连接时给“连接远程”引导；已连接则显示目标主机，可切换/断开 -->
    <div class="page-head">
      <div class="conn-left">
        <div class="conn-dot" :class="targetSession ? 'online' : 'offline'"></div>
        <span class="conn-name">Docker</span>
        <span v-if="targetHost" class="conn-host mono" :title="targetHost">{{ targetHost }}</span>
      </div>

      <!-- 多台已连接远程会话：在此切换目标主机 -->
      <select v-if="remoteSessions.length > 1" v-model="targetId" class="host-sel" aria-label="切换目标主机">
        <option v-for="s in remoteSessions" :key="s.id" :value="s.id">{{ s.name }}</option>
      </select>

      <span class="head-spacer"></span>
      <div class="conn-right">
        <button v-if="targetSession" class="btn" @click="disconnectTarget">断开</button>
        <button class="btn btn-primary" @click="showConnPicker = true">连接远程</button>
      </div>
    </div>

    <!-- 无远程会话：Docker 依附远程主机，就地一句话引导（连接动作头部一直有） -->
    <div v-if="!remoteSessions.length" class="empty">尚未连接远程主机</div>

    <!-- 有目标主机：工具条 + 正文 -->
    <template v-else>
      <div class="toolbar">
        <div class="seg" role="tablist" aria-label="Docker 资源">
          <button v-for="t in TABS" :key="t.key" class="seg-btn" role="tab" :aria-selected="tab === t.key" :class="{ on: tab === t.key }" @click="selectTab(t.key)">{{ t.label }}</button>
        </div>
        <div class="search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input v-model="query" placeholder="按名称过滤…" aria-label="过滤" />
        </div>
        <div class="tb-right">
          <button
            v-if="tab === 'volumes'"
            class="btn btn-danger-ghost"
            :disabled="!!busy || !volumes.length"
            title="删除未被容器引用的卷（docker volume prune -f）；Docker 23+ 默认只清匿名卷，具名卷需逐条删除"
            @click="doPruneVolumes"
          >
            <svg v-if="busy === 'prune:volumes'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
            清理未使用
          </button>
          <button
            v-else-if="tab === 'networks'"
            class="btn btn-danger-ghost"
            :disabled="!!busy || !networks.length"
            title="删除所有未被容器使用的自定义网络（docker network prune -f）"
            @click="doPruneNetworks"
          >
            <svg v-if="busy === 'prune:networks'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
            清理未使用
          </button>
          <button class="btn" @click="refresh">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
            刷新
          </button>
        </div>
      </div>

      <div v-if="loadError" class="err-band" role="alert">
        <svg class="eb-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <div class="eb-text">
          <div class="eb-msg">{{ loadError.message }}</div>
          <div v-if="loadError.hint" class="eb-hint">{{ loadError.hint }}</div>
        </div>
        <button v-if="loadError.retryable" class="btn btn-sm" @click="refresh">重试</button>
      </div>
      <div v-else-if="loading && !containers.length" class="load-band">
        <svg class="dk-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
        正在经 SSH 会话执行 docker ps …
      </div>
      <div v-else class="body">
        <!-- 左列：列表 -->
        <div class="list-col">
          <!-- 容器 -->
          <template v-if="tab === 'containers'">
            <div class="thead">
              <span></span><span>容器 ID</span><span>镜像</span><span>命令</span><span>创建</span><span>状态</span><span>端口</span><span>名称</span><span>操作</span>
            </div>
            <div class="list">
              <div v-for="c in filteredContainers" :key="c.id" class="crow" role="button" tabindex="0"
                   :class="{ selected: selected?.id === c.id, abnormal: c.state === 'exited' }" :aria-pressed="selected?.id === c.id"
                   @click="selected = c" @keydown.enter.prevent="selected = c" @keydown.space.prevent="selected = c">
                <span class="st-dot" :class="c.state"></span>
                <span class="c-id mono" :title="c.id">{{ c.id }}</span>
                <span class="c-image"><span class="repo" :title="c.repo + ':' + c.tag">{{ c.repo }}</span><span class="tag mono">{{ c.tag }}</span></span>
                <span class="c-cmd mono" :title="c.cmd">{{ c.cmd || '—' }}</span>
                <span class="c-created">{{ c.created || '—' }}</span>
                <span class="c-state" :class="c.state" :title="c.stateText">{{ c.stateText }}</span>
                <span class="c-ports mono" :class="{ none: !c.ports }" :title="c.ports || '未映射'">{{ c.ports || '未映射' }}</span>
                <span class="c-name mono" :title="c.name">{{ c.name }}</span>
                <!-- 行内统一操作列（单一操作面）：启停 toggle + exec（仅运行中）+ 重启 + 删除；均为图标，in-flight 禁用防连点 -->
                <span class="c-acts" @click.stop>
                  <button v-if="c.state === 'running'" class="icon-btn stop" title="停止" :disabled="busy === c.id" @click="doStop(c)">
                    <svg v-if="busy === c.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                    <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
                  </button>
                  <button v-else class="icon-btn start" title="启动" :disabled="busy === c.id" @click="doStart(c)">
                    <svg v-if="busy === c.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                    <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="6 4 20 12 6 20 6 4"/></svg>
                  </button>
                  <button v-if="c.state === 'running'" class="icon-btn act-exec" title="exec 进入终端（新标签）" :disabled="busy === c.id" @click="doExec(c)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
                  </button>
                  <button class="icon-btn act-restart" title="重启" :disabled="busy === c.id" @click="doRestart(c)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
                  </button>
                  <button class="icon-btn act-del" title="删除" :disabled="busy === c.id" @click="doRemove(c)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                  </button>
                </span>
              </div>
              <div v-if="!filteredContainers.length" class="list-empty">{{ hasContainers ? '没有匹配的容器' : '该主机没有容器' }}</div>
            </div>
          </template>

          <!-- 镜像 -->
          <div v-else-if="tab === 'images'" class="list">
            <div v-if="partialErrors.images" class="list-empty err-hint">镜像取数失败：{{ partialErrors.images }}</div>
            <table v-else-if="filteredImages.length" class="ptable">
              <colgroup><col style="width:50%"><col style="width:20%"><col style="width:18%"><col style="width:12%"></colgroup>
              <thead><tr><th>镜像</th><th>镜像 ID</th><th>内容大小</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="im in filteredImages" :key="imageLabel(im)">
                  <td class="mono" :title="imageLabel(im)">{{ imageLabel(im) }}</td>
                  <td class="mono muted" :title="im.id">{{ im.id }}</td>
                  <td class="mono">{{ im.size }}</td>
                  <td>
                    <span class="c-acts">
                      <button class="icon-btn act-del" title="删除镜像" :disabled="busy === imageLabel(im)" @click="doRemoveImage(im)">
                        <svg v-if="busy === imageLabel(im)" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                      </button>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-else class="list-empty">{{ images.length ? '没有匹配的镜像' : '该主机没有镜像' }}</div>
          </div>

          <!-- 卷：6 列 + 行点击开详情 + rm 行内操作 -->
          <div v-else-if="tab === 'volumes'" class="list">
            <div v-if="partialErrors.volumes" class="list-empty err-hint">卷取数失败：{{ partialErrors.volumes }}</div>
            <table v-else-if="filteredVolumes.length" class="ptable clickable">
              <colgroup><col style="width:9%"><col style="width:30%"><col style="width:11%"><col style="width:12%"><col style="width:24%"><col style="width:14%"></colgroup>
              <thead><tr><th>驱动</th><th>卷名</th><th>大小</th><th>引用</th><th>标签</th><th>操作</th></tr></thead>
              <tbody>
                <tr
                  v-for="v in filteredVolumes" :key="v.name"
                  :class="{ selected: selectedVolume?.name === v.name }"
                  @click="openVolumeDetail(v)"
                >
                  <td class="mono muted">{{ v.driver }}</td>
                  <td class="mono" :title="v.name">{{ v.name }}</td>
                  <td class="mono">{{ v.size || '—' }}</td>
                  <td class="use-hint" :class="volumeUseHint(v).tone">{{ volumeUseHint(v).text }}</td>
                  <td class="mono label-hint" :title="v.labels ? Object.entries(v.labels).map(([k, val]) => k + '=' + val).join('\n') : ''">{{ labelHint(v.labels) }}</td>
                  <td>
                    <span class="c-acts" @click.stop>
                      <button class="icon-btn act-del" title="删除卷" :disabled="busy === 'v:' + v.name" @click="doRemoveVolume(v)">
                        <svg v-if="busy === 'v:' + v.name" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                      </button>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-else class="list-empty">{{ volumes.length ? '没有匹配的卷' : '该主机没有卷' }}</div>
          </div>

          <!-- 网络：6 列 + 行点击开详情 + rm 行内操作 -->
          <div v-else class="list">
            <div v-if="partialErrors.networks" class="list-empty err-hint">网络取数失败：{{ partialErrors.networks }}</div>
            <table v-else-if="filteredNetworks.length" class="ptable clickable">
              <colgroup><col style="width:16%"><col style="width:12%"><col style="width:26%"><col style="width:12%"><col style="width:20%"><col style="width:14%"></colgroup>
              <thead><tr><th>网络 ID</th><th>驱动</th><th>名称</th><th>范围</th><th>标签</th><th>操作</th></tr></thead>
              <tbody>
                <tr
                  v-for="n in filteredNetworks" :key="n.id"
                  :class="{ selected: selectedNetwork?.id === n.id }"
                  @click="openNetworkDetail(n)"
                >
                  <td class="mono muted" :title="n.id">{{ n.id }}</td>
                  <td class="mono">{{ n.driver }}</td>
                  <td class="mono" :title="n.name">{{ n.name }}</td>
                  <td class="muted">{{ n.scope }}</td>
                  <td class="mono label-hint" :title="n.labels ? Object.entries(n.labels).map(([k, val]) => k + '=' + val).join('\n') : ''">{{ labelHint(n.labels) }}</td>
                  <td>
                    <span class="c-acts" @click.stop>
                      <button class="icon-btn act-del" title="删除网络" :disabled="busy === 'n:' + n.name" @click="doRemoveNetwork(n)">
                        <svg v-if="busy === 'n:' + n.name" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                      </button>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-else class="list-empty">{{ networks.length ? '没有匹配的网络' : '该主机没有网络' }}</div>
          </div>
        </div>

        <!-- 容器详情已改为居中弹窗（见下方 Teleport），列表列独享整幅宽度 -->
      </div>

      <!-- 状态栏 -->
      <div class="statusbar">
        <span>{{ targetName }}</span>
        <span>远程 · SSH</span>
        <span>容器 <span class="mono">{{ containers.length }}</span>（<span class="ok">{{ runningCount }}</span> 运行 / <span class="bad">{{ abnormalCount }}</span> 异常 / <span class="mono">{{ stoppedCount }}</span> 停止）</span>
        <span class="sb-right mono">{{ lastSync }}</span>
      </div>
    </template>

    <!-- 连接选择模态：列出所有已保存 SSH 连接，点选即建会话并设为目标（Teleport 到 body，Esc 关闭） -->
    <Teleport to="body">
      <div v-if="showConnPicker" class="dk-overlay" @click.self="showConnPicker = false">
        <div class="dk-dialog" role="dialog" aria-modal="true" aria-label="选择 SSH 连接">
          <div class="dk-header">
            <span>选择 SSH 连接</span>
            <button class="dk-close" aria-label="关闭" @click="showConnPicker = false">×</button>
          </div>
          <div class="dk-list">
            <div v-if="!sshStore.connections.length" class="dk-empty">暂无 SSH 连接配置，请先在「远程管理」中新建连接</div>
            <div v-for="conn in sshStore.connections" :key="conn.id" class="dk-item" :class="{ disabled: connecting }" @click="handleConnect(conn)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
              <div class="dk-info">
                <span class="dk-name">{{ conn.name }}</span>
                <span class="dk-host">{{ conn.username }}@{{ conn.host }}:{{ conn.port }}</span>
              </div>
              <svg v-if="connecting && pendingConnId === conn.id" class="dk-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 容器详情弹窗：点行打开，居中大幅面（820×78vh），日志吃满纵向空间（Teleport 到 body，Esc / 点遮罩 / × 关闭） -->
    <Teleport to="body">
      <div v-if="tab === 'containers' && selected" class="dt-overlay" @click.self="selected = null">
        <div class="dt-dialog" role="dialog" aria-modal="true" :aria-label="'容器详情：' + selected.name">
          <div class="dp-head">
            <span class="st-dot" :class="selected.state"></span>
            <span class="dp-name mono">{{ selected.name }}</span>
            <span class="badge" :class="selected.state">{{ selected.stateText }}</span>
            <button class="icon-btn dp-close" title="关闭" aria-label="关闭" @click="selected = null">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
          <div class="dp-body">
            <section>
              <h4>容器信息</h4>
              <div class="kv">
                <span class="k">镜像</span><span class="v">{{ selected.repo }}:{{ selected.tag }}</span>
                <span class="k">容器 ID</span><span class="v">{{ selected.id }}</span>
                <span class="k">状态</span><span class="v">{{ selected.stateText }}</span>
                <span class="k">命令</span><span class="v">{{ selected.cmd }}</span>
                <span class="k">端口</span><span class="v">{{ selected.ports || '未映射' }}</span>
              </div>
            </section>
            <section class="logs-section">
              <h4>
                <span>日志 <span class="logs-sub mono">docker logs --tail 200</span></span>
                <button class="follow-btn" :class="{ on: follow }" :aria-pressed="follow" title="实时跟随（每 2.5s 轮询增量）" @click="setFollow(!follow)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="11" height="11"><path d="M12 2a10 10 0 1 0 10 10"/><path d="M12 7v5l3 2"/></svg>
                  跟随
                </button>
              </h4>
              <div v-if="logsError" class="logs-err">{{ logsError.message }}</div>
              <div v-else ref="logsEl" class="logs mono">
                <div v-if="logsLoading && !logs.length" class="logs-hint">加载日志…</div>
                <div v-else-if="!logs.length" class="logs-hint">（无日志输出）</div>
                <div v-for="l in logsView" v-else :key="l.id" class="lrow" :class="l.cls">{{ l.text || ' ' }}</div>
              </div>
            </section>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 卷详情弹窗：点行开，自适应高度（最高 78vh）；inspect 一次拉后展示元数据 + 标签 + 驱动选项，底部快捷删除 -->
    <Teleport to="body">
      <div v-if="tab === 'volumes' && selectedVolume" class="dt-overlay" @click.self="selectedVolume = null">
        <div class="dt-dialog dt-dialog-auto" role="dialog" aria-modal="true" :aria-label="'卷详情：' + selectedVolume.name">
          <div class="dp-head">
            <span class="dp-name mono">{{ selectedVolume.name }}</span>
            <span class="badge">{{ selectedVolume.driver }}</span>
            <span v-if="selectedVolume.size" class="badge">{{ selectedVolume.size }}</span>
            <span
              v-if="typeof selectedVolume.linkCount === 'number'"
              class="badge"
              :class="selectedVolume.linkCount === 0 ? 'warn' : 'ok'"
            >{{ selectedVolume.linkCount === 0 ? '未使用' : selectedVolume.linkCount + ' 个容器' }}</span>
            <button class="icon-btn dp-close" title="关闭" aria-label="关闭" @click="selectedVolume = null">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
          <div class="dp-body dp-body-auto">
            <div v-if="inspectLoading" class="load-band">docker volume inspect…</div>
            <div v-else-if="inspectError" class="logs-err">{{ inspectError.message }}</div>
            <template v-else-if="volInspect">
              <section>
                <h4>基本信息</h4>
                <div class="kv">
                  <span class="k">名称</span><span class="v">{{ volInspect.name }}</span>
                  <span class="k">驱动</span><span class="v">{{ volInspect.driver }}</span>
                  <span class="k">创建时间</span><span class="v">{{ volInspect.createdAt || '—' }}</span>
                  <span class="k">挂载点</span><span class="v">{{ volInspect.mountpoint || '—' }}</span>
                  <template v-if="volInspect.usage">
                    <span class="k">引用容器</span><span class="v">{{ volInspect.usage?.refCount ?? '—' }}</span>
                    <template v-if="typeof volInspect.usage?.size === 'number'">
                      <span class="k">占用空间</span><span class="v">{{ humanSize(volInspect.usage?.size) || '—' }}</span>
                    </template>
                  </template>
                </div>
              </section>
              <section v-if="Object.keys(volInspect.labels).length">
                <h4>标签 <span class="logs-sub mono">docker label</span></h4>
                <div class="kv kv-wide">
                  <template v-for="(val, key) in volInspect.labels" :key="key">
                    <span class="k mono" :title="key">{{ key }}</span>
                    <span class="v">{{ val }}</span>
                  </template>
                </div>
              </section>
              <section v-if="Object.keys(volInspect.options).length">
                <h4>驱动选项</h4>
                <div class="kv kv-wide">
                  <template v-for="(val, key) in volInspect.options" :key="key">
                    <span class="k mono" :title="key">{{ key }}</span>
                    <span class="v">{{ String(val) }}</span>
                  </template>
                </div>
              </section>
              <div class="dp-actions">
                <button class="btn btn-sm btn-danger" :disabled="busy === 'v:' + selectedVolume.name" @click="doRemoveVolume(selectedVolume)">删除卷</button>
              </div>
            </template>
            <div v-else class="list-empty">无详情</div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 网络详情弹窗：基础 + IPAM 表 + 连接容器表 + 标签 / 驱动选项，底部快捷删除 -->
    <Teleport to="body">
      <div v-if="tab === 'networks' && selectedNetwork" class="dt-overlay" @click.self="selectedNetwork = null">
        <div class="dt-dialog dt-dialog-auto" role="dialog" aria-modal="true" :aria-label="'网络详情：' + selectedNetwork.name">
          <div class="dp-head">
            <span class="dp-name mono">{{ selectedNetwork.name }}</span>
            <span class="badge">{{ selectedNetwork.driver }}</span>
            <span class="badge">{{ selectedNetwork.scope }}</span>
            <button class="icon-btn dp-close" title="关闭" aria-label="关闭" @click="selectedNetwork = null">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
          <div class="dp-body dp-body-auto">
            <div v-if="inspectLoading" class="load-band">docker network inspect…</div>
            <div v-else-if="inspectError" class="logs-err">{{ inspectError.message }}</div>
            <template v-else-if="netInspect">
              <section>
                <h4>基本信息</h4>
                <div class="kv">
                  <span class="k">名称</span><span class="v">{{ netInspect.name }}</span>
                  <span class="k">ID</span><span class="v">{{ netInspect.id }}</span>
                  <span class="k">驱动</span><span class="v">{{ netInspect.driver }}</span>
                  <span class="k">范围</span><span class="v">{{ netInspect.scope }}</span>
                  <span class="k">特性</span><span class="v">
                    <span v-if="netInspect.internal" class="flag">internal</span>
                    <span v-if="netInspect.attachable" class="flag">attachable</span>
                    <span v-if="netInspect.ingress" class="flag">ingress</span>
                    <span v-if="netInspect.enableIPv6" class="flag">ipv6</span>
                    <span v-if="!(netInspect.internal || netInspect.attachable || netInspect.ingress || netInspect.enableIPv6)">—</span>
                  </span>
                </div>
              </section>
              <section v-if="netInspect.ipam.length">
                <h4>IPAM</h4>
                <table class="ptable sub">
                  <thead><tr><th>子网</th><th>网关</th><th>范围</th></tr></thead>
                  <tbody>
                    <tr v-for="(c, i) in netInspect.ipam" :key="i">
                      <td class="mono">{{ c.subnet || '—' }}</td>
                      <td class="mono">{{ c.gateway || '—' }}</td>
                      <td class="mono muted">{{ c.range || '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </section>
              <section>
                <h4>连接容器 <span class="logs-sub mono">{{ netInspect.containers.length }}</span></h4>
                <div v-if="!netInspect.containers.length" class="list-empty sub">无容器连接（可安全删除）</div>
                <table v-else class="ptable sub">
                  <thead><tr><th>容器</th><th>IPv4</th><th>MAC</th></tr></thead>
                  <tbody>
                    <tr v-for="(c, i) in netInspect.containers" :key="i">
                      <td class="mono">{{ c.name || '—' }}</td>
                      <td class="mono">{{ c.ipv4 || '—' }}</td>
                      <td class="mono muted">{{ c.mac || '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </section>
              <section v-if="Object.keys(netInspect.labels).length">
                <h4>标签 <span class="logs-sub mono">docker label</span></h4>
                <div class="kv kv-wide">
                  <template v-for="(val, key) in netInspect.labels" :key="key">
                    <span class="k mono" :title="key">{{ key }}</span>
                    <span class="v">{{ val }}</span>
                  </template>
                </div>
              </section>
              <section v-if="Object.keys(netInspect.options).length">
                <h4>驱动选项</h4>
                <div class="kv kv-wide">
                  <template v-for="(val, key) in netInspect.options" :key="key">
                    <span class="k mono" :title="key">{{ key }}</span>
                    <span class="v">{{ String(val) }}</span>
                  </template>
                </div>
              </section>
              <div class="dp-actions">
                <button class="btn btn-sm btn-danger" :disabled="busy === 'n:' + selectedNetwork.name" @click="doRemoveNetwork(selectedNetwork)">删除网络</button>
              </div>
            </template>
            <div v-else class="list-empty">无详情</div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
// DockerView.vue — 远程 Docker 管理页
// 定位：依附已连接的 SSH 会话，经 sshExec 在其远程主机上真执行 docker 命令。
// 命令构造/解析/错误归类委托 docker-core.ts；本页只做 sshExec 编排与状态管理。
import { computed, nextTick, ref, watch, onUnmounted } from 'vue'
import { toFriendlyMessage } from '@/api/client'
import { useSSHStore, type SSHConnection } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import {
  PS_CMD, IMAGES_CMD, VOLUMES_CMD, NETWORKS_CMD,
  logsCmd, startCmd, stopCmd, restartCmd, rmCmd, rmiCmd, execCmd,
  volumeRmCmd, networkRmCmd, volumePruneCmd, networkPruneCmd,
  volumeInspectCmd, networkInspectCmd,
  parsePs, parseImages, parseVolumes, parseNetworks, parseLogs, imageLabel, imageHaystack,
  labelHint,
  parseVolumeInspect, parseNetworkInspect, humanSize,
  dockerErrorFromResult, describeThrownDockerError,
  type ContainerRow, type ImageRow, type VolumeRow, type NetworkRow, type DockerError,
  type VolumeInspect, type NetworkInspect,
} from '@/stores/docker-core'

const sshStore = useSSHStore()
const toast = useToastStore()
const { confirm } = useConfirm()
// exec 进入终端：命令写进目标会话的交互 shell 后，请求父级（SshView）切到该会话终端
const emit = defineEmits<{ 'exec-terminal': [sessionId: string] }>()

const TABS = [
  { key: 'containers', label: '容器' },
  { key: 'images', label: '镜像' },
  { key: 'volumes', label: '卷' },
  { key: 'networks', label: '网络' },
] as const
type TabKey = (typeof TABS)[number]['key']

const remoteSessions = computed(() => sshStore.sessions.filter(s => s.type === 'remote'))
const targetId = ref('')
// 默认落在活动远程会话；活动会话切换或远程列表变化时校正目标
watch(remoteSessions, (list) => {
  if (!list.some(s => s.id === targetId.value)) {
    const active = list.find(s => s.id === sshStore.activeSessionId)
    targetId.value = (active || list[0])?.id || ''
  }
}, { immediate: true })
const targetName = computed(() => remoteSessions.value.find(s => s.id === targetId.value)?.name || '')

// ── 手动连接（仿监控仪表盘：不预设必须有会话，可从已保存连接直接连） ──
const showConnPicker = ref(false)
const connecting = ref(false)
const pendingConnId = ref('')
const targetSession = computed(() => remoteSessions.value.find(s => s.id === targetId.value) || null)
/** 目标主机串：优先用 connectionId 回查连接记录，回落到会话 shell（username@host） */
const targetHost = computed(() => {
  const s = targetSession.value
  if (!s) return ''
  const conn = sshStore.connections.find(c => c.id === s.connectionId)
  return conn ? `${conn.username}@${conn.host}` : s.shell
})

async function handleConnect(conn: SSHConnection) {
  if (connecting.value) return
  connecting.value = true
  pendingConnId.value = conn.id
  try {
    // 用 connectRemote 返回的那条会话 id，不按 connectionId 回查：同一主机已有会话时
    // find 命中的是**最早那条**，目标会指回旧会话，而新开这条没人认领（多占一个远端
    // PTY，还挂在终端标签列表里）。
    targetId.value = await sshStore.connectRemote(conn.id, 80, 24)
    showConnPicker.value = false
    toast.success(`已连接 ${conn.name}`)
  } catch (err) {
    // S0-24 口径：握手/认证失败抛出的是 SSHError transport（或 Electron 信封），
    // 原样 err.message 进 toast 等于把 getaddrinfo、Cannot parse privateKey 端给用户。
    toast.error(toFriendlyMessage(err, 'SSH 连接失败'))
  } finally {
    connecting.value = false
    pendingConnId.value = ''
  }
}

function disconnectTarget() {
  const s = targetSession.value
  if (!s) return
  void sshStore.closeSession(s.id)
  toast.info(`已断开 ${s.name}`)
}

// 弹窗 Esc 关闭 + 卸载清理监听（DESIGN.md 模态硬规则）
function onPickerKey(e: KeyboardEvent) { if (e.key === 'Escape') showConnPicker.value = false }
watch(showConnPicker, open => {
  if (open) document.addEventListener('keydown', onPickerKey)
  else document.removeEventListener('keydown', onPickerKey)
})
onUnmounted(() => document.removeEventListener('keydown', onPickerKey))

const tab = ref<TabKey>('containers')
const query = ref('')

// ── 数据状态（真取数填充，非样例）──
const containers = ref<ContainerRow[]>([])
const images = ref<ImageRow[]>([])
const volumes = ref<VolumeRow[]>([])
const networks = ref<NetworkRow[]>([])
const selected = ref<ContainerRow | null>(null)
// 卷/网络详情弹窗：互斥开关（同一时间只会开一个），共用 inspectLoading/inspectError
const selectedVolume = ref<VolumeRow | null>(null)
const selectedNetwork = ref<NetworkRow | null>(null)
const volInspect = ref<VolumeInspect | null>(null)
const netInspect = ref<NetworkInspect | null>(null)
const inspectLoading = ref(false)
const inspectError = ref<DockerError | null>(null)

const loading = ref(false)
const loadError = ref<DockerError | null>(null)
// 子列表尽力而为取数失败时的分 tab 错误（区别于整页 loadError）：
// 避免把「docker images/volumes/networks 报错」伪装成「该主机没有该资源」
const partialErrors = ref<{ images?: string; volumes?: string; networks?: string }>({})
const errText = (e: unknown): string => (e as DockerError)?.message || String(e)
const lastSync = ref('')

const logs = ref<string[]>([])
const logsLoading = ref(false)
const logsError = ref<DockerError | null>(null)
const logsEl = ref<HTMLElement | null>(null)
const follow = ref(false)
let followTimer: ReturnType<typeof setInterval> | null = null

const busy = ref('')
const q = () => query.value.toLowerCase()

/** 异常（非0退出）优先排序：exited → running → stopped，出事容器一眼可见 */
const isAbnormal = (c: ContainerRow): boolean => c.state === 'exited'
const filteredContainers = computed(() => {
  const list = containers.value.filter(c => c.name.toLowerCase().includes(q()))
  const rank = (c: ContainerRow): number => (isAbnormal(c) ? 0 : c.state === 'running' ? 1 : 2)
  return [...list].sort((a, b) => rank(a) - rank(b))
})
// 过滤串与行内展示同一个来源：悬空/按摘要拉取的行显示的是短 ID，原先只搜 repo:tag，
// 搜屏幕上真正写着的那串 ID 反而一条都不命中。
const filteredImages = computed(() =>
  images.value.filter(im => imageHaystack(im).includes(q())))
const filteredVolumes = computed(() =>
  volumes.value.filter(v => v.name.toLowerCase().includes(q())))
const filteredNetworks = computed(() =>
  networks.value.filter(n => n.name.toLowerCase().includes(q())))
const runningCount = computed(() => containers.value.filter(c => c.state === 'running').length)
const stoppedCount = computed(() => containers.value.filter(c => c.state === 'stopped').length)
const abnormalCount = computed(() => containers.value.filter(isAbnormal).length)
const hasContainers = computed(() => containers.value.length > 0)

/** 日志行轻量着色：命中 error/panic/fatal → err；warn → warn；其余默认 */
function logClass(line: string): string {
  if (/error|panic|fatal|exception|failed/i.test(line)) return 'err'
  if (/warn/i.test(line)) return 'warn'
  return ''
}

// P2 修复：把日志行着色从模板里挪出来 —— 否则每帧渲染都对每行跑一次正则
// （docker logs --tail 200 ≈ 200 行 × 每次响应式更新）。改为 logs 变化时一次性算好 {text, cls}，
// 模板只做纯绑定，渲染开销随日志刷新频率而非组件重渲染频率发生。
const logsView = computed(() =>
  logs.value.map((line, i) => ({ id: i, text: line, cls: logClass(line) })),
)

// 一次性 docker 执行：成功返回 stdout；docker 层/传输层错误统一归为 DockerError 抛出
// id 默认取当前目标，但批量取数路径显式传入发起时的 id，避免执行中途切宿主后
// 后续命令打到新宿主（见 loadAll 的代次守卫）
//
// 取数与变更分预算：docker stop/restart/rm -f 自带 10s 的 SIGTERM→SIGKILL 宽限期，
// 卡住的容器（D 状态、挂死的后端卷）能拖到几十秒，rmi/prune 也要真删层数据。
// 原先 12 处变更动作共用 15s 一条预算，超时会把「远端其实正在成功」的命令报成失败，
// 界面随即停在旧状态 —— 用户对着一个其实已经停掉的容器再点一次停止。
const READ_TIMEOUT_SEC = 15
const ACTION_TIMEOUT_SEC = 60

async function dockerExec(
  cmd: string, id = targetId.value, timeoutSec = READ_TIMEOUT_SEC,
): Promise<string> {
  const api = window.electronAPI
  if (!id || !api) throw { code: 'NO_SESSION', message: '未选择目标主机', retryable: false } as DockerError
  let res
  try {
    res = await api.sshExec(id, cmd, timeoutSec)
  } catch (err) {
    throw describeThrownDockerError(err)
  }
  const dErr = dockerErrorFromResult(res.stderr, res.exitCode, res.stdout)
  if (dErr) throw dErr
  return res.stdout
}

/** 变更类动作的入口：只有这一类需要放宽预算，取数路径继续用默认值 */
function actionExec(cmd: string): Promise<string> {
  return dockerExec(cmd, targetId.value, ACTION_TIMEOUT_SEC)
}

// 跨宿主竞态守卫：loadAll 内串行+并行共 4 个 await（单条硬超时 15s），用户中途切主机
// 会再触发一轮：旧轮若照常写入，会把 A 的容器盖进已切到 B 的页面（混合宿主数据是运维
// 场景的危险误导），且旧轮 finally 会提前熄灭新轮的转圈。故每轮递增代次，写入前校验。
let loadGen = 0

async function loadAll(): Promise<void> {
  const id = targetId.value
  if (!id) return
  const gen = ++loadGen
  loading.value = true
  loadError.value = null
  partialErrors.value = {}
  try {
    // 容器为主视图；其失败即整体错误（未装 / 无权限都在此暴露）
    const ps = parsePs(await dockerExec(PS_CMD, id))
    if (gen !== loadGen) return // 已被新一轮取代，丢弃旧宿主写入
    containers.value = ps
    // 其余尽力而为：单个子命令失败不阻断整页，但要按 tab 如实上浮，不伪装成「没有资源」
    const [im, vo, ne] = await Promise.allSettled([
      dockerExec(IMAGES_CMD, id), dockerExec(VOLUMES_CMD, id), dockerExec(NETWORKS_CMD, id),
    ])
    if (gen !== loadGen) return
    images.value = im.status === 'fulfilled' ? parseImages(im.value) : []
    volumes.value = vo.status === 'fulfilled' ? parseVolumes(vo.value) : []
    networks.value = ne.status === 'fulfilled' ? parseNetworks(ne.value) : []
    partialErrors.value = {
      images: im.status === 'rejected' ? errText(im.reason) : undefined,
      volumes: vo.status === 'rejected' ? errText(vo.reason) : undefined,
      networks: ne.status === 'rejected' ? errText(ne.reason) : undefined,
    }
    lastSync.value = 'docker ps · ' + new Date().toLocaleTimeString()
    // 详情已改弹窗：不再自动选中首个容器（否则进页面/刷新就弹模态）；仅当选中项已不在列表才清空
    const cur = selected.value
    if (cur && !containers.value.some(c => c.id === cur.id)) {
      selected.value = null
    }
  } catch (e) {
    if (gen !== loadGen) return
    loadError.value = e as DockerError
    containers.value = []; images.value = []; volumes.value = []; networks.value = []
    partialErrors.value = {}
    selected.value = null
  } finally {
    if (gen === loadGen) loading.value = false
  }
}

// 发起对象必须在 await 之后重新校验：慢的那次返回时用户可能已经切到另一个容器、
// 换了宿主或关掉了详情，原样写回会把 A 的日志显示在 B 的窗口里 —— 对着错的日志排障
// 比报一次错更糟。2.5s 跟随轮询让这种交叠成为常态而非边角（同 loadAll 的代次守卫理由）。
async function loadLogs(quiet = false): Promise<void> {
  const c = selected.value
  const id = targetId.value
  const stillCurrent = (): boolean => selected.value === c && targetId.value === id
  logsError.value = null
  if (!c) { logs.value = []; return }
  if (!quiet) logsLoading.value = true
  try {
    const raw = await dockerExec(logsCmd(c.name), id)
    if (!stillCurrent()) return
    logs.value = parseLogs(raw)
    if (follow.value) await scrollLogsToBottom()
  } catch (e) {
    // 旧请求的失败不该盖住新选中的视图；也不许在取代后熄灭新一轮的转圈
    if (!stillCurrent()) return
    logsError.value = e as DockerError
  } finally {
    if (!quiet && stillCurrent()) logsLoading.value = false
  }
}

async function scrollLogsToBottom(): Promise<void> {
  await nextTick()
  const el = logsEl.value
  if (el) el.scrollTop = el.scrollHeight
}

// 跟随：sshExec 是请求/响应、非流式，故用 2.5s 轮询 tail 伪跟随（不动后端）
function setFollow(on: boolean): void {
  if (followTimer) { clearInterval(followTimer); followTimer = null }
  follow.value = on
  if (on) followTimer = setInterval(() => { void loadLogs(true) }, 2500)
}

function refresh(): void { void loadAll() }

// 切离当前 tab 时清空对应选中：避免残留选中项在切回时重新弹出上次的详情大窗
function selectTab(key: TabKey): void {
  tab.value = key
  if (key !== 'containers') selected.value = null
  if (key !== 'volumes') selectedVolume.value = null
  if (key !== 'networks') selectedNetwork.value = null
}

function toastErr(e: unknown): void {
  const d = e as DockerError
  toast.error(d?.message || String(e), 6000, d?.hint ? { details: d.hint } : undefined)
}

// 容器动作只重跑 docker ps（镜像/卷/网络不受容器启停影响），避免每次动作全量刷新 4 条命令
async function reloadContainers(): Promise<void> {
  try {
    containers.value = parsePs(await dockerExec(PS_CMD))
    lastSync.value = 'docker ps · ' + new Date().toLocaleTimeString()
    // 详情已改弹窗：不再自动选中首个容器（否则进页面/刷新就弹模态）；仅当选中项已不在列表才清空
    const cur = selected.value
    if (cur && !containers.value.some(c => c.id === cur.id)) {
      selected.value = null
    }
  } catch (e) {
    loadError.value = e as DockerError
  }
}

// 动作统一执行器：in-flight 期间锁定按钮（docker stop 有 10s 宽限期），防连点重复下发
// 容器/镜像传 id；卷传 'v:'+name；网络传 'n:'+name；prune 传 'prune:volumes' 等。只要唯一即可。
async function withBusyKey(key: string, fn: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = key
  try { await fn() } finally { busy.value = '' }
}
async function withBusy(target: { id: string }, fn: () => Promise<void>): Promise<void> {
  return withBusyKey(target.id, fn)
}

function doStart(c: ContainerRow) { return withBusy(c, async () => {
  try { await actionExec(startCmd(c.name)); toast.success(`已启动 ${c.name}`); await reloadContainers() } catch (e) { toastErr(e) }
}) }
function doStop(c: ContainerRow) { return withBusy(c, async () => {
  try { await actionExec(stopCmd(c.name)); toast.info(`已停止 ${c.name}`); await reloadContainers() } catch (e) { toastErr(e) }
}) }
function doRestart(c: ContainerRow) { return withBusy(c, async () => {
  try { await actionExec(restartCmd(c.name)); toast.success(`已重启 ${c.name}`); await reloadContainers() } catch (e) { toastErr(e) }
}) }

async function doRemove(c: ContainerRow) {
  const ok = await confirm({
    title: '删除容器',
    message: `容器 ${c.name} 的可写层数据将永久丢失（挂载卷保留）。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusy(c, async () => {
    try { await actionExec(rmCmd(c.name)); toast.success(`已删除 ${c.name}`); await reloadContainers() } catch (e) { toastErr(e) }
  })
}

// 镜像删除后只重跑 docker images（容器/卷/网络不受影响）；失败按 tab 如实上浮
async function reloadImages(): Promise<void> {
  try {
    images.value = parseImages(await dockerExec(IMAGES_CMD))
    partialErrors.value.images = undefined
  } catch (e) {
    partialErrors.value.images = errText(e)
    images.value = []
  }
}

// 卷/网络单 tab 重拉：与 reloadImages 同构，失败不伪装为空列表
async function reloadVolumes(): Promise<void> {
  try {
    volumes.value = parseVolumes(await dockerExec(VOLUMES_CMD))
    partialErrors.value.volumes = undefined
  } catch (e) {
    partialErrors.value.volumes = errText(e)
    volumes.value = []
  }
}
async function reloadNetworks(): Promise<void> {
  try {
    networks.value = parseNetworks(await dockerExec(NETWORKS_CMD))
    partialErrors.value.networks = undefined
  } catch (e) {
    partialErrors.value.networks = errText(e)
    networks.value = []
  }
}

async function doRemoveImage(im: ImageRow) {
  // 按「这一行的引用」删，不按镜像 ID 删：docker rmi <ID> 会把该 ID 上的**所有标签**
  // 一起摘掉，而弹窗只承诺了当前这一行 —— 同一镜像多标签时用户会以为只删了一个，
  // 实际把另一个也卸了。多标签行各自 untag，只有最后一个标签才带走层数据。
  const ref = imageLabel(im)
  const ok = await confirm({
    title: '删除镜像',
    message: `将取消标签 ${ref}（${im.id}）；若它是该镜像上的最后一个标签，层数据一并删除。有容器（含已停止容器）正在使用时会失败。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusyKey(ref, async () => {
    try { await actionExec(rmiCmd(ref)); toast.success(`已删除镜像 ${ref}`); await reloadImages() } catch (e) { toastErr(e) }
  })
}

// ── 卷：列表列展示辅助 + 详情弹窗 + rm/prune 编排 ──

// 卷与网络的标签列共用 docker-core 的 labelHint（此前是两份逐字符相同的实现）

// linkCount 三元态：undefined（老 Docker 不支持）/ 0（未使用，可安全回收）/ >0（在用，rm 会失败）
function volumeUseHint(v: VolumeRow): { text: string; tone: 'ok' | 'warn' | 'muted' } {
  if (typeof v.linkCount !== 'number') return { text: '—', tone: 'muted' }
  if (v.linkCount === 0) return { text: '未使用', tone: 'warn' }
  return { text: `${v.linkCount} 个容器`, tone: 'ok' }
}

// inspect 弹窗开/关 + 子域加载
async function openVolumeDetail(v: VolumeRow) {
  selectedVolume.value = v
  selectedNetwork.value = null
  volInspect.value = null
  inspectError.value = null
  inspectLoading.value = true
  try { volInspect.value = parseVolumeInspect(await dockerExec(volumeInspectCmd(v.name))) }
  catch (e) { inspectError.value = e as DockerError }
  finally { inspectLoading.value = false }
}
async function openNetworkDetail(n: NetworkRow) {
  selectedNetwork.value = n
  selectedVolume.value = null
  netInspect.value = null
  inspectError.value = null
  inspectLoading.value = true
  try { netInspect.value = parseNetworkInspect(await dockerExec(networkInspectCmd(n.name))) }
  catch (e) { inspectError.value = e as DockerError }
  finally { inspectLoading.value = false }
}

async function doRemoveVolume(v: VolumeRow) {
  const ok = await confirm({
    title: '删除卷',
    message: `卷 ${v.name} 中的数据将永久丢失，不可恢复。若仍被容器引用（含已停止容器），删除会失败。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('v:' + v.name, async () => {
    try {
      await actionExec(volumeRmCmd(v.name))
      toast.success(`已删除卷 ${v.name}`)
      if (selectedVolume.value?.name === v.name) selectedVolume.value = null
      await reloadVolumes()
    } catch (e) { toastErr(e) }
  })
}
async function doRemoveNetwork(n: NetworkRow) {
  const ok = await confirm({
    title: '删除网络',
    message: `网络 ${n.name}（${n.driver}）将被移除；若仍有容器连接则删除失败。默认 bridge/host/none 不可删除。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('n:' + n.name, async () => {
    try {
      await actionExec(networkRmCmd(n.name))
      toast.success(`已删除网络 ${n.name}`)
      if (selectedNetwork.value?.name === n.name) selectedNetwork.value = null
      await reloadNetworks()
    } catch (e) { toastErr(e) }
  })
}

// prune 行名回收：docker 逐行打名称，末尾附 Total reclaimed space；取非命名行不算入个数
function countPrunedLines(out: string): number {
  return out.split('\n').map(s => s.trim()).filter(s => s && !/total reclaimed space/i.test(s)).length
}
function parseReclaimed(out: string): string | null {
  const m = out.match(/Total reclaimed space:\s*(\S+)/i)
  return m ? m[1] : null
}

async function doPruneVolumes() {
  const ok = await confirm({
    title: '清理未使用的卷',
    // 口径诚实：Docker 23+（API 1.42 起）`volume prune` 默认只清匿名卷，具名卷要显式
    // --all。这里不发 --all（老版本 docker 不识别该 flag，参数解析阶段就整条失败，
    // 与 VOLUMES_CMD 不用 --verbose 同一个理由），所以把实际范围写在确认框里，
    // 而不是让用户以为一键收走了全部孤儿卷。
    message: '将删除未被任何容器（包括已停止容器）引用的卷，数据不可恢复。注意：Docker 23+ 默认只清匿名卷、保留具名卷（实际范围以本机版本为准），具名卷请用行内「删除卷」逐个处理。',
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:volumes', async () => {
    try {
      const out = await actionExec(volumePruneCmd())
      const reclaimed = parseReclaimed(out)
      const n = countPrunedLines(out)
      toast.success(reclaimed ? `已清理 ${n} 个未使用卷，回收 ${reclaimed}` : `已清理 ${n} 个未使用卷`)
      selectedVolume.value = null
      await reloadVolumes()
    } catch (e) { toastErr(e) }
  })
}
async function doPruneNetworks() {
  const ok = await confirm({
    title: '清理未使用的网络',
    message: '将删除所有未被容器使用的自定义网络（不影响默认 bridge/host/none）。已停止但仍在的容器所接网络不会被删。',
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:networks', async () => {
    try {
      const out = await actionExec(networkPruneCmd())
      const n = countPrunedLines(out)
      toast.success(`已清理 ${n} 个未使用网络`)
      selectedNetwork.value = null
      await reloadNetworks()
    } catch (e) { toastErr(e) }
  })
}

// exec 进终端：命令写进目标会话的交互 shell（自带 PTY，docker exec -it 可直接附着），再请求切到该终端
// 走 withBusy(整行)：建会话 + 等 shell 就绪 + 写命令共三跳、约 0.5s 以上，期间连点会开出
// 第二个终端标签（第一条远端 PTY 也挂在会话列表里没人认领）；同一行的启停/删除一并锁住，
// 免得「exec 附着中」与「docker stop」打到同一个容器。
async function doExec(c: ContainerRow) {
  const s = targetSession.value
  const api = window.electronAPI
  if (!s || !api) { toast.error('未连接目标主机'); return }
  // 先把 connectionId 落成局部量：属性收窄进不了 withBusy 的回调闭包，
  // 直接在回调里用 s.connectionId 会被判成 string | undefined。
  const connId = s.connectionId
  if (!connId) { toast.error('当前会话缺少连接配置，无法新开终端标签'); return }
  await withBusy(c, async () => {
    try {
      // 新开一个终端标签（复用同一连接配置、独立会话），不再把 docker exec 写进用户当前
      // shell —— 那会盖掉他正敲的上下文。connectRemote 会 push 会话并置为 active。
      const newId = await sshStore.connectRemote(connId, 80, 24)
      emit('exec-terminal', newId)
      // 留一点时间让新标签的 RemoteTerminal 挂载、远端登录 shell 打出提示符，再写命令：
      // 早于组件订阅的 PTY 输出会丢帧，早于 shell 就绪则命令可能被登录流程吞掉。
      await new Promise<void>(r => setTimeout(r, 500))
      await api.sshWrite(newId, execCmd(c.name) + '\r')
    } catch (err) {
      toast.error(describeThrownDockerError(err).message)
    }
  })
}

// 目标主机变化：清空重取（不同主机容器集合不同）
watch(targetId, () => {
  selected.value = null
  selectedVolume.value = null
  selectedNetwork.value = null
  logs.value = []
  setFollow(false)
  void loadAll()
}, { immediate: true })
// 任一详情弹窗打开时挂 Esc；三个选中均为 null 时卸监。容器开日志，卷/网络开已在 click 里拉取。
// 同时 watch 选中项 → 确保重选同一行时不重复拉 inspect（已在 openXXXDetail 里取）
function onDetailKey(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (selected.value) selected.value = null
  else if (selectedVolume.value) selectedVolume.value = null
  else if (selectedNetwork.value) selectedNetwork.value = null
}
watch([selected, selectedVolume, selectedNetwork], ([c, v, n]) => {
  if (c) { document.addEventListener('keydown', onDetailKey); setFollow(false); void loadLogs() }
  else if (v || n) { document.addEventListener('keydown', onDetailKey) }
  else { document.removeEventListener('keydown', onDetailKey); setFollow(false) }
})

onUnmounted(() => {
  if (followTimer) clearInterval(followTimer)
  document.removeEventListener('keydown', onDetailKey)
})
</script>

<style scoped>
.docker-view {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* 整页结构画布：走 --canvas-bg（玻璃主题下=transparent，让渐变透上来；详见 DashboardView 同注）。 */
  background: var(--canvas-bg);
}

/* 连接状态条：仿仪表盘，44px / 16px 侧距 / 底部分隔线 */
.page-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
  height: var(--page-head-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
}
.conn-left { display: flex; align-items: center; gap: 8px; min-width: 0; }
.conn-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-tertiary); }
.conn-dot.online { background: var(--status-success-text); }
.conn-dot.offline { background: var(--text-tertiary); }
.conn-name { font-size: var(--page-title-size); font-weight: 600; color: var(--text-primary); }
.conn-host { font-size: 12px; font-weight: 400; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.head-spacer { flex: 1; }
.conn-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.host-sel {
  height: 26px; padding: 0 8px; max-width: 200px;
  background: var(--bg-input); border: 1px solid var(--border-input); border-radius: var(--radius-md);
  color: var(--text-primary); font-size: 12px; font-family: inherit; cursor: pointer;
}
.host-sel option { background: var(--bg-elevated); color: var(--text-primary); }

/* 工具条 */
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
}
.seg { display: inline-flex; background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 2px; gap: 2px; }
.seg-btn {
  border: none; background: transparent; color: var(--text-secondary); font-size: 12px;
  padding: 4px 14px; border-radius: var(--radius-sm);
  transition: background var(--transition-fast), color var(--transition-fast);
}
.seg-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.seg-btn.on { background: var(--bg-elevated); color: var(--accent-primary); }
.search {
  display: inline-flex; align-items: center; gap: 7px; height: 26px; padding: 0 10px; width: 210px;
  background: var(--bg-input); border: 1px solid var(--border-input); border-radius: var(--radius-md); color: var(--text-tertiary);
}
.search input { background: transparent; border: none; color: var(--text-primary); font-size: 12px; flex: 1; }
.search input::placeholder { color: var(--text-tertiary); }
.tb-right { margin-left: auto; }

/* 正文：左列表 + 右详情 */
.body { flex: 1; display: flex; min-height: 0; }
.list-col { flex: 1; display: flex; flex-direction: column; min-width: 0; }

.thead, .crow {
  display: grid;
  grid-template-columns: 12px 84px minmax(0, 1.3fr) minmax(0, 1.1fr) 76px minmax(0, 1fr) minmax(0, 1.1fr) minmax(0, 1fr) 120px;
  gap: 12px;
  align-items: center;
  padding: 0 16px;
}
/* 表头与数据单元格同轴左对齐（操作列 .c-acts 保持居中） */
.thead > span,
.crow > span:not(.st-dot):not(.c-acts) {
  text-align: left;
}
.thead {
  height: 30px; flex-shrink: 0; border-bottom: 1px solid var(--border-subtle);
  font-size: 10px; color: var(--text-tertiary); letter-spacing: .8px; text-transform: uppercase; user-select: none;
}
.list { flex: 1; overflow-y: auto; }
.crow {
  height: 46px; border-bottom: 1px solid var(--divider-primary); cursor: pointer;
  transition: background var(--transition-fast);
}
.crow:hover { background: var(--bg-hover); }
.crow.selected { background: var(--bg-elevated); box-shadow: inset 2px 0 0 var(--accent-primary); }
/* 键盘可达：行可 Tab 聚焦，Enter/Space 选中；焦点环内缩避免溢出相邻网格单元 */
.crow:focus-visible { outline: 2px solid var(--accent-primary); outline-offset: -2px; }
.st-dot { width: 8px; height: 8px; border-radius: 50%; justify-self: center; background: var(--text-tertiary); }
.st-dot.running { background: var(--status-success-text); }
.st-dot.exited { background: var(--status-error-text); }
.st-dot.stopped { background: var(--text-tertiary); }
.c-id { font-size: 11px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-image { min-width: 0; display: flex; align-items: center; gap: 6px; }
.c-image .repo { font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tag {
  font-size: 10px; color: var(--accent-indigo); background: color-mix(in srgb, var(--accent-indigo) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-indigo) 20%, transparent); border-radius: var(--radius-sm); padding: 0 5px; flex: none;
}
.c-state { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-state.running { color: var(--status-success-text); }
.c-state.exited { color: var(--status-error-text); }
.c-state.stopped { color: var(--text-tertiary); }
.c-ports { font-size: 10px; color: var(--accent-cyan); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-ports.none { color: var(--text-tertiary); }
.c-cmd { font-size: 11px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-created { font-size: 11px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-name { font-size: 12px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-acts { display: flex; align-items: center; gap: 2px; }
.list-empty { padding: 24px 16px; color: var(--text-tertiary); font-size: 12px; text-align: center; }
/* 子列表取数失败：不走空态，如实标红上浮（用随主题 status token） */
.list-empty.err-hint { color: var(--status-error-text); }

/* 图标按钮 */
.icon-btn {
  width: 24px; height: 24px; border: none; background: transparent; border-radius: var(--radius-sm); color: var(--text-secondary);
  display: inline-flex; align-items: center; justify-content: center; cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.icon-btn svg { width: 14px; height: 14px; }
.icon-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.icon-btn.start:hover { color: var(--status-success-text); }
.icon-btn.stop:hover { color: var(--status-warn-text); }
.icon-btn:disabled { opacity: .4; cursor: default; }
.icon-btn:disabled:hover { background: transparent; color: var(--text-secondary); }
.icon-btn.start:disabled:hover { color: var(--text-secondary); }
.icon-btn.stop:disabled:hover { color: var(--text-secondary); }
.btn:disabled { opacity: .45; cursor: default; }
/* 危险 ghost：prune 按钮 —— 基线透明 + 危险色描边，hover 不反白而是淡入危险底色 */
.btn-danger-ghost { color: var(--status-error-text); border: 1px solid color-mix(in srgb, var(--status-error-text) 35%, transparent); }
.btn-danger-ghost:hover:not(:disabled) { background: color-mix(in srgb, var(--status-error-text) 10%, transparent); color: var(--status-error-text); }
.spin { animation: dk-rot .8s linear infinite; }
@keyframes dk-rot { to { transform: rotate(360deg); } }

/* 异常优先：非0退出行左侧警示色带（选中时让位于 accent 选中条，不叠加）；用随主题的 status token */
.crow.abnormal:not(.selected) { box-shadow: inset 3px 0 0 var(--status-error-text); }

/* 镜像/卷/网络表：fixed 布局 + colgroup 比例列宽，随容器宽度自适应不横向溢出 */
.ptable { width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 12px; }
.ptable th {
  text-align: left; padding: 8px 16px; color: var(--text-tertiary); font-size: 10px; letter-spacing: .8px;
  text-transform: uppercase; border-bottom: 1px solid var(--border-subtle); font-weight: 400;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  /* 表头粘顶：外层 .list 纵向滚动时表头常驻（border-collapse 下底边会随滚动消失，用 inset 阴影补线） */
  position: sticky; top: 0; z-index: 1; background: var(--bg-base);
  box-shadow: inset 0 -1px 0 var(--border-subtle);
}
.ptable td { padding: 10px 16px; border-bottom: 1px solid var(--divider-primary); color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.ptable td.muted { color: var(--text-secondary); }

/* 卷/网络行可点击：整行 hover + 选中态。表格布局下 td 的 inset shadow 会沿列区断裂，选中条放在首单元格的左边框 */
.ptable.clickable tbody tr { cursor: pointer; transition: background var(--transition-fast); }
.ptable.clickable tbody tr:hover { background: var(--bg-hover); }
.ptable.clickable tbody tr.selected { background: var(--bg-elevated); }
.ptable.clickable tbody tr.selected td:first-child { box-shadow: inset 3px 0 0 var(--accent-primary); }

/* 引用列三元态（在用/未使用/未知）走 status token，不写颜色字面量 */
.use-hint.ok { color: var(--status-success-text); }
.use-hint.warn { color: var(--status-warn-text); }
.use-hint.muted { color: var(--text-tertiary); }
/* 标签列：mono 风格，ellipsis + 原生 title 提示完整列表 */
.label-hint { color: var(--text-secondary); font-size: 11px; }

/* 容器详情弹窗（居中大幅面，日志吃满纵向空间；Teleport 到 body） */
.dt-overlay {
  position: fixed; inset: 0; z-index: 2000; display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, .6); backdrop-filter: blur(2px);
}
.dt-dialog {
  width: min(820px, 100vw - 48px); height: min(78vh, 100vh - 96px);
  display: flex; flex-direction: column; overflow: hidden;
  background: var(--bg-base); border: 1px solid var(--border-default); border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}
.dp-head { padding: 12px 18px; border-bottom: 1px solid var(--divider-primary); display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.dp-name { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.badge { font-size: 10px; padding: 1px 7px; border-radius: 10px; color: var(--text-tertiary); background: var(--bg-input); border: 1px solid var(--border-subtle); }
.badge.running { color: var(--status-success-text); background: color-mix(in srgb, var(--status-success-text) 10%, transparent); border-color: color-mix(in srgb, var(--status-success-text) 25%, transparent); }
.badge.exited { color: var(--status-error-text); background: color-mix(in srgb, var(--status-error-text) 10%, transparent); border-color: color-mix(in srgb, var(--status-error-text) 25%, transparent); }
/* 卷弹窗头部三元徽章：在用=success，未使用=warn（与列表 tone 对齐） */
.badge.ok { color: var(--status-success-text); background: color-mix(in srgb, var(--status-success-text) 10%, transparent); border-color: color-mix(in srgb, var(--status-success-text) 25%, transparent); }
.badge.warn { color: var(--status-warn-text); background: color-mix(in srgb, var(--status-warn-text) 10%, transparent); border-color: color-mix(in srgb, var(--status-warn-text) 25%, transparent); }
/* 网络特性 flag：internal/attachable/ingress/ipv6 小胶囊 */
.flag {
  display: inline-block; margin: 0 4px 2px 0; padding: 1px 6px;
  font-family: var(--font-mono); font-size: 10px; color: var(--accent-indigo);
  background: color-mix(in srgb, var(--accent-indigo) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-indigo) 22%, transparent);
  border-radius: var(--radius-sm);
}
/* 卷/网络详情弹窗：高度自适应（日志固定弹性高的容器弹窗不适合元数据展示），内部纵向滚动 */
.dt-dialog-auto { height: auto; max-height: min(78vh, 100vh - 96px); }
.dp-body-auto { overflow-y: auto; }
.dp-close { margin-left: auto; }
.dp-body { flex: 1; min-height: 0; padding: 16px 18px; display: flex; flex-direction: column; gap: 16px; }
/* 日志区弹性填充剩余高度（dp-body 不再整体滚动，日志内部自滚） */
.logs-section { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.dp-body h4 {
  font-size: 11px; color: var(--text-tertiary); letter-spacing: .8px; text-transform: uppercase; font-weight: 400;
  margin-bottom: 8px; display: flex; align-items: center; gap: 6px;
}
.logs-sub { color: var(--accent-primary); text-transform: none; letter-spacing: 0; font-size: 10px; font-weight: 400; }
.follow-btn {
  margin-left: auto; display: inline-flex; align-items: center; gap: 4px;
  height: 20px; padding: 0 8px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
  background: transparent; color: var(--text-secondary); font-size: 11px; text-transform: none; letter-spacing: 0;
  cursor: pointer; transition: color var(--transition-fast), border-color var(--transition-fast), background var(--transition-fast);
}
.follow-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.follow-btn.on { color: var(--accent-primary); border-color: color-mix(in srgb, var(--accent-primary) 50%, transparent); }
.kv { display: grid; grid-template-columns: 68px 1fr; gap: 5px 10px; font-size: 12px; align-items: baseline; }
.kv .k { color: var(--text-tertiary); text-align: left; }
.kv .v { color: var(--text-primary); font-family: var(--font-mono); word-break: break-all; text-align: left; }
/* 标签/驱动选项 kv 行：key 很长（com.docker.compose.*），加宽左列 */
.kv-wide { grid-template-columns: minmax(140px, 220px) 1fr; }
/* 详情弹窗内嵌小表（IPAM/连接容器）：去除粘顶、缩小 padding，与主表风格一致但不吸顶 */
.ptable.sub { margin-top: 4px; }
.ptable.sub th { position: static; padding: 6px 10px; box-shadow: inset 0 -1px 0 var(--border-subtle); }
.ptable.sub td { padding: 6px 10px; }
.list-empty.sub { padding: 12px 8px; font-size: 11px; }
.logs {
  background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
  font-size: 11px; line-height: 1.6; padding: 10px; flex: 1; min-height: 120px; overflow-y: auto; color: var(--text-secondary);
}
.logs .lrow { white-space: pre-wrap; word-break: break-all; }
.logs .lrow.err { color: var(--status-error-text); }
.logs .lrow.warn { color: var(--status-warn-text); }
.logs-hint { color: var(--text-tertiary); font-style: italic; }
.logs-err {
  background: var(--bg-input); border: 1px solid color-mix(in srgb, var(--status-error-text) 30%, transparent);
  border-radius: var(--radius-md); padding: 10px; flex: 1; min-height: 120px; overflow-y: auto;
  font-size: 11px; color: var(--status-error-text); line-height: 1.6; word-break: break-word;
}

/* 取数错误带：docker 未装 / 无权限 / 会话异常，集中一处如实上浮（对齐仪表盘错误态） */
.err-band {
  display: flex; align-items: flex-start; gap: 10px;
  margin: 12px 16px; padding: 12px 14px; flex-shrink: 0;
  background: color-mix(in srgb, var(--status-error-text) 8%, var(--bg-elevated));
  border: 1px solid color-mix(in srgb, var(--status-error-text) 28%, transparent);
  border-radius: var(--radius-md);
}
.err-band .eb-icon { flex-shrink: 0; width: 18px; height: 18px; color: var(--status-error-text); margin-top: 1px; }
.err-band .eb-text { flex: 1; min-width: 0; }
.err-band .eb-msg { font-size: 13px; color: var(--text-primary); line-height: 1.5; }
.err-band .eb-hint { font-size: 12px; color: var(--text-secondary); margin-top: 4px; line-height: 1.5; }
.load-band {
  display: flex; align-items: center; justify-content: center; gap: 8px; flex: 1;
  color: var(--text-tertiary); font-size: 12px;
}
/* 行内操作图标 hover 语义色：全部走随主题 token（启停=success/warn，删除=error，exec/restart=accent） */
.c-acts .act-exec:hover { color: var(--accent-primary); }
.c-acts .act-restart:hover { color: var(--accent-cyan); }
.c-acts .act-del:hover { color: var(--status-error-text); }

/* 等宽：与其余视图同口径，.mono 非全局工具类，须本组件自带 */
.mono { font-family: var(--font-mono); }

/* 状态栏 */
.statusbar {
  height: 24px; flex-shrink: 0; display: flex; align-items: center; gap: 16px; padding: 0 16px;
  border-top: 1px solid var(--divider-primary); font-size: 11px; color: var(--text-tertiary); user-select: none;
}
.statusbar .mono { color: var(--text-secondary); }
.statusbar .ok { color: var(--status-success-text); }
.statusbar .bad { color: var(--status-error-text); }
.sb-right { margin-left: auto; }

/* 空态 */
.empty {
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--text-tertiary); font-size: 13px;
}
</style>

<!-- 连接选择弹窗：scoped 进不到 Teleport 到 body 的节点，故用非 scoped 块；类名 dk-* 避让仪表盘 dc-* -->
<style>
.dk-overlay {
  position: fixed; inset: 0; z-index: 2000; display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, .6); backdrop-filter: blur(2px);
}
.dk-dialog {
  width: min(420px, 100vw - 48px); max-height: min(460px, 100vh - 96px);
  display: flex; flex-direction: column;
  background: var(--bg-elevated); border: 1px solid var(--border-default); border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}
.dk-header {
  display: flex; align-items: center; justify-content: space-between; padding: 14px 18px;
  border-bottom: 1px solid var(--border-subtle); font-size: 14px; font-weight: 600; color: var(--text-primary); flex-shrink: 0;
}
.dk-close {
  width: 24px; height: 24px; border: none; background: transparent; color: var(--text-tertiary);
  font-size: 18px; line-height: 1; cursor: pointer; border-radius: var(--radius-sm); display: flex; align-items: center; justify-content: center;
}
.dk-close:hover { color: var(--text-primary); background: var(--bg-hover); }
.dk-list { overflow-y: auto; padding: 8px; }
.dk-empty { padding: 28px 18px; text-align: center; color: var(--text-tertiary); font-size: 13px; }
.dk-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: var(--radius-md); cursor: pointer; color: var(--accent-cyan); transition: background var(--transition-fast); }
.dk-item:hover { background: var(--bg-hover); }
.dk-item.disabled { opacity: .5; pointer-events: none; }
.dk-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.dk-name { font-size: 13px; color: var(--text-primary); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dk-host { font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dk-spinner { width: 16px; height: 16px; animation: dk-spin .8s linear infinite; flex-shrink: 0; }
@keyframes dk-spin { to { transform: rotate(360deg); } }
</style>
