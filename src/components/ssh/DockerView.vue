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
          <template v-if="tab === 'images'">
            <button class="btn" title="从仓库拉取镜像（docker pull）" @click="openPull">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
              拉取镜像
            </button>
            <button class="btn" title="从远端 tar 加载镜像（docker load -i）" @click="openImport">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>
              加载镜像
            </button>
            <button
              class="btn btn-danger-ghost"
              :disabled="!!busy || !images.length"
              title="删除悬空镜像（docker image prune -f）：无标签且未被引用的中间层；有标签的镜像不受影响"
              @click="doPruneImages"
            >
              <svg v-if="busy === 'prune:images'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
              <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
              清理悬空
            </button>
            <button
              class="btn btn-danger-ghost"
              :disabled="!!busy || !images.length"
              title="删除所有未被容器引用的镜像（docker image prune -a -f）：带标签镜像也在范围内，删除后需重新 pull"
              @click="doPruneAllImages"
            >
              <svg v-if="busy === 'prune:images-all'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
              <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/></svg>
              清理未使用
            </button>
          </template>
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
                   :class="{ selected: selected?.id === c.id, abnormal: isAbnormal(c) }" :aria-pressed="selected?.id === c.id"
                   @click="selected = c" @keydown.enter.self.prevent="selected = c" @keydown.space.self.prevent="selected = c">
                <span class="st-dot" :class="[c.state, { unhealthy: c.health === 'unhealthy' }]"></span>
                <span class="c-id mono" :title="c.id">{{ c.id }}</span>
                <span class="c-image"><span class="repo" :title="c.repo + ':' + c.tag">{{ c.repo }}</span><span class="tag mono">{{ c.tag }}</span></span>
                <span class="c-cmd mono" :title="c.cmd">{{ c.cmd || '—' }}</span>
                <span class="c-created">{{ c.created || '—' }}</span>
                <span class="c-state" :class="[c.state, { unhealthy: c.health === 'unhealthy' }]" :title="c.stateText">{{ c.stateText }}</span>
                <span class="c-ports mono" :class="{ none: !c.ports }" :title="c.ports || '未映射'">{{ c.ports || '未映射' }}</span>
                <span class="c-name mono" :title="c.name">{{ c.name }}</span>
                <!-- 行内统一操作列（单一操作面）：启停 toggle + exec（仅运行中）+ 重启 + 删除；均为图标，in-flight 禁用防连点 -->
                <span class="c-acts" @click.stop>
                  <button v-if="c.state === 'running'" class="icon-btn stop" title="停止" :disabled="!!busy" @click="doStop(c)">
                    <svg v-if="busy === c.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                    <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
                  </button>
                  <button v-else class="icon-btn start" title="启动" :disabled="!!busy" @click="doStart(c)">
                    <svg v-if="busy === c.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                    <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="6 4 20 12 6 20 6 4"/></svg>
                  </button>
                  <button v-if="c.state === 'running'" class="icon-btn act-exec" title="exec 进入终端（新标签）" :disabled="!!busy" @click="doExec(c)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
                  </button>
                  <button class="icon-btn act-restart" title="重启" :disabled="!!busy" @click="doRestart(c)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
                  </button>
                  <button class="icon-btn act-del" title="删除" :disabled="!!busy" @click="doRemove(c)">
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
              <colgroup><col style="width:38%"><col style="width:18%"><col style="width:14%"><col style="width:16%"><col style="width:14%"></colgroup>
              <thead><tr><th>镜像</th><th>镜像 ID</th><th>内容大小</th><th>引用</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="im in filteredImages" :key="imageLabel(im)">
                  <td class="mono" :title="imageLabel(im)">{{ imageLabel(im) }}</td>
                  <td class="mono muted" :title="im.id">{{ im.id }}</td>
                  <td class="mono">{{ im.size }}</td>
                  <td class="use-hint" :class="imageUseHint(im).tone">{{ imageUseHint(im).text }}</td>
                  <td>
                    <span class="c-acts">
                      <button class="icon-btn act-save" title="导出到远端目录（docker save）" :disabled="!!busy" @click="openExport(im)">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8"/><path d="M2 3h20v5H2z"/><path d="M10 12h4"/></svg>
                      </button>
                      <button class="icon-btn act-del" title="删除镜像" :disabled="!!busy" @click="doRemoveImage(im)">
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
                      <button class="icon-btn act-del" title="删除卷" :disabled="!!busy" @click="doRemoveVolume(v)">
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
                      <button class="icon-btn act-del" :title="isBuiltinNetwork(n.name) ? '内置网络（bridge/host/none）不可删除' : '删除网络'" :disabled="!!busy || isBuiltinNetwork(n.name)" @click="doRemoveNetwork(n)">
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

      <!-- 状态栏：计数必须跟着当前 tab 走。此前这里不分页永远报容器，切到镜像/卷/网络后
           数字对、对象错，比空着更容易误信。 -->
      <div class="statusbar">
        <span>{{ targetName }}</span>
        <span>远程 · SSH</span>
        <span v-if="tab === 'containers'">容器 <span class="mono">{{ containers.length }}</span>（<span class="ok">{{ runningCount }}</span> 运行 / <span class="bad">{{ abnormalCount }}</span> 异常 / <span class="mono">{{ stoppedCount }}</span> 停止）</span>
        <span v-else-if="tab === 'images'">镜像 <span class="mono">{{ images.length }}</span><template v-if="unusedCountKnown && unusedImageCount">（<span class="warn">{{ unusedImageCount }}</span> 未使用<template v-if="danglingCount">，其中 <span class="warn">{{ danglingCount }}</span> 悬空</template>）</template><template v-else-if="danglingCount">（<span class="warn">{{ danglingCount }}</span> 悬空）</template></span>
        <span v-else-if="tab === 'volumes'">卷 <span class="mono">{{ volumes.length }}</span><template v-if="unusedVolumeCount">（<span class="warn">{{ unusedVolumeCount }}</span> 未使用）</template></span>
        <span v-else>网络 <span class="mono">{{ networks.length }}</span><template v-if="builtinNetworkCount">（<span class="mono">{{ builtinNetworkCount }}</span> 内置）</template></span>
        <span v-if="truncated" class="sb-warn" title="单次 SSH 命令输出达到 1MB 上限，列表可能不完整">输出已截断</span>
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
                <span class="dk-host">{{ conn.username }}@{{ conn.host }}:{{ conn.port }}<span v-if="connNote(conn)" class="dk-note" :title="connNote(conn)">{{ connNote(conn) }}</span></span>
              </div>
              <svg v-if="connecting && pendingConnId === conn.id" class="dk-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 容器详情：inspect / stats / logs 三条取数腿在子组件内自持（含轮询与陈旧回包守卫） -->
    <DockerContainerDetail
      v-if="tab === 'containers' && selected"
      :container="selected"
      :session-id="targetId"
      @close="selected = null"
    />

    <!-- 镜像拉取：引用先校验；docker pull 的层下载远超读命令，走 600s 预算 -->
    <Teleport to="body">
      <div v-if="showPull" class="dk-overlay" @click.self="closePull">
        <div class="dk-dialog" role="dialog" aria-modal="true" aria-label="拉取镜像">
          <div class="dk-header">
            <span>拉取镜像</span>
            <button class="dk-close" :disabled="pulling" aria-label="关闭" @click="closePull">×</button>
          </div>
          <form class="pull-body" @submit.prevent="doPull">
            <label class="pull-label" for="docker-pull-ref">镜像引用</label>
            <input
              id="docker-pull-ref"
              ref="pullInputEl"
              v-model="pullRef"
              class="pull-input mono"
              placeholder="nginx:1.27 或 harbor.example.com/app:tag"
              autocomplete="off"
              spellcheck="false"
              :disabled="pulling"
            />
            <div v-if="pullRefInvalid" class="pull-invalid">引用格式不合法：不含空白，且不能以 - 开头</div>
            <div v-if="pullError" class="pull-err">
              <div>{{ pullError.message }}</div>
              <div v-if="pullError.hint" class="pull-hint">{{ pullError.hint }}</div>
            </div>
            <div class="pull-foot">
              <span v-if="pulling" class="pull-elapsed mono">{{ pullElapsed }}s</span>
              <span class="pull-spacer"></span>
              <button type="button" class="btn" :disabled="pulling" @click="closePull">取消</button>
              <button type="submit" class="btn btn-primary" :disabled="pulling || !pullRef.trim() || pullRefInvalid">
                <svg v-if="pulling" class="pull-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                拉取
              </button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <!-- 镜像导出/加载（纯远端）：tar 只落在远端主机，两端都不经过本地 -->
    <Teleport to="body">
      <DockerImageTransferModal
        v-if="transferMode"
        :mode="transferMode"
        :session-id="targetId"
        :image="transferImage"
        :running="transferRunning"
        :error="transferError"
        :elapsed="transferElapsed"
        :initial-dir="lastTransferDir"
        @close="closeTransfer"
        @submit="onTransferSubmit"
      />
    </Teleport>

    <!-- 卷详情弹窗：点行开，自适应高度（最高 78vh）；inspect 一次拉后展示元数据 + 标签 + 驱动选项，底部快捷删除 -->
    <Teleport to="body">
      <div v-if="tab === 'volumes' && selectedVolume" class="dt-overlay" @click.self="selectedVolume = null">
        <div class="dt-dialog dt-dialog-auto" role="dialog" aria-modal="true" :aria-label="'卷详情：' + selectedVolume.name">
          <div class="dp-head">
            <span class="dp-name mono">{{ selectedVolume.name }}</span>
            <span class="badge">{{ selectedVolume.driver }}</span>
            <span v-if="volumeUsageInfo.size" class="badge">{{ volumeUsageInfo.size }}</span>
            <span
              v-if="typeof volumeUsageInfo.refCount === 'number'"
              class="badge"
              :class="volumeUsageInfo.refCount === 0 ? 'warn' : 'ok'"
            >{{ volumeUsageInfo.refCount === 0 ? '未使用' : volumeUsageInfo.refCount + ' 个容器' }}</span>
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
                  <span class="k">引用容器</span><span class="v">{{ volumeUsageInfo.refCount ?? '—' }}</span>
                  <span class="k">占用空间</span><span class="v">{{ volumeUsageInfo.size || '—' }}</span>
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
                <button class="btn btn-sm btn-danger" :disabled="!!busy" @click="doRemoveVolume(selectedVolume)">删除卷</button>
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
                <div v-if="!netInspect.containers.length" class="list-empty sub">当前无容器连接</div>
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
                <button class="btn btn-sm btn-danger" :title="isBuiltinNetwork(selectedNetwork.name) ? '内置网络（bridge/host/none）不可删除' : '删除网络'" :disabled="!!busy || isBuiltinNetwork(selectedNetwork.name)" @click="doRemoveNetwork(selectedNetwork)">删除网络</button>
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
import { formatBytes } from '@/utils/format'
import DockerContainerDetail from './DockerContainerDetail.vue'
import DockerImageTransferModal from './DockerImageTransferModal.vue'
import { connNote, useSSHStore, type SSHConnection } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import {
  AUDIT_ACTION_DOCKER,
  auditEnvironmentFor,
  auditOutcomeForDispatchError,
  auditResultForExit,
  buildRemoteAuditRecord,
  writeRemoteAudit,
  type RemoteAuditConclusion,
} from '@/composables/remoteAudit'
import {
  PS_CMD, IMAGES_CMD, VOLUMES_CMD, VOLUME_USAGE_CMD, NETWORKS_CMD,
  startCmd, stopCmd, restartCmd, rmCmd, rmiCmd, execCmd, pullCmd, imagePruneCmd, imagePruneAllCmd,
  dockerSaveCmd, dockerLoadCmd, saveTempPath,
  volumeRmCmd, networkRmCmd, volumePruneCmd, networkPruneCmd,
  volumeInspectCmd, networkInspectCmd,
  parsePs, parseImages, parseVolumes, parseNetworks, imageLabel, imageHaystack, imageUsageCount,
  parseVolumeUsage, mergeVolumeUsage, volumeUsageSummary, isOutputTruncated,
  labelHint, countPrunedLines, isBuiltinNetwork,
  parseVolumeInspect, parseNetworkInspect,
  dockerErrorFromResult, describeThrownDockerError,
  type ContainerRow, type ImageRow, type VolumeRow, type NetworkRow, type DockerError,
  type VolumeInspect, type NetworkInspect,
} from '@/stores/docker-core'
// exec 三态判据与主进程 ssh-manager 同源（判据源 shared/exec-outcome-core.ts）
import { classifyExit } from '../../../shared/exec-outcome-core'

