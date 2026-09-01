<template>
  <div class="sftp-view">
    <!-- 会话状态栏 -->
    <div class="sftp-session-bar">
      <div class="session-left">
        <div class="session-dot" :class="store.online ? 'online' : 'offline'"></div>
        <span class="session-name">{{ store.online ? store.connectionName : '未连接远程' }}</span>
        <span v-if="store.online" class="session-host">{{ store.connectionHost }}</span>
        <span v-else class="session-host">仅本地模式</span>
      </div>
      <div class="session-right">
        <!-- 传输任务按钮 -->
        <button class="session-btn transfer" :class="{ active: showTransferList }" @click="showTransferList = !showTransferList">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
          <span v-if="store.transfers.length > 0" class="transfer-badge">{{ store.transfers.length }}</span>
          传输任务
        </button>
        <button v-if="!store.online" class="session-btn connect" @click="showConnPicker = true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
          连接远程
        </button>
        <template v-else>
          <button class="session-btn" @click="showConnPicker = true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            切换
          </button>
          <button class="session-btn" @click="handleDisconnect">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
            断开
          </button>
        </template>
      </div>
    </div>

    <!-- 双栏 -->
    <div class="sftp-panes">
      <SftpPane
        side="local"
        @transfer="onTransfer"
        @context-menu="onContextMenu"
        @open-file="onOpenFile"
      />
      <SftpPane
        side="remote"
        @transfer="onTransfer"
        @context-menu="onContextMenu"
        @open-file="onOpenFile"
      />
    </div>

    <!-- 传输任务列表弹窗 -->
    <div v-if="showTransferList" class="transfer-overlay" @click.self="showTransferList = false">
      <div class="transfer-panel">
        <div class="transfer-panel-header">
          <span class="transfer-panel-title">传输任务</span>
          <div class="transfer-panel-actions">
            <span v-if="store.transfers.length > 0" class="transfer-count">{{ store.activeTransfers.length }} 项进行中</span>
            <button v-if="store.transfers.length > 0" class="transfer-clear-btn" @click="clearCompleted">清除已完成</button>
            <button class="transfer-panel-close" @click="showTransferList = false">×</button>
          </div>
        </div>
        <div class="transfer-panel-body">
          <div v-if="store.transfers.length === 0" class="transfer-empty">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="48" height="48"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
            <p>暂无传输任务</p>
            <p class="transfer-empty-hint">断开连接后记录将自动清空</p>
          </div>
          <div v-else class="transfer-list">
            <!-- 进行中的任务 -->
            <div v-if="activeTransfers.length > 0" class="transfer-section">
              <div class="transfer-section-title">
                <span class="transfer-section-label">进行中</span>
                <span class="transfer-section-count">{{ activeTransfers.length }}</span>
              </div>
              <div
                v-for="t in activeTransfers"
                :key="t.id"
                class="transfer-list-item"
                :class="t.status"
                @contextmenu.prevent="onTransferContext($event, t)"
              >
              <div class="tl-icon" :class="t.direction">
                <svg v-if="t.direction === 'up'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
              </div>
              <div class="tl-info">
                <div class="tl-name" :title="t.name">{{ t.name }}</div>
                <div class="tl-path">
                  <span class="tl-path-from" :title="t.from">{{ truncatePath(t.from) }}</span>
                  <svg class="tl-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="10" height="10"><polyline points="9 18 15 12 9 6"/></svg>
                  <span class="tl-path-to" :title="t.to">{{ truncatePath(t.to) }}</span>
                </div>
                <div class="tl-meta">
                  <span v-if="t.status === 'running'" class="tl-speed">{{ formatSpeed(t) }} · {{ formatSize(t.transferred) }} / {{ formatSize(t.total) }} · {{ formatElapsed(t) }}</span>
                  <span v-else-if="t.status === 'paused'" class="tl-paused">已暂停 · {{ formatSize(t.transferred) }} / {{ formatSize(t.total) }} · {{ formatElapsed(t) }}</span>
                  <span v-else-if="t.status === 'done'" class="tl-done">完成 · {{ formatSize(t.total) }} · {{ formatDuration(t) }}</span>
                  <span v-else-if="t.status === 'error'" class="tl-error" :title="t.error">失败: {{ t.error }}</span>
                  <span v-else-if="t.status === 'cancelled'" class="tl-cancelled">已取消 · {{ formatSize(t.transferred) }} / {{ formatSize(t.total) }}</span>
                  <span v-else class="tl-pending">等待中 · {{ formatSize(t.total) }}</span>
                </div>
                <div class="tl-progress-bar">
                  <div
                    v-if="isIndeterminate(t)"
                    class="tl-progress-fill indeterminate"
                  ></div>
                  <div
                    v-else
                    class="tl-progress-fill"
                    :class="t.status"
                    :style="{ width: progressPercent(t) + '%' }"
                  ></div>
                </div>
              </div>
              <div class="tl-right">
                <span v-if="isIndeterminate(t)" class="tl-percent-indeterminate">复制中</span>
                <span v-else-if="t.status === 'running'" class="tl-percent">{{ progressPercent(t) }}%</span>
                <span v-else-if="t.status === 'paused'" class="tl-percent-paused">已暂停</span>
                <span v-else-if="t.status === 'done'" class="tl-percent-success">{{ progressPercent(t) }}%</span>
                <span v-else-if="t.status === 'error'" class="tl-percent-error">失败</span>
                <span v-else-if="t.status === 'cancelled'" class="tl-percent-cancelled">已取消</span>
                <span v-else class="tl-percent-pending">—</span>
              </div>
              <button
                v-if="t.status === 'running' || t.status === 'pending'"
                class="tl-action tl-cancel"
                :title="canPauseTransfer(t) ? '暂停' : '取消'"
                @click.stop="canPauseTransfer(t) ? store.pauseTransfer(t.id) : store.cancelTransfer(t.id)"
              >
                <!-- F-6 修复：copy 类项不支持暂停，只给取消 -->
                <svg v-if="canPauseTransfer(t)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
              <button
                v-else-if="t.status === 'paused'"
                class="tl-action tl-resume"
                title="继续"
                @click.stop="store.resumeTransfer(t.id)"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              </button>
              <button
                v-else
                class="tl-action tl-remove"
                title="移除"
                @click.stop="store.removeTransfer(t.id)"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/></svg>
              </button>
            </div>
            </div>
          </div>
          <!-- 已完成的记录 -->
          <div v-if="completedTransfers.length > 0" class="transfer-section">
            <div class="transfer-section-title">
              <span class="transfer-section-label">已完成</span>
              <span class="transfer-section-count">{{ completedTransfers.length }}</span>
            </div>
            <div
              v-for="t in completedTransfers"
              :key="t.id"
              class="transfer-list-item"
              :class="t.status"
              @contextmenu.prevent="onTransferContext($event, t)"
            >
              <div class="tl-icon" :class="t.direction">
                <svg v-if="t.direction === 'up'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
              </div>
              <div class="tl-info">
                <div class="tl-name" :title="t.name">{{ t.name }}</div>
                <div class="tl-path">
                  <span class="tl-path-from" :title="t.from">{{ truncatePath(t.from) }}</span>
                  <svg class="tl-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="10" height="10"><polyline points="9 18 15 12 9 6"/></svg>
                  <span class="tl-path-to" :title="t.to">{{ truncatePath(t.to) }}</span>
                </div>
                <div class="tl-meta">
                  <span v-if="t.status === 'done'" class="tl-done">完成 · {{ formatSize(t.total) }} · {{ formatDuration(t) }}</span>
                  <span v-else-if="t.status === 'error'" class="tl-error" :title="t.error">失败: {{ t.error }}</span>
                  <span v-else-if="t.status === 'cancelled'" class="tl-cancelled">已取消 · {{ formatSize(t.transferred) }} / {{ formatSize(t.total) }}</span>
                </div>
                <div class="tl-progress-bar">
                  <div
                    class="tl-progress-fill"
                    :class="t.status"
                    :style="{ width: '100%' }"
                  ></div>
                </div>
              </div>
              <div class="tl-right">
                <span v-if="t.status === 'done'" class="tl-percent-success">完成</span>
                <span v-else-if="t.status === 'error'" class="tl-percent-error">失败</span>
                <span v-else class="tl-percent-cancelled">已取消</span>
              </div>
              <button
                class="tl-action tl-remove"
                title="移除"
                @click.stop="store.removeTransfer(t.id)"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/></svg>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 传输任务右键菜单 -->
    <div
      v-if="transferCtxMenu.visible"
      class="transfer-ctx-menu"
      :style="{ left: transferCtxMenu.x + 'px', top: transferCtxMenu.y + 'px' }"
      @click.stop
      @contextmenu.prevent
    >
      <!-- F-6 修复：copy 类任务不支持暂停，右键菜单同样隐藏"暂停传输" -->
      <div v-if="transferCtxMenu.item && canPauseTransfer(transferCtxMenu.item)" class="ctx-item" @click="store.pauseTransfer(transferCtxMenu.item.id); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
        暂停传输
      </div>
      <div v-if="transferCtxMenu.item && transferCtxMenu.item.status === 'paused'" class="ctx-item" @click="store.resumeTransfer(transferCtxMenu.item.id); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        继续传输
      </div>
      <div v-if="transferCtxMenu.item && transferCtxMenu.item.status === 'running'" class="ctx-item" @click="store.cancelTransfer(transferCtxMenu.item.id); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        取消传输
      </div>
      <div v-if="transferCtxMenu.item && transferCtxMenu.item.status === 'pending'" class="ctx-item" @click="store.cancelTransfer(transferCtxMenu.item.id); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        取消排队
      </div>
      <div v-if="transferCtxMenu.item && (transferCtxMenu.item.status === 'done' || transferCtxMenu.item.status === 'error' || transferCtxMenu.item.status === 'cancelled')" class="ctx-item" @click="store.removeTransfer(transferCtxMenu.item.id); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/></svg>
        移除记录
      </div>
      <div v-if="transferCtxMenu.item" class="ctx-sep"></div>
      <div v-if="transferCtxMenu.item" class="ctx-item" @click="copyTransferPath(transferCtxMenu.item); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        复制源路径
      </div>
      <div v-if="transferCtxMenu.item" class="ctx-item" @click="copyTransferDestPath(transferCtxMenu.item); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        复制目标路径
      </div>
      <div v-if="transferCtxMenu.item && transferCtxMenu.item.status === 'error'" class="ctx-sep"></div>
      <div v-if="transferCtxMenu.item && transferCtxMenu.item.status === 'error'" class="ctx-item" @click="retryTransfer(transferCtxMenu.item); transferCtxMenu.visible = false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        重试
      </div>
    </div>

    <!-- 连接选择模态 -->
    <div v-if="showConnPicker" class="conn-overlay" @click.self="showConnPicker = false">
      <div class="conn-dialog">
        <div class="conn-dialog-header">
          <span>选择 SSH 连接</span>
          <button class="conn-close" @click="showConnPicker = false">×</button>
        </div>
        <div class="conn-list">
          <div v-if="sshStore.connections.length === 0" class="conn-empty">
            暂无 SSH 连接配置,请先在"远程管理"中新建连接
          </div>
          <div
            v-for="conn in sshStore.connections"
            :key="conn.id"
            class="conn-item"
            :class="{ disabled: connecting }"
            @click="handleConnect(conn)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
            <div class="conn-item-info">
              <div class="conn-item-name">{{ conn.name }}</div>
              <div class="conn-item-host">{{ conn.username }}@{{ conn.host }}:{{ conn.port }}</div>
            </div>
            <svg v-if="connecting && pendingConnId === conn.id" class="spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
          </div>
        </div>
      </div>
    </div>

    <!-- 右键菜单 -->
    <div
      v-if="ctxMenu.visible"
      class="ctx-menu"
      :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
      @click="ctxMenu.visible = false"
      @contextmenu.prevent
    >
      <div class="ctx-item" @click="handleCtxAction('open')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        打开
      </div>
      <div v-if="ctxMenu.entry" class="ctx-item" @click="handleCtxAction('transfer')">
        <svg v-if="ctxMenu.side === 'local'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
        {{ ctxMenu.side === 'local' ? '上传' : '下载' }}
      </div>
      <div v-if="ctxMenu.entry" class="ctx-item" @click="handleCtxAction('copy')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        复制
      </div>
      <div v-if="canPaste" class="ctx-item" @click="handleCtxAction('paste')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>
        粘贴
      </div>
      <div class="ctx-sep"></div>
      <div class="ctx-item" @click="handleCtxAction('newFolder')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>
        新建文件夹
      </div>
      <template v-if="ctxMenu.entry">
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="handleCtxAction('zip')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>
          压缩
        </div>
        <div v-if="isZipFile(ctxMenu.entry)" class="ctx-item" @click="handleCtxAction('unzip')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/><path d="M12 12v5" stroke-dasharray="2 2"/></svg>
          解压
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="handleCtxAction('copyPath')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          复制路径
        </div>
        <div class="ctx-item" @click="handleCtxAction('rename')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          重命名
        </div>
        <div class="ctx-item danger" @click="handleCtxAction('delete')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          删除
        </div>
      </template>
    </div>

    <!-- 重命名/新建对话框 -->
    <div v-if="renameDialog.visible" class="rename-overlay" @click.self="renameDialog.visible = false">
      <div class="rename-dialog">
        <div class="rename-title">{{ renameDialog.isNew ? '新建文件夹' : '重命名' }}</div>
        <input
          ref="renameInput"
          v-model="renameDialog.name"
          class="rename-input"
          @keydown.enter="confirmRename"
          @keydown.escape="renameDialog.visible = false"
        />
        <div class="rename-actions">
          <button class="rename-btn cancel" @click="renameDialog.visible = false">取消</button>
          <button class="rename-btn confirm" @click="confirmRename">确定</button>
        </div>
      </div>
    </div>

    <!-- 压缩包文件名对话框 -->
    <div v-if="zipDialog.visible" class="rename-overlay" @click.self="zipDialog.visible = false">
      <div class="rename-dialog">
        <div class="rename-title">压缩为 zip</div>
        <input
          ref="zipInput"
          v-model="zipDialog.name"
          class="rename-input"
          @keydown.enter="confirmZip"
          @keydown.escape="zipDialog.visible = false"
        />
        <div class="rename-actions">
          <button class="rename-btn cancel" @click="zipDialog.visible = false">取消</button>
          <button class="rename-btn confirm" @click="confirmZip">确定</button>
        </div>
      </div>
    </div>

    <!-- 弹窗文件编辑器 -->
    <SftpFileEditor
      :visible="fileEditor.visible"
      :side="fileEditor.side"
      :path="fileEditor.path"
      :name="fileEditor.name"
      :ssh-session-id="store.sshSessionId"
      @close="fileEditor.visible = false"
      @saved="onFileSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'