const props = defineProps<{ active?: boolean }>()
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
// 同步时间按资源分槽：共用一个字段时，刷完镜像再切到卷页仍显示「docker images · 12:00」，
// 读起来像卷列表也是那一刻取的。状态栏只展示当前 tab 那一格。
const syncedAt = ref<Record<TabKey, string>>({ containers: '', images: '', volumes: '', networks: '' })
/** 一次可标多类：loadAll 四类同取时四格写同一个时刻，"已同步" 不再分先后 */
function markSynced(kind: TabKey | TabKey[], label: string): void {
  const at = `${label} · ${new Date().toLocaleTimeString()}`
  const next = { ...syncedAt.value }
  for (const k of Array.isArray(kind) ? kind : [kind]) next[k] = at
  syncedAt.value = next
}
const lastSync = computed(() => syncedAt.value[tab.value])
// 单次 exec 输出上限 1MB：命中标记时列表可能缺行，状态栏必须明示，别让用户
// 把「只看到前 N 个容器」当成主机真实状态。
const truncated = ref(false)
// 其中的 docker ps 单独记账：镜像「引用」列的准确性只取决于容器列表是否完整，
// 不能因为 docker images 被截断就把所有镜像的引用状态都降级成「—」。
const psTruncated = ref(false)

const busy = ref('')
const q = () => query.value.toLowerCase()

// ── 镜像拉取 ──
// 层下载可能远超读命令：主进程把 ssh:exec 超时钳到 600s，这里用满；
// --quiet 只留摘要行，避免进度条把 1MB 输出上限撑爆。
const PULL_TIMEOUT_SEC = 600
const IMAGE_REF_RE = /^[a-zA-Z0-9][a-zA-Z0-9._:@/-]*$/
const showPull = ref(false)
const pullRef = ref('')
const pullInputEl = ref<HTMLInputElement | null>(null)
const pulling = ref(false)
const pullError = ref<DockerError | null>(null)
const pullElapsed = ref(0)
let pullClock: ReturnType<typeof setInterval> | null = null
const pullRefInvalid = computed(() => {
  const ref = pullRef.value.trim()
  return !!ref && (ref.startsWith('-') || !IMAGE_REF_RE.test(ref))
})

// 连接选择 / 镜像拉取共用的 Esc：拉取在途时 closePull 自会拒绝
function onOverlayKey(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (showPull.value) closePull()
  else if (showConnPicker.value) showConnPicker.value = false
}
watch([showConnPicker, showPull], ([picker, pull]) => {
  if (picker || pull) document.addEventListener('keydown', onOverlayKey)
  else document.removeEventListener('keydown', onOverlayKey)
})
onUnmounted(() => document.removeEventListener('keydown', onOverlayKey))

/** 异常优先排序：exited / health=unhealthy → running → stopped，出事容器一眼可见 */
const isAbnormal = (c: ContainerRow): boolean => c.state === 'exited' || c.health === 'unhealthy'
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
const runningCount = computed(() =>
  containers.value.filter(c => c.state === 'running' && !isAbnormal(c)).length)
const stoppedCount = computed(() => containers.value.filter(c => c.state === 'stopped').length)
const abnormalCount = computed(() => containers.value.filter(isAbnormal).length)
const hasContainers = computed(() => containers.value.length > 0)
// 状态栏副标题用的三组细分计数：都是「清理」按钮的作用面，有值才显示（缺数时不报假的 0）。
// 悬空镜像 = 无标签层（列表里显示的是短 ID），正是 image prune 的目标集合。
const danglingCount = computed(() =>
  images.value.filter(im => im.repo === '<none>' || im.tag === '<none>').length)
// 「未使用」= 没有任何容器（含已停止）引用。docker ps 被 1MB 截断时列表不完整，
// 此时一律不给计数/状态（显示「—」），宁可少报也不要把在用的镜像说成可删。
const unusedCountKnown = computed(() => !psTruncated.value)
const unusedImageCount = computed(() =>
  unusedCountKnown.value
    ? images.value.filter(im => imageUsageCount(im, containers.value) === 0).length
    : 0)