import SftpPane from './SftpPane.vue'
import SftpFileEditor from './SftpFileEditor.vue'
import { useSftpStore, type TransferItem } from '@/stores/sftp'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { isPreviewable } from '@/utils/language'
import { toFriendlyMessage } from '@/api/client'

const store = useSftpStore()
const sshStore = useSSHStore()
const toast = useToastStore()
const { confirm } = useConfirm()

const showConnPicker = ref(false)
const connecting = ref(false)
const pendingConnId = ref('')
const showTransferList = ref(false)

// 传输任务计算属性
const activeTransfers = computed(() =>
  store.transfers.filter(t => t.status === 'running' || t.status === 'paused' || t.status === 'pending')
)
const completedTransfers = computed(() =>
  store.transfers.filter(t => t.status === 'done' || t.status === 'error' || t.status === 'cancelled')
)

// 传输任务右键菜单状态
const transferCtxMenu = ref({
  visible: false,
  x: 0,
  y: 0,
  item: null as TransferItem | null
})

// ─── 传输任务辅助函数 ─────────────────
function progressPercent(t: TransferItem): number {
  if (!t.total) return 0
  return Math.min(100, Math.round((t.transferred / t.total) * 100))
}

function isIndeterminate(t: TransferItem): boolean {
  return t.status === 'running' && !t.total
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function formatSpeed(t: TransferItem): string {
  if (!t.startTime) return ''
  const elapsed = (Date.now() - t.startTime) / 1000
  if (elapsed <= 0) return ''
  const speed = t.transferred / elapsed
  const speedText = speed < 1024
    ? `${Math.round(speed)} B/s`
    : speed < 1024 * 1024
      ? `${(speed / 1024).toFixed(1)} KB/s`
      : `${(speed / 1024 / 1024).toFixed(1)} MB/s`
  const remaining = t.total > t.transferred && speed > 0
    ? Math.ceil((t.total - t.transferred) / speed)
    : 0
  if (remaining > 0) return `${speedText} · 剩余 ${remaining}s`
  return speedText
}

function formatDuration(t: TransferItem): string {
  if (!t.startTime || !t.endTime) return '—'
  const ms = t.endTime - t.startTime
  if (ms < 1000) return `${ms}ms`
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  return `${m}m ${rs}s`
}

/** 格式化已运行时间（用于进行中和暂停状态） */
function formatElapsed(t: TransferItem): string {
  if (!t.startTime) return '—'
  const ms = Date.now() - t.startTime
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  if (m < 60) return `${m}m ${rs}s`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return `${h}h ${rm}m`
}

function truncatePath(path: string): string {
  if (path.length <= 30) return path
  const parts = path.split('/')
  if (parts.length > 3) {
    return `.../${parts.slice(-2).join('/')}`
  }
  return '...' + path.slice(-27)
}

function clearCompleted() {
  store.clearCompleted()
}

/** F-6 修复：仅 upload/download 支持暂停；copy 类（同侧复制）不支持暂停/恢复 */
function canPauseTransfer(t: TransferItem): boolean {
  return t.status === 'running' && (t.kind === 'upload' || t.kind === 'download')
}

function onTransferContext(event: MouseEvent, item: TransferItem) {
  // C32 修复：打开传输右键菜单时先隐藏文件右键菜单（原来互不 dismiss）
  ctxMenu.visible = false
  transferCtxMenu.value = {
    visible: true,
    x: event.clientX,
    y: event.clientY,
    item
  }
}

function copyTransferPath(item: TransferItem) {
  navigator.clipboard.writeText(item.from).then(() => {
    toast.success('已复制源路径')
  }).catch(() => {
    toast.error('复制失败')
  })
}

function copyTransferDestPath(item: TransferItem) {
  navigator.clipboard.writeText(item.to).then(() => {
    toast.success('已复制目标路径')
  }).catch(() => {
    toast.error('复制失败')
  })
}

function retryTransfer(item: TransferItem) {
  // F-6 修复：原实现只取 item.from 重发、目标推导为当前 cwd —
  // 用户换过目录就传错位置。改为 store 按 kind 用原始 from → to 精确重发
  store.retryTransfer(item.id)
}

// ─── 进度订阅 ─────────────────────────
let unsubProgress: (() => void) | null = null
let unsubStalled: (() => void) | null = null
let unsubClosed: (() => void) | null = null
let unsubSshClose: (() => void) | null = null

onMounted(() => {
  // B9 修复：浏览器（非 Electron）环境无 electronAPI —
  // 原 `window.electronAPI!` 非空断言在此处 onMounted 即 TypeError
  const api = window.electronAPI
  if (!api) {
    console.warn('[SftpView] 非 Electron 环境，SFTP 功能不可用')
    return
  }
  unsubProgress = api.onSftpProgress((data) => {
    store.applyProgress(data)
  })
  unsubStalled = api.onSftpStalled((data) => {
    const item = store.transfers.find(t => t.id === data.id)
    const name = item?.name || '文件'
    toast.warning(`"${name}" 传输停滞，正在尝试自动续传…`)
  })
  unsubClosed = api.onSftpClosed((data) => {
    if (data.id === store.sshSessionId) {
      store.disconnect()
      toast.warning('远程 SFTP 连接已断开')
    }
  })
  unsubSshClose = api.onSshClose((data) => {
    if (data.id === store.sshSessionId) {
      store.disconnect()
      toast.warning('远程 SFTP 连接已断开')
    }
  })
  document.addEventListener('click', dismissCtxMenu)
  if (!store.localCwd) store.initLocalHome()
})

onUnmounted(() => {
  unsubProgress?.()
  unsubStalled?.()
  unsubClosed?.()
  unsubSshClose?.()
  document.removeEventListener('click', dismissCtxMenu)
  showConnPicker.value = false
  ctxMenu.visible = false
  renameDialog.visible = false
})

// ─── 连接管理 ─────────────────────────
async function handleConnect(conn: SSHConnectionRecord): Promise<void> {
  if (connecting.value) return
  connecting.value = true
  pendingConnId.value = conn.id
  try {
    await store.connect(conn)
    showConnPicker.value = false
    toast.success(`已连接 ${conn.name}`)
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '连接失败'))
  } finally {
    connecting.value = false
    pendingConnId.value = ''
  }
}

async function handleDisconnect(): Promise<void> {
  const ok = await confirm({
    title: '断开 SFTP 连接',
    message: `确认断开与「${store.connectionName}」的 SFTP 连接?`,
    confirmText: '断开',
    danger: true,
  })
  if (ok) {
    await store.disconnect()
    toast.success('已断开')
  }
}

// ─── 跨栏传输(由 SftpPane drop 触发) ───
async function onTransfer(payload: { side: 'local' | 'remote'; paths: string[] }): Promise<void> {
  // 覆盖确认：检查目标路径是否已存在同名文件
  const existingFiles: string[] = []
  if (payload.side === 'local') {
    // 上传：检查远程目标
    for (const lp of payload.paths) {
      const name = lp.split('/').pop()?.split('\\').pop() ?? lp
      const remotePath = store.remoteCwd === '/' ? '/' + name : store.remoteCwd + '/' + name
      const exists = await store.remotePathExists(remotePath)
      if (exists) existingFiles.push(name)
    }
  } else {
    // 下载：检查本地目标
    for (const rp of payload.paths) {
      const name = rp.split('/').pop() ?? rp
      const localPath = store.localCwd + '/' + name
      const exists = await store.localPathExists(localPath)
      if (exists) existingFiles.push(name)
    }
  }

  // 如果有文件已存在，弹出覆盖确认
  if (existingFiles.length > 0) {
    const fileList = existingFiles.slice(0, 3).join('、')
    const more = existingFiles.length > 3 ? ` 等 ${existingFiles.length} 个文件` : ''
    const ok = await confirm({
      title: '文件已存在',
      message: `目标位置已存在同名文件「${fileList}${more}」，是否覆盖？`,
      confirmText: '覆盖',
      danger: true,
    })
    if (!ok) return
  }

  if (payload.side === 'local') {
    // 本地 → 远程: 上传
    store.uploadFiles(payload.paths)
  } else {
    // 远程 → 本地: 下载
    store.downloadFiles(payload.paths)
  }
}