function imageUseHint(im: ImageRow): { text: string; tone: 'ok' | 'warn' | 'muted' } {
  if (!unusedCountKnown.value) return { text: '—', tone: 'muted' }
  const n = imageUsageCount(im, containers.value)
  return n > 0 ? { text: `${n} 个容器`, tone: 'ok' } : { text: '未使用', tone: 'warn' }
}
// 未使用卷 = 引用容器数为 0；老版本 docker 不给 Links 时 linkCount 缺失，不凭空断言
const unusedVolumeCount = computed(() => volumes.value.filter(v => v.linkCount === 0).length)
// 内置网络（bridge/host/none）删不掉，计数在状态栏里解释那几枚置灰的删除按钮
const builtinNetworkCount = computed(() => networks.value.filter(n => isBuiltinNetwork(n.name)).length)

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

/** 审计目标在发起时定格：动作飞行途中切宿主，仍记实际执行的那台 */
function dockerAuditTarget(id: string): { targetId: string; environment: 'test' | 'prod' } {
  const s = remoteSessions.value.find(x => x.id === id)
  const conn = s?.connectionId
    ? sshStore.connections.find(c => c.id === s.connectionId)
    : undefined
  return {
    targetId: conn ? `${conn.name} (${conn.username}@${conn.host})` : (s?.shell || s?.name || id),
    environment: auditEnvironmentFor(s, sshStore.connections),
  }
}

function writeDockerAudit(
  target: { targetId: string; environment: 'test' | 'prod'; action: string },
  cmd: string,
  conclusion: RemoteAuditConclusion,
  result: string,
) {
  void writeRemoteAudit(buildRemoteAuditRecord({
    action: target.action,
    conclusion,
    targetId: target.targetId,
    environment: target.environment,
    command: cmd,
    result,
  })).then(ok => {
    if (!ok) toast.warning('审计记录写入失败：本次 Docker 操作未进入审计链')
  })
}

async function dockerExec(
  cmd: string, id = targetId.value, timeoutSec = READ_TIMEOUT_SEC, auditAction?: string,
): Promise<string> {
  const api = window.electronAPI
  if (!id || !api) throw { code: 'NO_SESSION', message: '未选择目标主机', retryable: false } as DockerError
  // 只有变更动作传 auditAction；读路径（列表/日志/inspect）不写审计，
  // 否则一次列表刷新就会往审计链里灌十几条噪声。
  const auditCtx = auditAction
    ? { action: auditAction, ...dockerAuditTarget(id) }
    : null
  const started = Date.now()
  let res
  try {
    res = await api.sshExec(id, cmd, timeoutSec)
  } catch (err) {
    const dErr = describeThrownDockerError(err)
    if (auditCtx) {
      const outcome = auditOutcomeForDispatchError(err, dErr.message, timeoutSec)
      writeDockerAudit(auditCtx, cmd, outcome.conclusion, outcome.result)
    }
    throw dErr
  }
  if (isOutputTruncated(res.stdout, res.stderr)) truncated.value = true
  const durationMs = Date.now() - started
  const dErr = dockerErrorFromResult(res.stderr, res.exitCode, res.stdout)
  if (auditCtx) {
    // sshExec 回包 = 命令已下发；docker 返回非零（容器已不存在等）仍记 executed，
    // 错误码附在 result 里供检索 —— 审计结论不是成败统计。
    const base = auditResultForExit(res.exitCode, durationMs)
    writeDockerAudit(
      auditCtx, cmd, 'executed',
      dErr ? `${base} docker_error=${dErr.code}` : base,
    )
  }
  if (dErr) throw dErr
  return res.stdout
}

/** 变更类动作的入口：只有这一类需要放宽预算，取数路径继续用默认值 */
function actionExec(cmd: string): Promise<string> {
  return dockerExec(cmd, targetId.value, ACTION_TIMEOUT_SEC, AUDIT_ACTION_DOCKER)
}

// 跨宿主竞态守卫：loadAll 内串行+并行共 4 个 await（单条硬超时 15s），用户中途切主机
// 会再触发一轮：旧轮若照常写入，会把 A 的容器盖进已切到 B 的页面（混合宿主数据是运维
// 场景的危险误导），且旧轮 finally 会提前熄灭新轮的转圈。故每轮递增代次，写入前校验。
let loadGen = 0
// loadGen 只覆盖 loadAll；动作后的 reloadX 不经过它。切宿主时递增 epoch，并给每类列表
// 各自一个请求序号：同一资源的并发重拉以最后发起者为准，旧回包（含旧宿主）一律丢弃。
let targetEpoch = 0
const listSeq = { containers: 0, images: 0, volumes: 0, networks: 0 }
type ListKind = keyof typeof listSeq
interface ListPin { id: string; epoch: number; seq: number }

function pinList(kind: ListKind, id: string): ListPin {
  return { id, epoch: targetEpoch, seq: ++listSeq[kind] }
}
function listFresh(kind: ListKind, p: ListPin): boolean {
  return p.epoch === targetEpoch && p.id === targetId.value && p.seq === listSeq[kind]
}