// ─── 右键菜单 ─────────────────────────
interface AnyEntry { name: string; path: string; isDirectory: boolean }
const ctxMenu = reactive({
  visible: false,
  x: 0, y: 0,
  side: '' as 'local' | 'remote',
  entry: null as AnyEntry | null,
})

function onContextMenu(e: MouseEvent, entry: AnyEntry | null, side: 'local' | 'remote'): void {
  const estimatedHeight = 200
  const viewportHeight = window.innerHeight
  const y = e.clientY + estimatedHeight > viewportHeight
    ? e.clientY - estimatedHeight
    : e.clientY
  // C32 修复：打开文件右键菜单时先隐藏传输右键菜单（原来互不 dismiss）
  transferCtxMenu.value.visible = false
  ctxMenu.visible = true
  ctxMenu.x = e.clientX
  ctxMenu.y = y
  ctxMenu.entry = entry
  ctxMenu.side = side
}

/** 剪贴板中有来自同侧的数据时才可粘贴 */
const canPaste = computed(() => {
  if (!store.clipboard) return false
  return store.clipboard.side === ctxMenu.side
})

async function handleCtxAction(action: string): Promise<void> {
  const entry = ctxMenu.entry
  const side = ctxMenu.side
  ctxMenu.visible = false

  if (action === 'open') {
    if (entry?.isDirectory) {
      if (side === 'local') await store.loadLocal(entry.path)
      else await store.loadRemote(entry.path)
    } else if (entry && isPreviewable(entry.name)) {
      openFileEditor(side, entry)
    }
    return
  }

  if (action === 'transfer' && entry) {
    // 收集选中项路径（如果当前项在选中集合中，传输所有选中项；否则只传输当前项）
    const paths = getCtxPaths(side, entry)
    if (side === 'local') store.uploadFiles(paths)
    else store.downloadFiles(paths)
    return
  }

  if (action === 'copy' && entry) {
    const paths = getCtxPaths(side, entry)
    store.copyToClipboard(side, paths)
    toast.success(`已复制 ${paths.length} 项`)
    return
  }

  if (action === 'paste') {
    const cb = store.clipboard
    if (!cb) return
    // 确定目标目录：右键在目录上则粘贴到该目录，否则粘贴到当前 cwd
    const targetDir = entry?.isDirectory
      ? entry.path
      : (side === 'local' ? store.localCwd : store.remoteCwd)
    if (side === 'local') await store.localCopy(cb.paths, targetDir)
    else await store.remoteCopy(cb.paths, targetDir)
    return
  }

  if (action === 'newFolder') {
    openRenameDialog(true, '', side)
    return
  }

  if (action === 'zip' && entry) {
    const paths = getCtxPaths(side, entry)
    const defaultName = paths.length === 1
      ? entry.name + '.zip'
      : 'archive.zip'
    openZipDialog(paths, defaultName, side)
    return
  }

  if (action === 'unzip' && entry) {
    const destName = entry.name.replace(/\.zip$/i, '') || 'unzipped'
    // Bug 1 修复：检查解压目标目录是否已存在，提示用户是否继续（内部同名文件会被覆盖）
    const targetPath = side === 'local'
      ? await store.joinLocalPath(destName)
      : store.joinRemotePath(destName)
    const exists = side === 'local'
      ? await store.localPathExists(targetPath)
      : await store.remotePathExists(targetPath)
    if (exists) {
      const ok = await confirm({
        title: '目录已存在',
        message: `解压目标「${destName}」已存在，内部同名文件将被覆盖，是否继续？`,
        confirmText: '继续',
        danger: true,
      })
      if (!ok) return
    }
    try {
      if (side === 'local') await store.unzipLocal(entry.path, destName)
      else await store.unzipRemote(entry.path, destName)
    } catch { /* toast 已在 store 内弹出 */ }
    return
  }

  if (action === 'copyPath' && entry) {
    try {
      await navigator.clipboard.writeText(entry.path)
      toast.success('已复制路径')
    } catch {
      toast.error('复制失败')
    }
    return
  }

  if (action === 'rename' && entry) {
    openRenameDialog(false, entry.name, side, entry.path)
    return
  }

  if (action === 'delete' && entry) {
    const paths = getCtxPaths(side, entry)
    const ok = await confirm({
      title: '删除',
      message: paths.length === 1
        ? `确认删除「${entry.name}」?\n此操作不可撤销。`
        : `确认删除 ${paths.length} 项?\n此操作不可撤销。`,
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      for (const p of paths) {
        if (side === 'local') await store.localDelete(p)
        else await store.remoteDelete(p)
      }
      toast.success('已删除')
    } catch (err: any) {
      toast.error(toFriendlyMessage(err, '删除失败'))
    }
  }
}

/** 判断是否为 zip 文件（用于右键菜单显示"解压"选项） */
function isZipFile(entry: AnyEntry): boolean {
  return !entry.isDirectory && /\.zip$/i.test(entry.name)
}

/**
 * 获取右键菜单作用对象路径列表：
 * - 若 entry 已被选中，则返回该栏所有选中项路径（支持批量操作）
 * - 否则只返回 entry 自身路径
 */
function getCtxPaths(side: 'local' | 'remote', entry: AnyEntry): string[] {
  const selected = side === 'local' ? store.selectedLocal : store.selectedRemote
  if (selected.has(entry.path)) {
    return Array.from(selected)
  }
  return [entry.path]
}

// ─── 重命名/新建 ──────────────────────
const renameDialog = reactive({
  visible: false,
  name: '',
  isNew: false,
  side: '' as 'local' | 'remote',
  oldPath: '',
})
const renameInput = ref<HTMLInputElement | null>(null)

// ─── 压缩 zip ──────────────────────────
const zipDialog = reactive({
  visible: false,
  name: '',
  side: '' as 'local' | 'remote',
  paths: [] as string[],
})
const zipInput = ref<HTMLInputElement | null>(null)

function openZipDialog(paths: string[], defaultName: string, side: 'local' | 'remote'): void {
  zipDialog.paths = paths
  zipDialog.name = defaultName
  zipDialog.side = side
  zipDialog.visible = true
  nextTick(() => {
    zipInput.value?.focus()
    // 预选主文件名部分（不含 .zip 扩展名）
    const dot = defaultName.lastIndexOf('.')
    if (dot > 0) zipInput.value?.setSelectionRange(0, dot)
  })
}

async function confirmZip(): Promise<void> {
  const finalName = zipDialog.name.trim()
  if (!finalName) return
  const { side, paths } = zipDialog
  zipDialog.visible = false
  // 检查目标 zip 是否已存在，提示用户是否覆盖
  const targetPath = side === 'local'
    ? await store.joinLocalPath(finalName)
    : store.joinRemotePath(finalName)
  const exists = side === 'local'
    ? await store.localPathExists(targetPath)
    : await store.remotePathExists(targetPath)
  if (exists) {
    const ok = await confirm({
      title: '文件已存在',
      message: `「${finalName}」已存在，是否覆盖？`,
      confirmText: '覆盖',
      danger: true,
    })
    if (!ok) return
  }
  try {
    if (side === 'local') await store.zipLocal(paths, finalName)
    else await store.zipRemote(paths, finalName)
  } catch { /* toast 已在 store 内弹出 */ }
}

function openRenameDialog(isNew: boolean, name: string, side: 'local' | 'remote', oldPath = ''): void {
  renameDialog.isNew = isNew
  renameDialog.name = name
  renameDialog.side = side
  renameDialog.oldPath = oldPath
  renameDialog.visible = true
  nextTick(() => {
    renameInput.value?.focus()
    if (!isNew) {
      const dot = name.lastIndexOf('.')
      renameInput.value?.setSelectionRange(0, dot > 0 ? dot : name.length)
    }
  })
}

async function confirmRename(): Promise<void> {
  const name = renameDialog.name.trim()
  if (!name) return
  try {
    if (renameDialog.isNew) {
      if (renameDialog.side === 'local') await store.localMkdir(name)
      else await store.remoteMkdir(name)
    } else {
      if (renameDialog.side === 'local') await store.localRename(renameDialog.oldPath, name)
      else await store.remoteRename(renameDialog.oldPath, name)
    }
    toast.success(renameDialog.isNew ? '已创建' : '已重命名')
  } catch (err: any) {
    toast.error(toFriendlyMessage(err, '操作失败'))
  }
  renameDialog.visible = false
}

// ─── 关闭右键菜单 ─────────────────────
function dismissCtxMenu(): void {
  ctxMenu.visible = false
  transferCtxMenu.value.visible = false
}

// ─── 弹窗编辑器 ───────────────────────
const fileEditor = reactive({
  visible: false,
  side: 'local' as 'local' | 'remote',
  path: '',
  name: '',
})

function openFileEditor(side: 'local' | 'remote', entry: AnyEntry): void {
  fileEditor.side = side
  fileEditor.path = entry.path
  fileEditor.name = entry.name
  fileEditor.visible = true
}

function onOpenFile(payload: { side: 'local' | 'remote'; entry: AnyEntry }): void {
  openFileEditor(payload.side, payload.entry)
}

async function onFileSaved(): Promise<void> {
  if (fileEditor.side === 'local') await store.localRefresh()
  else await store.remoteRefresh()
}
</script>

<style scoped>
.sftp-view {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-base);
}

/* 会话状态栏 */
.sftp-session-bar {
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.session-left { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.session-dot {
  width: 8px; height: 8px; border-radius: 50%;
}
.session-dot.online { background: var(--accent-green); box-shadow: 0 0 6px rgba(78,201,176,0.6); }
.session-dot.offline { background: var(--text-tertiary); }
.session-name { color: var(--text-primary); font-weight: 500; }
.session-host { color: var(--text-tertiary); font-family: var(--font-mono); font-size: 11px; }
.session-right { display: flex; gap: 6px; }
.session-btn {
  height: 26px; padding: 0 10px; font-size: 12px;
  background: transparent; border: 1px solid var(--border-default);
  color: var(--text-secondary); border-radius: 4px; cursor: pointer;
  display: flex; align-items: center; gap: 5px;
  font-family: var(--font-sans);
}
.session-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.session-btn svg { width: 12px; height: 12px; }
.session-btn.connect { color: var(--accent-green); border-color: rgba(78,201,176,0.4); }
.session-btn.connect:hover { background: rgba(78,201,176,0.1); }
.session-btn.transfer { position: relative; }
.session-btn.transfer.active { background: var(--bg-hover); color: var(--text-primary); }
.transfer-badge {
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 16px;
  height: 16px;
  border-radius: 8px;
  background: var(--accent-blue);
  color: #fff;
  font-size: 10px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 4px;
  pointer-events: none;
}

/* 双栏 */
.sftp-panes {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 0;
}

/* 连接选择模态 */
.conn-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.conn-dialog {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  width: 420px;
  max-height: 60vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
}
.conn-dialog-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 13px; font-weight: 500; color: var(--text-primary);
}
.conn-close {
  background: transparent; border: none; color: var(--text-tertiary);
  font-size: 20px; cursor: pointer; padding: 0; line-height: 1;
}
.conn-close:hover { color: var(--text-primary); }
.conn-list { padding: 6px; overflow-y: auto; }
.conn-empty {
  padding: 24px 16px; text-align: center;
  font-size: 12px; color: var(--text-tertiary);
}
.conn-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-radius: 4px; cursor: pointer;
  color: var(--text-tertiary);
}
.conn-item:hover { background: var(--bg-hover); color: var(--text-secondary); }
.conn-item.disabled { opacity: 0.5; cursor: not-allowed; }
.conn-item-info { flex: 1; min-width: 0; }
.conn-item-name { font-size: 13px; color: var(--text-primary); }
.conn-item-host { font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); }
.conn-item .spinner {
  width: 12px; height: 12px;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* 右键菜单 */
.ctx-menu {
  position: fixed;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: var(--shadow-lg);
  padding: 4px;
  min-width: 160px;
  z-index: 9999;
  background: var(--bg-elevated);
}
.ctx-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px;
  font-size: 12px; color: var(--text-secondary);
  cursor: pointer; border-radius: 4px;
}
.ctx-item:hover { background: var(--bg-hover); color: var(--text-primary); }
.ctx-item.danger:hover { background: rgba(248, 81, 73, 0.15); color: var(--accent-red); }
.ctx-sep { height: 1px; margin: 4px 0; background: var(--border-subtle); }