async function loadAll(): Promise<void> {
  const id = targetId.value
  if (!id) return
  const gen = ++loadGen
  const pins = {
    containers: pinList('containers', id),
    images: pinList('images', id),
    volumes: pinList('volumes', id),
    networks: pinList('networks', id),
  }
  loading.value = true
  loadError.value = null
  partialErrors.value = {}
  truncated.value = false
  psTruncated.value = false
  try {
    // 容器为主视图；其失败即整体错误（未装 / 无权限都在此暴露）
    const psRaw = await dockerExec(PS_CMD, id)
    const ps = parsePs(psRaw)
    if (gen !== loadGen || !listFresh('containers', pins.containers)) return
    containers.value = ps
    psTruncated.value = isOutputTruncated(psRaw)
    // 其余尽力而为：单个子命令失败不阻断整页，但要按 tab 如实上浮，不伪装成「没有资源」
    const [im, vo, ne] = await Promise.allSettled([
      dockerExec(IMAGES_CMD, id), dockerExec(VOLUMES_CMD, id), dockerExec(NETWORKS_CMD, id),
    ])
    if (gen !== loadGen) return
    if (listFresh('images', pins.images)) {
      images.value = im.status === 'fulfilled' ? parseImages(im.value) : []
    }
    if (listFresh('volumes', pins.volumes)) {
      volumes.value = vo.status === 'fulfilled' ? parseVolumes(vo.value) : []
      // 用量来自 docker system df -v，可能比列表慢一个量级：不挡首屏，单独回填
      if (vo.status === 'fulfilled') void enrichVolumeUsage(id, pins.volumes)
    }
    if (listFresh('networks', pins.networks)) {
      networks.value = ne.status === 'fulfilled' ? parseNetworks(ne.value) : []
    }
    partialErrors.value = {
      images: listFresh('images', pins.images) && im.status === 'rejected' ? errText(im.reason) : partialErrors.value.images,
      volumes: listFresh('volumes', pins.volumes) && vo.status === 'rejected' ? errText(vo.reason) : partialErrors.value.volumes,
      networks: listFresh('networks', pins.networks) && ne.status === 'rejected' ? errText(ne.reason) : partialErrors.value.networks,
    }
    markSynced(TABS.map(t => t.key), '已同步')
    // 详情已改弹窗：不再自动选中首个容器（否则进页面/刷新就弹模态）；仅当选中项已不在列表才清空
    const cur = selected.value
    if (cur && !containers.value.some(c => c.id === cur.id)) {
      selected.value = null
    }
  } catch (e) {
    if (gen !== loadGen || !listFresh('containers', pins.containers)) return
    loadError.value = e as DockerError
    containers.value = []; images.value = []; volumes.value = []; networks.value = []
    partialErrors.value = {}
    selected.value = null
  } finally {
    if (gen === loadGen) loading.value = false
  }
}

/**
 * 卷用量富集：docker volume ls 的 JSON 没有 UsageData（那是 GET /system/df 专有字段），
 * 只能再跑一条 docker system df -v。失败/超时保持「—」，不影响卷管理主流程；
 * 回包仍须通过列表序号校验——等待期间用户可能已切宿主或重拉，同名卷会跨宿主撞名。
 */
async function enrichVolumeUsage(id: string, pin: ListPin): Promise<void> {
  try {
    const usage = parseVolumeUsage(await dockerExec(VOLUME_USAGE_CMD, id, ACTION_TIMEOUT_SEC))
    if (!listFresh('volumes', pin)) return
    volumes.value = mergeVolumeUsage(volumes.value, usage)
    const cur = selectedVolume.value
    if (cur) {
      const merged = volumes.value.find(v => v.name === cur.name)
      if (merged) selectedVolume.value = merged
    }
  } catch { /* 增强项：保持列显示「—」 */ }
}

// 页面被 v-show 隐藏时组件仍挂载：Teleport 到 body 的弹窗会继续悬浮在别的页面上。
// 隐藏即收起全部弹窗；拉取弹窗不阻塞关闭，在途 pull 由自身 finally 收尾。
watch(() => props.active, (active) => {
  if (active !== false) return
  // 执行中的导出/加载不强收：关面板不等于停掉远端进程；其余情况跟随页面一起收起。
  if (!transferRunning.value) transferMode.value = ''
  showConnPicker.value = false
  showPull.value = false
  stopPullClock()
  selected.value = null
  selectedVolume.value = null
  selectedNetwork.value = null
})

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