/* 重命名/新建对话框 */
.rename-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.5);
  display: flex; align-items: center; justify-content: center;
  z-index: 1001;
}
.rename-dialog {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 20px;
  width: 320px;
  box-shadow: var(--shadow-lg);
}
.rename-title {
  font-size: 14px; font-weight: 600;
  color: var(--text-primary); margin-bottom: 12px;
}
.rename-input {
  width: 100%; height: 32px; padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: 13px; outline: none;
  font-family: var(--font-sans);
}
.rename-input:focus { border-color: var(--accent-blue); }
.rename-actions {
  display: flex; justify-content: flex-end;
  gap: 8px; margin-top: 16px;
}
.rename-btn {
  height: 30px; padding: 0 14px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  font-size: 12px; cursor: pointer;
  font-family: var(--font-sans);
}
.rename-btn.cancel { background: transparent; color: var(--text-secondary); }
.rename-btn.cancel:hover { background: var(--bg-hover); }
.rename-btn.confirm { background: var(--accent-blue); color: #fff; border-color: var(--accent-blue); }
.rename-btn.confirm:hover { opacity: 0.9; }

/* 传输任务右键菜单 */
.transfer-ctx-menu {
  position: fixed;
  z-index: 1100;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 4px;
  min-width: 160px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  animation: ctx-fade-in 0.1s ease-out;
}
@keyframes ctx-fade-in {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}

/* 传输任务列表弹窗 */
.transfer-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(2px);
}
.transfer-panel {
  background: color-mix(in srgb, var(--bg-elevated) 92%, transparent);
  border: 1px solid var(--border-default);
  border-radius: 12px;
  width: 560px;
  max-height: 600px;
  display: flex;
  flex-direction: column;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(12px);
  overflow: hidden;
}
.transfer-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.transfer-panel-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.transfer-panel-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.transfer-count {
  font-size: 11px;
  color: var(--text-tertiary);
}
.transfer-clear-btn {
  background: transparent;
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
}
.transfer-clear-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.transfer-panel-close {
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  font-size: 20px;
  cursor: pointer;
  padding: 0;
  line-height: 1;
  width: 24px;
  height: 24px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.transfer-panel-close:hover { background: var(--bg-hover); color: var(--text-primary); }

.transfer-panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.transfer-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  gap: 12px;
  color: var(--text-tertiary);
}
.transfer-empty svg { opacity: 0.4; }
.transfer-empty p { font-size: 13px; margin: 0; }
.transfer-empty-hint { font-size: 11px !important; opacity: 0.6; }

/* 传输列表分区 */
.transfer-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.transfer-section + .transfer-section {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}
.transfer-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
}
.transfer-section-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}
.transfer-section-count {
  font-size: 10px;
  color: var(--text-tertiary);
  background: var(--bg-hover);
  padding: 1px 6px;
  border-radius: 8px;
}

/* 传输列表 */
.transfer-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.transfer-list-item {
  display: grid;
  grid-template-columns: 24px 1fr auto auto 20px;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--bg-hover);
  transition: background 0.15s;
}
.transfer-list-item:hover { background: var(--bg-active); }

/* 图标 */
.tl-icon {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.tl-icon.up { background: rgba(78, 201, 176, 0.12); color: var(--accent-green); }
.tl-icon.down { background: rgba(0, 200, 255, 0.12); color: var(--accent-cyan); }
.tl-icon svg { width: 14px; height: 14px; }

/* 信息 */
.tl-info {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tl-name {
  font-size: 12px;
  color: var(--text-primary);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tl-path {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  color: var(--text-tertiary);
  overflow: hidden;
}
.tl-path-from,
.tl-path-to {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 120px;
}
.tl-path-from { opacity: 0.8; }
.tl-arrow { flex-shrink: 0; opacity: 0.5; }
.tl-meta {
  font-size: 10px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tl-speed { color: var(--accent-blue); }
.tl-paused { color: var(--accent-orange); }
.tl-done { color: var(--accent-green); }
.tl-error { color: var(--accent-red); }
.tl-cancelled { color: var(--text-tertiary); }
.tl-pending { color: var(--text-tertiary); }

/* 进度条 */
.tl-progress-bar {
  grid-column: 2;
  width: 100%;
  height: 3px;
  background: var(--bg-base);
  border-radius: 2px;
  overflow: hidden;
  margin-top: 2px;
}
.tl-progress-fill {
  height: 100%;
  background: var(--accent-blue);
  border-radius: 2px;
  transition: width 0.15s;
}
.tl-progress-fill.paused { background: var(--accent-orange); }
.tl-progress-fill.done { background: var(--accent-green); }
.tl-progress-fill.error { background: var(--accent-red); }
.tl-progress-fill.cancelled { background: var(--text-tertiary); }
.tl-progress-fill.indeterminate {
  width: 40%;
  background: var(--accent-blue);
  animation: indeterminate-slide 1.2s ease-in-out infinite;
}
@keyframes indeterminate-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(250%); }
}

/* 右侧百分比 */
.tl-right {
  font-size: 11px;
  color: var(--text-secondary);
  text-align: right;
  font-family: var(--font-mono);
  white-space: nowrap;
}
.tl-percent-indeterminate { color: var(--text-secondary); }
.tl-percent-success { color: var(--accent-green); font-weight: 600; }
.tl-percent-paused { color: var(--accent-orange); }
.tl-percent-error { color: var(--accent-red); }
.tl-percent-cancelled { color: var(--text-tertiary); }
.tl-percent-pending { color: var(--text-tertiary); }

/* 操作按钮 */
.tl-action {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  cursor: pointer;
  border-radius: 4px;
  padding: 0;
  flex-shrink: 0;
}
.tl-action:hover { background: var(--bg-hover); }
.tl-action.tl-cancel:hover { color: var(--accent-red); }
.tl-action.tl-resume:hover { color: var(--accent-green); }
.tl-action.tl-remove:hover { color: var(--text-secondary); }
.tl-action svg { width: 10px; height: 10px; }
</style>