// 容器动作只重跑 docker ps（镜像/卷/网络不受容器启停影响），避免每次动作全量刷新 4 条命令。
// 动作走的是 60s 预算：回包时用户完全可能已切到另一台主机，写回前必须校验发起时的宿主与序号。
async function reloadContainers(): Promise<void> {
  const id = targetId.value
  if (!id) return
  const pin = pinList('containers', id)
  try {
    const raw = await dockerExec(PS_CMD, id)
    const next = parsePs(raw)
    if (!listFresh('containers', pin)) return
    containers.value = next
    psTruncated.value = isOutputTruncated(raw)
    markSynced('containers', 'docker ps')
    // 详情已改弹窗：不再自动选中首个容器（否则进页面/刷新就弹模态）；仅当选中项已不在列表才清空
    const cur = selected.value
    if (cur && !containers.value.some(c => c.id === cur.id)) {
      selected.value = null
    }
  } catch (e) {
    if (!listFresh('containers', pin)) return
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
  const id = targetId.value
  if (!id) return
  const pin = pinList('images', id)
  try {
    const next = parseImages(await dockerExec(IMAGES_CMD, id))
    if (!listFresh('images', pin)) return
    images.value = next
    partialErrors.value.images = undefined
    markSynced('images', 'docker images')
  } catch (e) {
    if (!listFresh('images', pin)) return
    partialErrors.value.images = errText(e)
    images.value = []
  }
}

// 卷/网络单 tab 重拉：与 reloadImages 同构，失败不伪装为空列表
async function reloadVolumes(): Promise<void> {
  const id = targetId.value
  if (!id) return
  const pin = pinList('volumes', id)
  try {
    const next = parseVolumes(await dockerExec(VOLUMES_CMD, id))
    if (!listFresh('volumes', pin)) return
    volumes.value = next
    partialErrors.value.volumes = undefined
    markSynced('volumes', 'docker volume ls')
    void enrichVolumeUsage(id, pin)
  } catch (e) {
    if (!listFresh('volumes', pin)) return
    partialErrors.value.volumes = errText(e)
    volumes.value = []
  }
}
async function reloadNetworks(): Promise<void> {
  const id = targetId.value
  if (!id) return
  const pin = pinList('networks', id)
  try {
    const next = parseNetworks(await dockerExec(NETWORKS_CMD, id))
    if (!listFresh('networks', pin)) return
    networks.value = next
    partialErrors.value.networks = undefined
    markSynced('networks', 'docker network ls')
  } catch (e) {
    if (!listFresh('networks', pin)) return
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
    message: `将取消标签 ${ref}（${im.id}）；若它是该镜像上的最后一个标签，层数据一并删除。有容器（含已停止容器）引用该镜像时，删除最后一个标签会失败。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusyKey(ref, async () => {
    try { await actionExec(rmiCmd(ref)); toast.success(`已删除镜像 ${ref}`); await reloadImages() } catch (e) { toastErr(e) }
  })
}

// ── 镜像拉取 / 悬空清理 ──

function startPullClock(): void {
  stopPullClock()
  pullElapsed.value = 0
  pullClock = setInterval(() => { pullElapsed.value += 1 }, 1000)
}
function stopPullClock(): void {
  if (pullClock) { clearInterval(pullClock); pullClock = null }
}

function openPull(): void {
  if (pulling.value) return
  pullRef.value = ''
  pullError.value = null
  showPull.value = true
  void nextTick(() => pullInputEl.value?.focus())
}

// 在途拉取不可关闭：关掉再开会让两条 pull 的 finally 互相踩状态；页面隐藏走强制收起。
function closePull(): void {
  if (pulling.value) return
  showPull.value = false
  stopPullClock()
}

// ── 镜像导出 / 加载（纯远端）──
// tar 只落在远端主机：docker save 的 stdout 是二进制流，走不了 sshExec 的文本通道，
// 所以必须 -o 落盘；加载侧对称地 -i 读盘。两端都不经过本地，没有传输链路。
const TRANSFER_TIMEOUT_SEC = 600

type TransferMode = 'export' | 'import'
const transferMode = ref<TransferMode | ''>('')
const transferImage = ref<ImageRow | null>(null)
const transferRunning = ref(false)
const transferError = ref('')
const transferElapsed = ref(0)
/** 上次落盘目录：重开弹窗直接停在那儿，不用每次从 / 重新找 */
const lastTransferDir = ref('')
let transferClock: ReturnType<typeof setInterval> | null = null

function startTransferClock(): void {
  stopTransferClock()
  transferElapsed.value = 0
  transferClock = setInterval(() => { transferElapsed.value += 1 }, 1000)
}
function stopTransferClock(): void {
  if (transferClock) { clearInterval(transferClock); transferClock = null }
}

/**
 * save/load 专用执行：不复用 dockerExec 的 dockerErrorFromResult 归类。
 * 后者把「有输出但 exit-status 缺失」（exitCode === null）判成 DOCKER_UNKNOWN 并抛出，
 * 而 docker save 写完文件却没回 exit-status 是真会发生的：此时文件可能是完整的，
 * 报失败会让人删掉一份好备份重导一遍。这里原样回包，由调用方按 classifyExit 三态
 * 结合产物大小决定说法；审计照写。
 */
async function transferExec(cmd: string): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
  const api = window.electronAPI
  const id = targetId.value
  if (!id || !api) throw { code: 'NO_SESSION', message: '未选择目标主机', retryable: false } as DockerError
  const ctx = { action: AUDIT_ACTION_DOCKER, ...dockerAuditTarget(id) }
  const started = Date.now()
  try {
    const res = await api.sshExec(id, cmd, TRANSFER_TIMEOUT_SEC)
    writeDockerAudit(ctx, cmd, 'executed', auditResultForExit(res.exitCode, Date.now() - started))
    return res
  } catch (err) {
    const dErr = describeThrownDockerError(err)
    const outcome = auditOutcomeForDispatchError(err, dErr.message, TRANSFER_TIMEOUT_SEC)
    writeDockerAudit(ctx, cmd, outcome.conclusion, outcome.result)
    throw dErr
  }
}

/** 产物大小，仅用于文案；读不到（SFTP 不可用 / 无权限）返回 0 */
async function statSize(id: string, path: string): Promise<number> {
  const api = window.electronAPI
  if (!api || !id) return 0
  try {
    await api.sftpOpen(id)
    return (await api.sftpStat(id, path)).size || 0
  } catch {
    return 0
  }
}

/** 首行非空输出：docker 的报错主体几乎都在第一行，后面是建议动作 */
function firstLine(s: string): string {
  return (s || '').split('\n').map(l => l.trim()).find(Boolean) || ''
}

/** 失败原因：docker 的原话优先；没有就用退出码，unknown 不能写成「退出码 null」 */
function execWhy(res: { stdout: string; stderr: string; exitCode: number | null }): string {
  const detail = firstLine(res.stderr) || firstLine(res.stdout)
  if (detail) return detail
  return classifyExit(res.exitCode) === 'unknown' ? '命令未回退出码' : `退出码 ${res.exitCode}`
}

function transferFail(head: string, why: string, path: string, tmp = ''): DockerError {
  // 超时只关 channel：远端 docker 大概率已死，但写了一半的 tar 会留下，
  // 不说清楚用户会以为目录是干净的。gz 还多一个临时文件要指。
  const where = tmp ? `${path}；临时文件：${tmp}` : path
  return { code: 'DOCKER_EXEC_FAILED', message: `${head}：${why}`, hint: `可能残留半截文件：${where}`, retryable: true }
}

function dirOf(path: string): string {
  const i = path.lastIndexOf('/')
  return i <= 0 ? '/' : path.slice(0, i)
}

function openExport(im: ImageRow): void {
  if (transferRunning.value) return
  transferImage.value = im
  transferError.value = ''
  transferMode.value = 'export'
}

function openImport(): void {
  if (transferRunning.value) return
  transferImage.value = null
  transferError.value = ''
  transferMode.value = 'import'
}

function closeTransfer(): void {
  if (transferRunning.value) return
  transferMode.value = ''
}

async function onTransferSubmit(
  p: { mode: 'export'; ref: string; path: string; gz: boolean } | { mode: 'import'; path: string },
): Promise<void> {
  if (transferRunning.value) return
  const id = targetId.value
  transferRunning.value = true
  transferError.value = ''
  startTransferClock()
  try {
    if (p.mode === 'export') {
      const res = await transferExec(dockerSaveCmd(p.ref, p.path, p.gz))
      const outcome = classifyExit(res.exitCode)
      const tmp = p.gz ? saveTempPath(p.path) : ''
      if (outcome === 'failed') throw transferFail('打包失败', execWhy(res), p.path, tmp)
      // gz 由 `&&` 串在 save 之后、脚本以 $? 退出：exit 0 意味着 save 与 gzip 都成功，
      // 产物必定完整 —— 不再用大小猜（scratch 镜像压完可能只有几百字节）。
      // 大小只进文案：读不到不该把一次成功导出报成失败。
      const size = await statSize(id, p.path)
      if (outcome === 'unknown') {
        toast.warning(size
          ? `命令未回退出码；已生成 ${formatBytes(size)}，建议用加载验证完整性`
          : `命令未回退出码，无法确认 ${p.path} 是否完整，建议用加载验证`)
      } else {
        toast.success(size ? `已导出 ${formatBytes(size)} 到 ${p.path}` : `已导出到 ${p.path}`)
      }
    } else {
      const res = await transferExec(dockerLoadCmd(p.path))
      const outcome = classifyExit(res.exitCode)
      if (outcome === 'failed') throw transferFail('加载失败', execWhy(res), p.path)
      if (outcome === 'unknown') {
        toast.warning('命令未回退出码：镜像可能已载入，请刷新列表核对')
      } else {
        // docker load 成功会打印 Loaded image: <ref>，比自编文案有用
        toast.success(firstLine(res.stderr) || firstLine(res.stdout) || '镜像已加载')
      }
    }
    lastTransferDir.value = dirOf(p.path)
    transferMode.value = ''
    await reloadImages()
  } catch (e) {
    // 错误文案单一来源：这里把 hint（含「可能残留」路径）拼进来，弹窗只负责显示，
    // 不自己再猜一次失败原因。
    const d = e as DockerError
    const base = d?.hint ? `${d.message}；${d.hint}` : (d?.message || String(e))
    // 超时/断链是 reject，拿不到 exitCode，也就拿不到 transferFail 的 hint：
    // 残留路径得在这里补，否则最可能留下半截文件的那条路径反而不提。
    const tail = p.mode !== 'export'
      ? ''
      : p.gz
        ? `可能残留半截文件：${p.path}；临时文件：${saveTempPath(p.path)}`
        : `可能残留半截文件：${p.path}`
    transferError.value = tail && !base.includes(p.path) ? `${base}；${tail}` : base
  } finally {
    transferRunning.value = false
    stopTransferClock()
  }
}

async function doPull(): Promise<void> {
  if (pulling.value) return
  const ref = pullRef.value.trim()
  if (!ref || ref.startsWith('-') || !IMAGE_REF_RE.test(ref)) return
  const id = targetId.value
  pulling.value = true
  pullError.value = null
  startPullClock()
  try {
    await dockerExec(pullCmd(ref), id, PULL_TIMEOUT_SEC, AUDIT_ACTION_DOCKER)
    toast.success(`已拉取 ${ref}`)
    showPull.value = false
    if (targetId.value === id) await reloadImages()
  } catch (e) {
    pullError.value = e as DockerError
  } finally {
    pulling.value = false
    stopPullClock()
  }
}

async function doPruneImages(): Promise<void> {
  const ok = await confirm({
    title: '清理悬空镜像',
    message: '将删除所有悬空镜像（无标签、也未被其它镜像引用的中间层）；有标签的镜像不受影响。',
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:images', async () => {
    try {
      const out = await actionExec(imagePruneCmd())
      const reclaimed = parseReclaimed(out)
      const n = countPrunedLines(out)
      toast.success(reclaimed ? `已清理 ${n} 个悬空镜像，回收 ${reclaimed}` : `已清理 ${n} 个悬空镜像`)
      await reloadImages()
    } catch (e) { toastErr(e) }
  })
}

async function doPruneAllImages(): Promise<void> {
  // 作用面以 Docker 判定为准；列表计数只用于让用户预先知道大概删多少。
  // docker ps 被截断时容器集合不全，计数会高估，此时不给数字。
  const known = unusedCountKnown.value
  const n = unusedImageCount.value
  const k = danglingCount.value
  const scope = known
    ? `当前 ${n} 个未使用镜像${k ? `（其中 ${k} 个悬空）` : ''}。`
    : '容器列表不完整，实际删除数由 Docker 判定。'
  const ok = await confirm({
    title: '清理未使用镜像',
    message: `将删除所有未被任何容器（包括已停止容器）引用的镜像；带标签的镜像也在范围内，删除后需重新 pull，不可恢复。${scope}`,
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:images-all', async () => {
    try {
      const out = await actionExec(imagePruneAllCmd())
      const reclaimed = parseReclaimed(out)
      const pruned = countPrunedLines(out)
      toast.success(reclaimed ? `已清理 ${pruned} 个未使用镜像，回收 ${reclaimed}` : `已清理 ${pruned} 个未使用镜像`)
      await reloadImages()
    } catch (e) { toastErr(e) }
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

// inspect 弹窗开/关 + 子域加载。快速点 A→B 时 A 的慢回包可能后到；关窗、切宿主同理，
// 回包前必须确认「还是同一个弹窗、同一台主机」。
let inspectSeq = 0

async function openVolumeDetail(v: VolumeRow) {
  const id = targetId.value
  const seq = ++inspectSeq
  selectedVolume.value = v
  selectedNetwork.value = null
  volInspect.value = null
  inspectError.value = null
  inspectLoading.value = true
  const fresh = (): boolean =>
    seq === inspectSeq && selectedVolume.value?.name === v.name && targetId.value === id
  try {
    const next = parseVolumeInspect(await dockerExec(volumeInspectCmd(v.name), id))
    if (!fresh()) return
    volInspect.value = next
  } catch (e) {
    if (!fresh()) return
    inspectError.value = e as DockerError
  } finally {
    if (fresh()) inspectLoading.value = false
  }
}
async function openNetworkDetail(n: NetworkRow) {
  const id = targetId.value
  const seq = ++inspectSeq
  selectedNetwork.value = n
  selectedVolume.value = null
  netInspect.value = null
  inspectError.value = null
  inspectLoading.value = true
  const fresh = (): boolean =>
    seq === inspectSeq && selectedNetwork.value?.id === n.id && targetId.value === id
  try {
    const next = parseNetworkInspect(await dockerExec(networkInspectCmd(n.name), id))
    if (!fresh()) return
    netInspect.value = next
  } catch (e) {
    if (!fresh()) return
    inspectError.value = e as DockerError
  } finally {
    if (fresh()) inspectLoading.value = false
  }
}

// 用量来源优先级：inspect 的 UsageData（新版才给）→ 列表富集的 system df；都缺则「—」
const volumeUsageInfo = computed(() =>
  selectedVolume.value
    ? volumeUsageSummary(selectedVolume.value, volInspect.value)
    : { refCount: undefined, size: undefined },
)

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

// prune 行名回收已收敛到 docker-core.countPrunedLines（表头与回收空间行不算个数）
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

// 目标主机变化：清空重取（不同主机容器集合不同）。epoch 先递增，作废所有在途回包。
watch(targetId, () => {
  targetEpoch++
  showPull.value = false
  stopPullClock()
  selected.value = null
  selectedVolume.value = null
  selectedNetwork.value = null
  void loadAll()
}, { immediate: true })
// 任一详情弹窗打开时挂 Esc；三个选中均为 null 时卸监。容器详情的日志/轮询在子组件内自理。
function onDetailKey(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (selected.value) selected.value = null
  else if (selectedVolume.value) selectedVolume.value = null
  else if (selectedNetwork.value) selectedNetwork.value = null
}
watch([selected, selectedVolume, selectedNetwork], ([c, v, n]) => {
  if (c || v || n) document.addEventListener('keydown', onDetailKey)
  else document.removeEventListener('keydown', onDetailKey)
})

onUnmounted(() => {
  stopPullClock()
  stopTransferClock()
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
  /* 整页结构画布：走 --canvas-bg（默认=--bg-base；详见 DashboardView 同注）。 */
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
.tb-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }

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
/* healthcheck 失败优先于 running 绿：容器还在跑，但已经在报错 */
.st-dot.unhealthy { background: var(--status-error-text); }
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
.c-state.unhealthy { color: var(--status-error-text); }
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
.icon-btn.act-save:hover { color: var(--accent-primary); }
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

/* 镜像/卷/网络表：fixed 布局 + colgroup 比例列宽（基础样式在 docker-modal.css，弹窗内共用） */

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
.statusbar .sb-warn { color: var(--status-warn-text); }
/* 悬空/未使用计数：可回收的量，用 warn 色与上方 danger-ghost 清理按钮呼应 */
.statusbar .warn { color: var(--status-warn-text); }
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
.dk-close:disabled { opacity: .5; cursor: default; background: transparent; color: var(--text-tertiary); }

/* 拉取镜像：引用输入 + 校验/错误提示 + 底部操作；层下载耗时长，等宽计时给出进度感 */
.pull-body { display: flex; flex-direction: column; gap: 8px; padding: 16px 18px 18px; overflow-y: auto; }
.pull-label { font-size: 12px; color: var(--text-secondary); }
.pull-input {
  width: 100%; padding: 8px 10px; font-size: 12px; box-sizing: border-box;
  background: var(--bg-primary); color: var(--text-primary);
  border: 1px solid var(--border-default); border-radius: var(--radius-md);
}
.pull-input:focus { outline: none; border-color: var(--accent-primary); }
.pull-input:disabled { opacity: .6; }
.pull-invalid { font-size: 11px; color: var(--status-warn-text); }
.pull-err {
  padding: 8px 10px; font-size: 12px; line-height: 1.5; color: var(--text-primary);
  background: color-mix(in srgb, var(--status-error-text) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--status-error-text) 28%, transparent);
  border-radius: var(--radius-md);
}
.pull-hint { margin-top: 4px; color: var(--text-secondary); }
.pull-foot { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.pull-elapsed { font-size: 11px; color: var(--text-tertiary); }
.pull-spacer { flex: 1; }
.pull-spin { animation: dk-spin .8s linear infinite; }

.dk-list { overflow-y: auto; padding: 8px; }
.dk-empty { padding: 28px 18px; text-align: center; color: var(--text-tertiary); font-size: 13px; }
.dk-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: var(--radius-md); cursor: pointer; color: var(--accent-cyan); transition: background var(--transition-fast); }
.dk-item:hover { background: var(--bg-hover); }
.dk-item.disabled { opacity: .5; pointer-events: none; }
.dk-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.dk-name { font-size: 13px; color: var(--text-primary); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dk-host { font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 连接选择器上的备注：跟在主机行后，同仪表盘快连卡片口径 */
.dk-note { margin-left: 6px; font-family: var(--font-sans); color: var(--text-secondary); }
.dk-spinner { width: 16px; height: 16px; animation: dk-spin .8s linear infinite; flex-shrink: 0; }
@keyframes dk-spin { to { transform: rotate(360deg); } }
</style>
