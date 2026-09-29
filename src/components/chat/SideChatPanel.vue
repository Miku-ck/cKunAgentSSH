<template>
  <div class="side-chat-panel" :style="{ width: panelWidth + 'px' }">
    <!-- 左侧拖拽手柄 -->
    <div class="resize-handle" @mousedown="startResize"></div>

    <!-- 顶部栏 -->
    <div class="panel-header">
      <slot name="header-prefix"></slot>
      <span class="panel-title">{{ panelTitle }}</span>
      <div class="header-spacer"></div>
      <div class="header-actions">
        <button class="icon-btn" :class="{ loading: props.creating }" :title="props.creating ? '创建中...' : '新建对话'" :aria-label="props.creating ? '创建中...' : '新建对话'" :disabled="props.creating" @click="$emit('new-chat')">
          <svg v-if="!props.creating" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          <svg v-else class="spinner" viewBox="0 0 24 24" fill="none" width="14" height="14"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/><path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
        </button>
        <button class="icon-btn" title="历史记录" aria-label="历史记录" @click="showHistoryPanel = !showHistoryPanel">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        </button>
        <button class="icon-btn" title="关闭面板" aria-label="关闭面板" @click="$emit('close')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    </div>

    <!-- 主内容区 -->
    <div class="main-content" ref="messagesEl">
      <!-- 空状态（历史面板打开时隐藏：遮罩半透明会透出当前对话，且 display:none
           使 main-content 无可滚内容，滚轮不再穿透滚动背后消息列表） -->
      <div v-if="displayMessages.length === 0" v-show="!showHistoryPanel" class="empty-state">
        <div class="empty-icon-wrapper">
          <slot name="empty-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="40" height="40">
              <rect x="2" y="4" width="20" height="16" rx="2"/>
              <path d="M6 8h.01M10 8h.01M14 8h.01"/>
              <path d="M6 12h12M6 16h8"/>
            </svg>
          </slot>
        </div>
        <h3 class="empty-title">{{ emptyTitle }}</h3>
        <p class="empty-desc">{{ emptyDesc }}</p>
      </div>

      <!-- 消息列表（历史面板打开时隐藏，见上方空状态注释） -->
      <div v-else class="messages-list" role="log" aria-live="polite" v-show="!showHistoryPanel">
        <template v-for="(msg, idx) in displayMessages" :key="msg.id">
          <MessageItem
            :message="msg"
            :is-last="idx === props.messages.length - 1"
            :is-streaming="isLiveAgentRun(msg, idx)"
          >
            <template v-if="showMessageActions" #actions="{ content }">
              <slot name="message-actions" :content="content" :msg="msg"></slot>
            </template>
          </MessageItem>
        </template>

        <!-- Agent 面板插槽（运行告警/审批卡等，由具体助手面板注入） -->
        <slot name="agent-panel"></slot>
      </div>

      <!-- 历史面板 -->
      <HistoryPanel
        v-if="showHistoryPanel"
        :groups="groupedConversations"
        :current-conv-id="currentConvId"
        :is-empty="conversations.length === 0"
        @close="showHistoryPanel = false"
        @switch="handleSwitch"
        @delete="handleDelete"
      />
    </div>

    <!-- 输入区域 -->
    <div class="input-section">
      <div class="input-container">
        <!-- 上下文标签插槽（会话附加信息，可选） -->
        <slot name="input-prefix"></slot>

        <!-- 已附加能力（+ 号菜单选中项）—— 仅前端态，后端接入前不参与发送。
             「目标」「计划」都是阶段姿态：切换后随下一次消息注入，不在此单独成块展示 -->

        <!-- 已选技能标签（与 Work 模式 ChatInput 风格一致） -->
        <div v-if="selectedSkillTags.length > 0" class="skill-tags">
          <span v-for="tag in selectedSkillTags" :key="tag.id" class="tag-skill">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9">
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
            </svg>
            {{ tag.name }}
            <button class="tag-remove" @click="removeSkill(tag.id)" title="移除技能" aria-label="移除技能">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="8" height="8">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </span>
        </div>

        <!-- 已选附件（上传文件，随消息注入 AI 上下文） -->
        <div v-if="pendingFiles.length > 0" class="pending-files">
          <span v-for="att in pendingFiles" :key="att.file_id" class="tag-file" :title="att.truncated ? '文件较大，已截断前 50K 字符注入' : ''">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            <span class="file-name">{{ att.filename }}</span>
            <span v-if="att.size" class="file-size">{{ formatBytes(att.size) }}</span>
            <button class="tag-remove" @click="removePendingFile(att.file_id)" title="移除文件" aria-label="移除文件">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="8" height="8">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </span>
        </div>

        <textarea
          ref="inputRef"
          v-model="inputText"
          class="chat-input"
          rows="1"
          :placeholder="inputPlaceholder"
          aria-label="消息输入框"
          role="combobox"
          :aria-expanded="showSkillMenu || showPlusMenu"
          :aria-controls="showPlusMenu ? 'plus-menu' : 'skill-menu-list'"
          :aria-activedescendant="showPlusMenu ? plusActiveOptionId : showSkillMenu ? activeSkillOptionId : undefined"
          :maxlength="50000"
          :disabled="agentRunning"
          @keydown="handleKeydown"
          @input="onInput"
        ></textarea>

        <!-- / 触发技能菜单 -->
        <div v-if="showSkillMenu" class="skill-menu">
          <div class="skill-menu-header">
            <span class="menu-title">技能</span>
            <span class="menu-hint">↑↓ 选择 · Enter 确认 · Esc 关闭</span>
          </div>
          <div id="skill-menu-list" class="skill-menu-list" role="listbox" aria-label="技能">
            <div
              v-for="(item, i) in filteredSkills"
              :key="item.id"
              class="skill-menu-item"
              :class="{ active: i === skillActiveIdx }"
              role="option"
              :id="`skill-opt-${item.id}`"
              :aria-selected="i === skillActiveIdx"
              @click="pickSkill(item)"
              @mouseenter="skillActiveIdx = i"
            >
              <div class="skill-item-info">
                <span class="skill-item-name">{{ item.name }}</span>
                <span v-if="item.description" class="skill-item-desc">{{ item.description }}</span>
              </div>
              <span v-if="item.category" class="skill-item-cat">{{ item.category }}</span>
            </div>
            <div v-if="filteredSkills.length === 0" class="skill-menu-empty">
              无匹配技能
            </div>
          </div>
        </div>
      </div>

      <!-- 工具栏 -->
      <div class="input-toolbar">
        <div class="toolbar-left">
          <!-- 能力入口（目标 / 计划 / 技能 / 插件），位于上传按钮左侧。
               工具栏贴面板底部，故菜单向上展开（与 / 技能菜单同层、同定位策略）。 -->
          <div class="plus-wrap">
            <button
              class="plus-btn"
              :disabled="agentRunning"
              :title="agentRunning ? '当前响应中，请稍后再试' : '添加'"
              :aria-label="agentRunning ? '当前响应中' : '添加'"
              aria-haspopup="menu"
              aria-controls="plus-menu"
              :aria-expanded="showPlusMenu"
              @click="togglePlusMenu"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="14" height="14">
                <line x1="12" y1="5" x2="12" y2="19"/>
                <line x1="5" y1="12" x2="19" y2="12"/>
              </svg>
            </button>

            <div v-if="showPlusMenu" id="plus-menu" class="plus-menu" role="menu" aria-label="切换对话姿态">
              <button
                class="menu-item"
                :class="{ 'is-active': plusActiveIdx === 0, 'is-selected': isGoalMode, 'is-on': workflowPhase === 'goal' }"
                role="menuitemcheckbox"
                :aria-checked="isGoalMode"
                id="plus-opt-goal"
                :title="isGoalMode
                  ? '目标姿态已开启：AI 会先确认要达成什么、怎么算成功。点击关闭'
                  : '目标姿态：让 AI 聚焦本次要达成什么、怎么算成功'"
                @click="pickCap('goal')"
                @mouseenter="plusActiveIdx = 0"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <circle cx="12" cy="12" r="8"/>
                    <path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>
                  </svg>
                </span>
                <span class="mi-name">目标</span>
                <span class="mi-desc"></span>
                <span class="mi-check" :class="{ show: isGoalMode }">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="11" height="11"><polyline points="20 6 9 17 4 12"/></svg>
                </span>
              </button>

              <!-- 「计划」= 只读强制姿态的开关（qcode 形态：模式切换收进 + 菜单）。
                   点亮态取当前姿态；审批下拉（手动/自动）是另一条轴，点亮计划时下拉
                   休眠禁用，两处状态在 resolveRunModes 交汇。
                   「目标」与此同形：两者各在工具条常驻一枚 chip（见 .chip-wrap /
                   chipViews），因为"只选了姿态、还没有工件"的中间态需要常驻可见。
                   索引 1 与 CAP_KEYS 对齐 —— 此前 DOM 缺这一行但键盘导航仍会停在
                   plus-opt-plan 上（aria-activedescendant 指向不存在的元素），恢复该行同时修好它。 -->
              <button
                class="menu-item"
                :class="{ 'is-active': plusActiveIdx === 1, 'is-on': isPlanMode }"
                role="menuitemcheckbox"
                :aria-checked="isPlanMode"
                id="plus-opt-plan"
                :title="isPlanMode
                  ? '计划模式已开启：只读调研，写操作会被直接拒绝。点击退出'
                  : '计划模式：先只读调研并产出计划，批准后再动手'"
                @click="pickCap('plan')"
                @mouseenter="plusActiveIdx = 1"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <path d="m3 7 2 2 4-4"/>
                    <path d="M13 6h8M13 12h8M13 18h8"/>
                    <path d="M3 16h4M3 20h4"/>
                  </svg>
                </span>
                <span class="mi-name">计划</span>
                <span class="mi-desc"></span>
                <span class="mi-check" :class="{ show: isPlanMode }">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="11" height="11"><polyline points="20 6 9 17 4 12"/></svg>
                </span>
              </button>

              <!-- 分组约定：带 › 的进「二级清单」（插件 / 技能），不带的直接改状态
                   （目标 / 计划）。上一版这里是一条 .menu-divider 横线 —— 那是我按
                   「任务层 / 能力层」自己推的分层；参考形态各行是**等距**的
                   （行距一律 ~29px，无横线造成的突增），说明它靠尾标而非横线
                   表达分组，故去掉横线、改由 › 承担，键盘顺序同步为视觉顺序。 -->

              <!-- 插件：全仓没有任何插件清单与加载端（grep plugin 只命中本文件），
                   故整行置灰且**不开**二级菜单 —— 不给 aria-haspopup，避免对读屏
                   承诺一个打不开的菜单；也不挂 @mouseenter（disabled 元素本就不派发
                   鼠标事件，且即便派发，键盘高亮也不该落到一个点不动的行上）。
                   尾标保留 › 是把它归入「带清单的项」这一族，纯视觉归属；
                   左边文案写明「尚未接入」，不做假启用。 -->
              <button
                class="menu-item is-disabled"
                :class="{ 'is-active': plusActiveIdx === 2 }"
                role="menuitem"
                id="plus-opt-plugin"
                aria-disabled="true"
                disabled
                title="插件尚未接入：全仓没有插件清单与加载端，此处只保留入口位"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <rect x="3" y="3" width="7" height="7" rx="1.5"/>
                    <rect x="14" y="3" width="7" height="7" rx="1.5"/>
                    <rect x="3" y="14" width="7" height="7" rx="1.5"/>
                    <path d="M14 17.5h7M17.5 14v7"/>
                  </svg>
                </span>
                <span class="mi-name">插件</span>
                <span class="mi-desc"></span>
                <span class="mi-arrow">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="10" height="10"><path d="m9 6 6 6-6 6"/></svg>
                </span>
              </button>

              <!-- 技能：打开的是既有技能清单（与 / 触发同一份 filteredSkills），
                   所以它不是开关、也不带勾选态；挑中的技能以 skill-tags 呈现 -->
              <button
                class="menu-item"
                :class="{ 'is-active': plusActiveIdx === 3 }"
                role="menuitem"
                id="plus-opt-skill"
                aria-haspopup="menu"
                title="从已启用技能中挑一个附加到本次输入"
                @click="pickCap('skill')"
                @mouseenter="plusActiveIdx = 3"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>
                  </svg>
                </span>
                <span class="mi-name">技能</span>
                <span class="mi-desc"></span>
                <span class="mi-arrow">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="10" height="10"><path d="m9 6 6 6-6 6"/></svg>
                </span>
              </button>
            </div>
          </div>

          <!-- 上传文件附件 -->
          <button
            v-if="uploadHandler"
            class="toolbar-btn upload-btn"
            :disabled="uploading || agentRunning"
            :title="uploading ? '上传中...' : agentRunning ? '当前响应中，请稍后再上传' : '上传文件（文本类，单文件 ≤ 10MB）'"
            :aria-label="uploading ? '上传中' : '上传文件'"
            @click="handleUploadClick"
          >
            <svg v-if="!uploading" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="14" height="14"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            <svg v-else class="spinner" viewBox="0 0 24 24" fill="none" width="13" height="13"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/><path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
          </button>
          <input
            ref="fileInputRef"
            type="file"
            multiple
            class="hidden-file-input"
            :disabled="agentRunning"
            @change="handleFileChange"
          />
          <!-- 审批档位选择器（手动审批 / 自动审批）—— 与「+（添加）」是两个独立控件：
               + 管「往里加什么 / 以什么姿态跑」，这里管「危险操作谁来点头」。
               计划模式点亮时本控件休眠（写操作在弹卡前就被拒，没有可审批的卡），
               故禁用并在 title 里说明。 -->
          <div class="perm-wrap">
            <button
              ref="permTriggerRef"
              class="perm-btn"
              :disabled="agentRunning || isPlanMode"
              :title="agentRunning ? '当前响应中，请稍后再试'
                : isPlanMode ? '计划模式下写操作直接拒绝，审批档暂不生效'
                : '审批模式：危险操作由你逐条批准，还是让模型先代判一次'"
              aria-haspopup="listbox"
              aria-controls="perm-menu"
              :aria-expanded="showPermMenu"
              aria-label="审批模式"
              @click="togglePermMenu"
              @keydown.down.prevent="openPermMenu()"
            >
              <svg class="perm-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="14" height="14" aria-hidden="true">
                <circle cx="12" cy="12" r="9"/>
                <path d="M12 12V3a9 9 0 0 1 9 9z" fill="currentColor" stroke="none"/>
              </svg>
              <span class="perm-name">{{ PERM_LABELS[approvalMode] }}</span>
              <svg class="perm-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
            </button>

            <div
              v-if="showPermMenu"
              id="perm-menu"
              ref="permMenuRef"
              class="perm-menu"
              role="listbox"
              aria-label="审批模式"
              @keydown="handlePermMenuKeydown"
            >
              <button
                v-for="opt in PERM_OPTIONS"
                :key="opt.key"
                class="perm-opt"
                role="option"
                :id="`perm-opt-${opt.key}`"
                :aria-selected="approvalMode === opt.key"
                :title="opt.desc"
                @click="pickPerm(opt.key)"
              >
                <span class="po-text">
                  <span class="po-name">{{ opt.label }}</span>
                  <span class="po-desc">{{ opt.desc }}</span>
                </span>
                <span class="po-check" :class="{ show: approvalMode === opt.key }">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="11" height="11" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
                </span>
              </button>
            </div>
          </div>

          <!-- 模式 chip（目标/计划共用一套）：常驻工具条，紧挨审批下拉。显隐见 script
               里 chipViews 的注释（姿态 ∨ 工件，两者缺一都会说谎）。
               主体点击看详情，× 点击关闭。 -->
          <div v-for="c in chipViews" :key="c.kind" class="chip-wrap">
            <div class="mode-chip" :class="{ 'is-open': openChip === c.kind }">
              <button
                class="mc-body"
                :title="c.bodyTitle"
                :aria-expanded="openChip === c.kind"
                aria-haspopup="dialog"
                @click="toggleChipDetail(c.kind)"
              >
                <span v-if="c.hasArtifact" class="mc-dot" :title="c.dotTitle"></span>
                <span class="mc-name">{{ c.name }}</span>
              </button>
              <button
                class="mc-close"
                :aria-label="`移除${c.name}`"
                :title="c.clearTitle"
                @click="clearChip(c.kind)"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" width="10" height="10" aria-hidden="true">
                  <path d="M18 6 6 18M6 6l12 12"/>
                </svg>
              </button>
            </div>

            <div
              v-if="openChip === c.kind"
              class="chip-pop"
              role="dialog"
              :aria-label="`${c.name}详情`"
            >
              <template v-if="c.hasArtifact">
                <div class="cp-head">{{ c.artifactHead }}</div>
                <pre class="cp-body">{{ c.artifact }}</pre>
              </template>
              <template v-else>
                <div class="cp-head">{{ c.postureHead }}</div>
                <p class="cp-desc">{{ c.postureDesc }}</p>
                <button
                  class="cp-act"
                  :disabled="agentRunning"
                  @click="emit('open-phase', c.kind); closeChipDetail()"
                >{{ c.actLabel }}</button>
              </template>
            </div>
          </div>

          <slot name="toolbar-left"></slot>
        </div>
        <div class="toolbar-right">
          <!-- 模型选择器（未配置模型时显示引导空态） -->
          <div
            v-if="models.length === 0"
            class="model-empty-hint"
            role="button"
            tabindex="0"
            title="尚未配置模型，点击去设置"
            @click="openSettings('models')"
            @keydown.enter="openSettings('models')"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            <span>尚未配置模型</span>
            <span class="model-empty-hint-action">去设置</span>
          </div>
          <div v-else class="model-selector" ref="modelSelectorRef">
            <button class="model-selector-btn" @click="showModelDropdown = !showModelDropdown">
              <span class="model-name">{{ currentModelName }}</span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="10" height="10"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
            <div v-if="showModelDropdown" class="model-dropdown">
              <div
                v-for="model in models"
                :key="model.id"
                class="model-dropdown-item"
                :class="{ active: model.id === selectedModelId }"
                @click="handleSelectModel(model.id)"
              >
                <div class="model-item-info">
                  <span class="model-label">{{ model.name || model.model_id || model.id }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 发送/中止按钮 -->
          <button
            v-if="!agentRunning"
            class="send-btn"
            :disabled="!canSend"
            @click="handleSend"
            :title="canSend ? '发送' : '请输入内容或添加附件后再发送'"
            aria-label="发送"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="12" y1="19" x2="12" y2="5"/>
              <polyline points="5 12 12 5 19 12"/>
            </svg>
          </button>
          <button v-else class="abort-btn" @click="$emit('abort')" title="中止" aria-label="中止">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import type { ModelConfig } from '@/api/client'
import type { ConvGroup } from '@/types/chat'
import MessageItem from '@/components/chat/MessageItem.vue'
import HistoryPanel from '@/components/chat/HistoryPanel.vue'
import { useResizablePanel } from '@/composables/useResizablePanel'
import { useModelSelector } from '@/composables/useModelSelector'
import { useSettings } from '@/composables/useSettings'
import { useAgentStore } from '@/stores/agent'
import { useSettingsStore } from '@/stores/settings'
import { useSkillStore } from '@/stores/skills'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useToastStore } from '@/stores/toast'
import type { ChatAttachment } from '@/api/client'
// 档位与文案单点定义在 utils/run-modes：首页输入卡读的是同一份，两处各写一遍
// 就会对同样两个档位给出不同说明
import { APPROVAL_OPTIONS as PERM_OPTIONS, APPROVAL_LABELS as PERM_LABELS, type PostureKey } from '@/utils/run-modes'
import { formatBytes } from '@/utils/format'

const settingsStore = useSettingsStore()
const skillStore = useSkillStore()
// 审查修复：loadBuiltinSkills 此前全仓零调用方 —— 内置技能扫描、后端同步
// （syncBuiltinToBackend → SkillRein 匹配）与远程 manual/auto 装载整条链路
// 悬空，斜杠技能菜单永远为空。面板创建时装载一次（重复挂载重扫，语义同刷新）。
void skillStore.loadBuiltinSkills()
const toast = useToastStore()

// ── 消息接口（兼容 ModeMessage 和 MessageItem 的 ChatMessage） ──
interface DisplayMsg {
  id: string
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  streamingContent?: string
  streamingReasoning?: string
  duration?: number
  [key: string]: any
}

// ── Props ──
const props = defineProps<{
  conversations: Array<{ id: string; title: string; updated_at: string }>
  currentConvId: string | null
  messages: DisplayMsg[]
  selectedModelId: string
  models: ModelConfig[]
  groupedConversations: ConvGroup[]
  showMessageActions?: boolean
  agentRunning?: boolean
  creating?: boolean
  placeholder?: string
  emptyTitle?: string
  emptyDesc?: string
  /** 面板头部标题（默认 "AI 助手"） */
  panelTitle?: string
  /** 上传处理函数：由父组件注入（返回上传成功的附件列表）；未注入则不显示上传入口 */
  uploadHandler?: (files: File[]) => Promise<ChatAttachment[]>
  /**
   * 当前会话工作流阶段（''/spec/plan/goal，后端权威）。驱动菜单「目标」项的
   * 点亮态：goal → 目标项点亮。「计划」项点亮与否取当前姿态（开关状态），
   * 不由阶段推导 —— 阶段来自落盘工件（backend /agent/workflow.py），组件只读
   * 展示，不在此伪造切换结果。
   */
  workflowPhase?: string
  /** goal.md 原文（后端权威）。有内容时「目标」chip 才给"看详情"，否则只表姿态。 */
  workflowGoal?: string
  /** plan.md 原文（后端权威）。同上，驱动「计划」chip 的详情与实心点。 */
  workflowPlan?: string
}>()

const emit = defineEmits<{
  close: []
  /** mode = 对话姿态（goal/plan/null=未表态），approval = 审批档（手动/自动）—— 两条独立轴，
   *  合成 permission_mode 的唯一口径在 utils/run-modes.ts */
  send: [content: string, skillIds: string[], attachments: ChatAttachment[], mode: PostureKey | null, approval: 'normal' | 'auto']
  'update:selectedModelId': [id: string]
  'new-chat': []
  'switch-conversation': [id: string]
  'delete-conversation': [id: string]
  abort: []
  /** 请求进入某阶段：通知父层发一条合成消息触发 agent 落盘对应工件（组件不碰网络）。
   *  kind 由 chip 决定：goal → goal_write 沉淀成功标准，plan → plan_write 沉淀方案。 */
  'open-phase': [kind: PostureKey]
  /** 移除某模式的工件：父层删除对应 .md（组件不碰网络），并提示连带回落的阶段 */
  'clear-artifact': [kind: PostureKey]
}>()

// ── 面板头部标题（未传时使用通用默认） ──
const panelTitle = computed(() => props.panelTitle || 'AI 助手')

// ── Refs ──
const messagesEl = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLTextAreaElement | null>(null)
const inputText = ref('')
const showHistoryPanel = ref(false)

// ── 文件上传附件 ──
const fileInputRef = ref<HTMLInputElement | null>(null)
const pendingFiles = ref<ChatAttachment[]>([])
const uploading = ref(false)

function handleUploadClick() {
  fileInputRef.value?.click()
}

async function handleFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const files = input.files ? Array.from(input.files) : []
  input.value = '' // 允许重复选择同一文件
  if (files.length === 0 || !props.uploadHandler || uploading.value) return
  uploading.value = true
  try {
    const results = await props.uploadHandler(files)
    // 追加已上传成功的附件（失败的文件由 handler 内部 toast 提示）
    pendingFiles.value.push(...results)
  } catch (err: unknown) {
    // 防御：handler 出现非预期异常时给出提示，避免静默无反馈
    const msg = err instanceof Error ? err.message : String(err)
    toast.error(`上传失败：${msg}`)
    console.error('[SideChatPanel] upload failed:', err)
  } finally {
    uploading.value = false
  }
}

function removePendingFile(fileId: string) {
  pendingFiles.value = pendingFiles.value.filter(f => f.file_id !== fileId)
}

// ── 能力入口 + 号菜单（视觉顺序：目标 / 计划 / 插件 / 技能）──
// 四个条目的形态各不相同，不能压成同一种控件：
// - 「目标」是对话姿态开关（点亮 → 下一次发送注入目标姿态提示词），并在工具条
//   落一枚常驻 chip；本会话是否已有成功标准文档（goal.md，后端落盘）另说一轴，
//   由 props.workflowPhase 反映在菜单的点亮底色与 chip 的圆点上；
// - 「计划」是权限模式开关（qcode 形态：模式切换收进 + 菜单）：menuitemcheckbox，
//   点亮 = 只读强制 + 计划姿态成对下发，再点退出；工具条权限下拉显示同一个状态；
// - 「技能」从已启用清单里挑一个（与 / 触发同源），选中的以 skill-tags 呈现 ——
//   它不是开关，所以菜单里不给勾选态；
// - 「插件」无清单可列，整行置灰。
// 后端逻辑未接入者，改动点一律集中在 pickCap()。
type CapKey = 'goal' | 'plan' | 'skill' | 'plugin'
/**
 * 键盘导航顺序 —— 必须与 DOM 里的视觉顺序一致，否则 ↑↓ 会「跳着走」。
 * 视觉顺序见模板：目标 → 计划 → 插件 → 技能（插件在技能之前）。
 */
const CAP_KEYS: CapKey[] = ['goal', 'plan', 'plugin', 'skill']

const showPlusMenu = ref(false)
const plusActiveIdx = ref(0)

/**
 * 当前对话姿态（可关的单选，缺省 null = 未表态）—— 对应主流 Agent 的模式选择器。
 * 只有「目标/计划」这类纵向姿态落进 activeMode（决定下一次消息注入的姿态）；
 * 「技能」从清单里挑、走 skill-tags，「插件」未接入、
 * 都没有"单一姿态"的含义，勾选态只对 activeMode 的两项成立。
 *
 * 缺省不是 'goal' 而是 null，是为了让「目标」chip 能被关掉：
 * 'goal' 起步 + × 写回 'goal'，chip 消失了但后端行为一字未变（能关、关的是摆设）。
 * 未表态时后端 workflow_mode 省略 → 不注入任何姿态段，与"用户没选"一致。
 */
const activeMode = ref<PostureKey | null>(null)

/** aria-activedescendant 目标 id（与 / 技能菜单同一无障碍模式） */
const plusActiveOptionId = computed(() => {
  const key = CAP_KEYS[plusActiveIdx.value]
  return key ? `plus-opt-${key}` : undefined
})

/**
 * 键盘导航跳过的条目：disabled 项既点不了、也不该成为落点
 * —— 否则 ↑↓ 会停在「插件」上，回车毫无反应，读起来像坏了。
 * 索引仍按 CAP_KEYS 计数（aria-activedescendant 靠索引取 id），只跳过、不压缩。
 */
const PLUS_SKIP: ReadonlySet<CapKey> = new Set<CapKey>(['plugin'])

function stepPlusMenu(dir: 1 | -1) {
  const n = CAP_KEYS.length
  let i = plusActiveIdx.value
  for (let step = 0; step < n; step++) {
    i = (i + dir + n) % n
    const key = CAP_KEYS[i]
    if (key && !PLUS_SKIP.has(key)) {
      plusActiveIdx.value = i
      return
    }
  }
  // 全部被跳过时保持原位。当前只有「插件」一项 disabled，走不到这里；
  // 写成 for 循环是为了将来加第二项 disabled 时不用改逻辑。
}

function togglePlusMenu() {
  if (showPlusMenu.value) {
    closePlusMenu()
    return
  }
  if (props.agentRunning) return
  showPlusMenu.value = true
  plusActiveIdx.value = 0
  // 焦点留在输入框：键盘导航由 handleKeydown 统一处理（同 / 技能菜单的手感）
  nextTick(() => inputRef.value?.focus())
}

function closePlusMenu(refocus = false) {
  if (!showPlusMenu.value) return
  showPlusMenu.value = false
  plusActiveIdx.value = 0
  if (refocus) nextTick(() => inputRef.value?.focus())
}

/** 菜单项选择：目标 → 姿态开关（点亮即注入目标姿态，再点回到未表态）；
 *  计划 → 权限模式开关（再点一次退出，回到未表态）；
 *  技能 → 打开既有技能清单；插件未接入，兜住键盘 Enter。 */
function pickCap(key: CapKey) {
  // 目标：纵向姿态开关，随下一次发送传给后端作为姿态提示；点亮态同时驱动工具条 chip
  if (key === 'goal') {
    toggleGoalMode()
    closePlusMenu(true)
    return
  }
  // 计划：不是「选一个姿态」而是开关 —— 点亮即只读强制 + 计划提示词成对下发，
  // 再点退出（回到未表态，不是目标姿态）。它是独立于审批下拉的另一条轴（plan 姿态下下拉休眠）。
  if (key === 'plan') {
    togglePlanMode()
    closePlusMenu(true)
    return
  }
  // 技能：打开既有技能清单（与「/」触发同一条路径），选中的以 skill-tags 呈现
  if (key === 'skill') {
    closePlusMenu(true)
    openSkillMenuFromPlus()
    return
  }
  // 插件：无清单可列。按钮已 disabled 拦住鼠标，这里兜住键盘 Enter
  if (key === 'plugin') return
}

// ── 权限模式（发往后端的 AgentRunRequest.permission_mode）──
/**
 * 两条独立轴：
 *   审批轴（下拉，常驻工具条）= normal 手动 / auto 自动 —— 危险操作是用户逐条
 *     批准，还是先让模型代判一次（拿不准仍弹卡，fail-closed 在后端）。
 *   计划轴（+ 菜单「计划」开关）= 只读强制 —— 写操作在弹卡之前就直接拒绝。
 *
 * 「计划」同时意味着**两个**后端字段，不能只发一个：
 *   permission_mode: 'plan' → 后端 PlanModeGuard 只读强制（真正的约束）
 *   workflow_mode:   'plan' → 后端注入计划姿态提示词（只影响措辞）
 * 只发后者就是「亮的空开关」：用户以为进了计划模式，写操作照样执行。
 *
 * 审批轴休眠规则：plan 姿态下 permission_mode 恒为 'plan'（后端 PlanModeGuard
 * 在任何审批卡之前拦截，此时谈"谁来批准"没有意义）。真正的发送值在
 * stores/ssh-ai.ts 经 resolveRunModes(activeMode, approval) 派生 —— 这里是
 * **展示**同源（下拉文案 + 「计划」菜单项点亮），不存在自相矛盾的状态。
 */
const sshAi = useSshAiStore()
/** 档位存在 ssh-ai store 而非组件内：首页输入卡与这里必须共享同一档，
 *  各存一份就是第二条通路（用户在首页选「自动」、侧栏仍按「手动」发送）。 */
const approvalMode = computed(() => sshAi.approvalMode)
const isPlanMode = computed(() => activeMode.value === 'plan')

const showPermMenu = ref(false)
const permMenuRef = ref<HTMLElement | null>(null)
const permTriggerRef = ref<HTMLButtonElement | null>(null)

/** 菜单项（role=option）—— 键盘导航直接作用于真实 DOM，不另存一份索引状态，
 *  避免索引与 DOM 顺序（未来增删选项时）静默错位。 */
function permOptionEls(): HTMLElement[] {
  const root = permMenuRef.value
  if (!root) return []
  return Array.from(root.querySelectorAll<HTMLElement>('[role="option"]'))
}

function openPermMenu() {
  if (props.agentRunning || isPlanMode.value) return
  closePlusMenu()
  closeSkillMenu()
  showPermMenu.value = true
  // 焦点移进菜单（button + role=listbox 的标准做法）：不这么做 ↑↓ 没有落点，
  // 而输入框的 handleKeydown 不接管这个菜单。
  nextTick(() => {
    const els = permOptionEls()
    const idx = PERM_OPTIONS.findIndex(o => o.key === approvalMode.value)
    els[idx >= 0 ? idx : 0]?.focus()
  })
}

function closePermMenu(refocus = false) {
  if (!showPermMenu.value) return
  showPermMenu.value = false
  if (refocus) nextTick(() => permTriggerRef.value?.focus())
}

function togglePermMenu() {
  if (showPermMenu.value) closePermMenu(true)
  else openPermMenu()
}

/** 审批档选择（纯审批轴）：不再触碰姿态 —— plan 的开关只在 + 菜单，
 *  两轴交汇发生在 resolveRunModes（plan 姿态下审批档休眠）。 */
function pickPerm(key: 'normal' | 'auto') {
  sshAi.setApprovalMode(key)
  closePermMenu(true)
}

/** + 菜单「目标」项 = 取反目标姿态。默认未表态（null），所以 chip 不是常驻的：
 *  选了才亮，× 或再点一次即关 —— 与「计划」同为一个可关的开关，而非二选一单选。 */
function toggleGoalMode() {
  activeMode.value = isGoalMode.value ? null : 'goal'
}

/** + 菜单「计划」项 = 取反当前计划态（点亮即只读强制；审批档原样保留，
 *  退出计划后回到未表态，而不是替用户选一个目标姿态） */
function togglePlanMode() {
  activeMode.value = isPlanMode.value ? null : 'plan'
}

// ── 模式 chip（工具条常驻，紧挨审批下拉；目标/计划共用一套机制）──
/**
 * 每枚 chip 的显示条件取两根轴的**或**，因为它们各说一件事、缺一都会说谎：
 *   activeMode === key     用户选了该姿态（下一次发送注入对应提示词）
 *   workflowPhase === key  agent 已落盘该工件（hydrate_for_session 每轮都注入，与前端无关）
 * 只看姿态：plan.md/goal.md 早就存在并持续生效，界面上却什么都没有 —— 工件在跑、UI 装死。
 * 只看阶段：用户刚点了「目标」，chip 不出来，除非 AI 恰好写过对应文件 —— 点了没反应。
 */
const isGoalMode = computed(() => activeMode.value === 'goal')

/** 某模式的工件原文。刻意从 props 直接取，不经 chipViews ——
 *  × 的处理里"先读状态再改状态"若读到已重算过的视图，判定会随改动漂移。 */
function artifactOf(kind: PostureKey): string {
  return (kind === 'goal' ? props.workflowGoal : props.workflowPlan) ?? ''
}

interface ChipView {
  kind: PostureKey
  name: string
  artifact: string
  hasArtifact: boolean
  bodyTitle: string
  dotTitle: string
  clearTitle: string
  postureHead: string
  postureDesc: string
  artifactHead: string
  actLabel: string
}

/** 文案集中在这一处：模板只认字段，不再散落三元表达式。
 *  「计划」浮层刻意不提"批准"—— 计划批准门（spec §2）尚未实现，
 *   在这里写「待批准」就是一个点了没用的假动作。 */
const chipViews = computed<ChipView[]>(() => {
  const goalArtifact = artifactOf('goal')
  const planArtifact = artifactOf('plan')
  const views: ChipView[] = [
    {
      kind: 'goal',
      name: '目标',
      artifact: goalArtifact,
      hasArtifact: !!goalArtifact,
      bodyTitle: goalArtifact
        ? '查看本次任务的成功标准（已落盘，每轮注入）'
        : '目标姿态已开启：本轮起围绕目标收敛。点击查看',
      dotTitle: '本会话已有成功标准文档',
      clearTitle: '移除目标：关闭姿态，并删除本会话的成功标准文档',
      postureHead: '目标姿态已开启',
      postureDesc: 'AI 会优先确认「要达成什么、怎么算成功」并围绕目标收敛。' +
        '本会话还没有成功标准文档 —— 生成后它会每轮注入，chip 上会出现实心点。',
      artifactHead: '本次任务的成功标准',
      actLabel: '让 AI 生成成功标准',
    },
    {
      kind: 'plan',
      name: '计划',
      artifact: planArtifact,
      hasArtifact: !!planArtifact,
      bodyTitle: planArtifact
        ? '查看本会话已落盘的方案（每轮注入）'
        : '计划姿态已开启：本轮只读调研并产出方案。点击查看',
      dotTitle: '本会话已有方案文档',
      clearTitle: '移除计划：关闭姿态，并删除本会话的方案文档',
      postureHead: '计划姿态已开启',
      postureDesc: '本轮只做只读调研并产出方案：写操作会被后端直接拒绝，' +
        '工具条的审批下拉同时休眠。本会话还没有方案文档 —— 生成后它会每轮注入，' +
        'chip 上会出现实心点。',
      artifactHead: '本会话的方案',
      actLabel: '让 AI 生成方案',
    },
  ]
  return views.filter(v => activeMode.value === v.kind || props.workflowPhase === v.kind)
})

/** 同时只展开一枚 chip 的详情（两个浮层叠在工具条上方会互相遮住） */
const openChip = ref<PostureKey | null>(null)

/** 主体点击 = 展开详情。工件已落盘时给原文，否则说明当前只是姿态。 */
function toggleChipDetail(kind: PostureKey) {
  closePlusMenu()
  closePermMenu()
  closeSkillMenu()
  openChip.value = openChip.value === kind ? null : kind
}

function closeChipDetail() {
  openChip.value = null
}

/**
 * × = 关掉。两件事一起做，少一件都是没关：
 *   姿态置 null（下一次发送不再注入该姿态提示词）
 *   删掉对应工件（否则它仍每轮注入，chip 消失只是眼不见为真）
 * 删工件交给父层走 DELETE /api/workflow/artifact —— 组件不碰网络。
 *
 * 姿态只在"本 key 就是当前姿态"时才清：两枚 chip 可以同场（plan.md 在盘上、
 * 本轮用户选了目标姿态），那时点「计划」的 × 若无条件置 null，就顺手关掉了
 * 用户正在用的目标姿态。
 */
function clearChip(kind: PostureKey) {
  if (artifactOf(kind) || props.workflowPhase === kind) emit('clear-artifact', kind)
  if (activeMode.value === kind) activeMode.value = null
  openChip.value = null
}

/** Enter/Space 由按钮自身的 click 处理，这里只管方向键与退出 */
function handlePermMenuKeydown(e: KeyboardEvent) {
  const els = permOptionEls()
  if (els.length === 0) return
  const cur = els.indexOf(document.activeElement as HTMLElement)
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    els[(cur + 1 + els.length) % els.length]?.focus()
    return
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault()
    els[(cur - 1 + els.length) % els.length]?.focus()
    return
  }
  if (e.key === 'Home') {
    e.preventDefault()
    els[0]?.focus()
    return
  }
  if (e.key === 'End') {
    e.preventDefault()
    els[els.length - 1]?.focus()
    return
  }
  if (e.key === 'Escape') {
    e.preventDefault()
    closePermMenu(true)
    return
  }
  // Tab 移出即关（焦点交给下一个控件），不抢它的默认行为
  if (e.key === 'Tab') closePermMenu()
}

// 点 .perm-wrap 以外区域关闭（同 .skill-menu / .plus-menu 的外点策略）
function onDocClickOutsidePermMenu(e: MouseEvent) {
  if (!showPermMenu.value) return
  const target = e.target as HTMLElement | null
  if (target && target.closest?.('.perm-wrap')) return
  closePermMenu()
}
onMounted(() => document.addEventListener('mousedown', onDocClickOutsidePermMenu))
onUnmounted(() => document.removeEventListener('mousedown', onDocClickOutsidePermMenu))

// 点 .chip-wrap 以外区域收起详情（同 .perm-wrap 的外点策略）。
// 命中任一 .chip-wrap 都不关：点另一枚 chip 的主体时 toggleChipDetail 自己会切换过去。
function onDocClickOutsideChip(e: MouseEvent) {
  if (!openChip.value) return
  const target = e.target as HTMLElement | null
  if (target && target.closest?.('.chip-wrap')) return
  closeChipDetail()
}
onMounted(() => document.addEventListener('mousedown', onDocClickOutsideChip))
onUnmounted(() => document.removeEventListener('mousedown', onDocClickOutsideChip))

// 三个浮层（+ 号菜单 / 技能菜单 / 权限菜单）互斥：同时只允许开一个
watch(showPlusMenu, (v) => { if (v) { closePermMenu(); closeChipDetail() } })
watch(showPermMenu, (v) => { if (v) { closePlusMenu(); closeSkillMenu(); closeChipDetail() } })

// ── / 斜杠技能菜单 ──
const selectedSkillIds = ref<string[]>([])
const showSkillMenu = ref(false)
const skillQuery = ref('')
const skillActiveIdx = ref(0)

/** 根据查询串过滤技能 */
const filteredSkills = computed(() => {
  const q = skillQuery.value.toLowerCase().trim()
  const list = skillStore.enabledSkills
  if (!q) return list.slice(0, 8)
  return list
    .filter(s =>
      s.name.toLowerCase().includes(q) ||
      s.slug.toLowerCase().includes(q) ||
      (s.description || '').toLowerCase().includes(q),
    )
    .slice(0, 8)
})

/** aria-activedescendant 目标 id（C-review NIT：技能菜单无障碍） */
const activeSkillOptionId = computed(() => {
  const item = filteredSkills.value[skillActiveIdx.value]
  return item ? `skill-opt-${item.id}` : undefined
})

/** 已选技能标签（用于 UI 展示） */
const selectedSkillTags = computed(() => {
  return selectedSkillIds.value
    .map(id => {
      const s = skillStore.enabledSkills.find(sk => sk.id === id)
      return s ? { id: s.id, name: s.name } : null
    })
    .filter(Boolean) as Array<{ id: string; name: string }>
})

/** 输入框 placeholder：根据是否已选技能显示提示 */
const inputPlaceholder = computed(() => {
  if (selectedSkillIds.value.length > 0) return '已附加技能，输入消息发送...'
  return props.placeholder || '输入消息，/ 调用技能...'
})

function onInput() {
  autoResize()
  detectSkillTrigger()
}

/** 触发菜单的 '/' 在输入框中的下标（选中技能后要抹掉整段 "/query"） */
let skillTriggerPos = -1

/** 检测 / 触发技能菜单 */
function detectSkillTrigger() {
  const el = inputRef.value
  if (!el) return
  const pos = el.selectionStart
  const text = inputText.value.slice(0, pos)
  const lastSlash = text.lastIndexOf('/')
  if (lastSlash === -1) { closeSkillMenu(); return }

  // 触发符前必须是空白或行首（避免路径、URL 误触发）
  if (lastSlash > 0) {
    const charBefore = text[lastSlash - 1]
    if (charBefore && !/\s/.test(charBefore)) { closeSkillMenu(); return }
  }

  // / 后到光标位置之间不能有空白
  const segment = text.slice(lastSlash + 1, pos)
  if (/\s/.test(segment)) { closeSkillMenu(); return }

  skillQuery.value = segment
  skillActiveIdx.value = 0
  skillTriggerPos = lastSlash
  showSkillMenu.value = true
}

function closeSkillMenu() {
  showSkillMenu.value = false
  skillQuery.value = ''
  skillActiveIdx.value = 0
  skillTriggerPos = -1
}

/**
 * 从 + 菜单打开技能清单：与「/」触发复用同一份 filteredSkills/键盘导航，
 * 区别只是不经输入框文本 —— 故触发位置置 -1，pickSkill 会跳过「抹除 /query」
 * 那一步（它已按 skillTriggerPos >= 0 守卫），点选后焦点回到输入框。
 */
function openSkillMenuFromPlus() {
  skillQuery.value = ''
  skillActiveIdx.value = 0
  skillTriggerPos = -1
  showSkillMenu.value = true
}

// + 号菜单与 / 技能菜单互斥：两者浮在同一输入区，需要保证同时只开一个
watch(showPlusMenu, (v) => { if (v) closeSkillMenu() })
watch(showSkillMenu, (v) => { if (v) closePlusMenu() })

// 审查修复（P2）：点击菜单外区域关闭技能菜单 — 原实现只有 Esc/选中/失焦
// 能关，鼠标点输入区外（如消息列表）菜单残留，视觉像死菜单
function onDocClickOutsideSkillMenu(e: MouseEvent) {
  if (!showSkillMenu.value) return
  const target = e.target as HTMLElement | null
  // 菜单自身或输入框内的点击不关闭（菜单项点击走 pickSkill 自行关闭）
  if (target && target.closest?.('.skill-menu')) return
  if (inputRef.value?.contains(target)) return
  closeSkillMenu()
}
// 同理（+ 号菜单）：点菜单/按钮以外区域关闭。.plus-wrap 内含触发按钮与菜单，
// 按钮自行处理开合，故容器内的 mousedown 不在此关闭。
function onDocClickOutsidePlusMenu(e: MouseEvent) {
  if (!showPlusMenu.value) return
  const target = e.target as HTMLElement | null
  if (target && target.closest?.('.plus-wrap')) return
  closePlusMenu()
}
onMounted(() => {
  document.addEventListener('mousedown', onDocClickOutsideSkillMenu)
  document.addEventListener('mousedown', onDocClickOutsidePlusMenu)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', onDocClickOutsideSkillMenu)
  document.removeEventListener('mousedown', onDocClickOutsidePlusMenu)
})

/** 选中技能：加入 selectedSkillIds（输入框上方以标签展示）。
 *  C-review NIT：原实现同时在输入框插入 "@技能名" 文本 — 文本与 skillIds
 *  双重注入可漂移（手动删文本技能仍附加 / 删标签文本残留在消息里）。
 *  现以 skillIds 为单一数据源，标签提供视觉反馈。 */
function pickSkill(skill: { id: string; name: string }) {
  const el = inputRef.value
  // 抹掉唤起菜单的 "/query" 片段：斜杠只是触发符，留在输入框里会被当成正文发出去。
  // （早先的实现用 "@技能名" 文本替换它，改为 skillIds 单一数据源时把替换一起删了）
  let caretPos = -1
  if (skillTriggerPos >= 0) {
    const end = el?.selectionStart ?? inputText.value.length
    inputText.value = inputText.value.slice(0, skillTriggerPos) + inputText.value.slice(end)
    caretPos = skillTriggerPos
  }
  if (!selectedSkillIds.value.includes(skill.id)) {
    selectedSkillIds.value.push(skill.id)
  }
  closeSkillMenu()
  requestAnimationFrame(() => {
    if (el && caretPos >= 0) {
      el.focus()
      el.setSelectionRange(caretPos, caretPos)
    } else {
      inputRef.value?.focus()
    }
    autoResize()
  })
}

function removeSkill(id: string) {
  selectedSkillIds.value = selectedSkillIds.value.filter(sid => sid !== id)
}

// ── Agent 执行轨迹 ──
const agentStore = useAgentStore()

function isLiveAgentRun(msg: DisplayMsg, idx: number): boolean {
  return !!props.agentRunning &&
    idx === props.messages.length - 1 &&
    msg.role === 'assistant' &&
    msg.id?.startsWith('temp-agent-')
}

// ── 面板拖拽 ──
const { panelWidth, startResize } = useResizablePanel()

// ── 模型选择器 ──
const { showModelDropdown, modelSelectorRef, currentModelName, selectModel } = useModelSelector({
  getSelectedId: () => props.selectedModelId,
  setSelectedId: (id) => emit('update:selectedModelId', id),
  models: () => props.models,
})
const { openSettings } = useSettings()

function handleSelectModel(id: string) {
  selectModel(id)
}

// ── 消息显示 ──
// 优化：仅对最后一条消息计算 live 状态，其余消息保持原始引用避免 O(n) 重渲染
const displayMessages = computed(() => {
  const lastIdx = props.messages.length - 1
  return props.messages.map((m, idx) => {
    if (idx === lastIdx && isLiveAgentRun(m, idx)) {
      return {
        ...m,
        // Agent 运行中：实时同步 agentStore 的 content/reasoning
        // C-review NIT：原实现 reasoning 强制空串、注释声称"外层 ThinkingBlock
        // 展示"，实际不存在该展示（运行中 reasoning 完全丢失）。现透传
        // agentStore.reasoning，由 MessageItem 的 ThinkingBlock 渲染。
        content: agentStore.content || m.content || m.streamingContent || '',
        streamingContent: undefined,
        reasoning: agentStore.reasoning || '',
        streamingReasoning: undefined,
        // 运行中的实时轨迹：不注入这一份，气泡里的内联轨迹要等到整轮跑完才出现 ——
        // 期间用户看不到任何工具调用进度（AgentTraceInline 的 isLive 分支也因此永远
        // 走不到）。lastRunId 必须清空：它是「查看轨迹」按钮的显示条件，而按钮拉的是
        // 后端已固化的 run，运行中点开只会拿到半截结果并被组件缓存住不再刷新。
        agentTrace: { ...agentStore.snapshot(), lastRunId: '' },
      }
    }
    // 非活跃消息保持原始引用，避免不必要的重渲染
    return m
  })
})

// ── 滚动 ──
async function scrollToBottom() {
  if (!settingsStore.settings.chat.autoScroll) return
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

watch(() => props.messages.length, () => {
  scrollToBottom()
})

// Agent 运行中 content/reasoning 通过 agentStore 实时更新，需单独监听以触发滚动
watch(() => agentStore.content, () => {
  if (agentStore.isRunning) scrollToBottom()
})

// reasoning 流式输出同样需要跟随滚动
watch(() => agentStore.reasoning, () => {
  if (agentStore.isRunning) scrollToBottom()
})

// ── 输入处理 ──
function handleKeydown(e: KeyboardEvent) {
  // C-review M16 修复：IME 组合输入期间不拦截按键，
  // 否则中文输入法按 Enter 确认候选词会同时触发发送/技能选择
  if (e.isComposing || e.keyCode === 229) return
  // + 号菜单激活时优先处理菜单导航（与下方技能菜单同手感）
  if (showPlusMenu.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      stepPlusMenu(1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      stepPlusMenu(-1)
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      const key = CAP_KEYS[plusActiveIdx.value]
      if (key) pickCap(key)
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closePlusMenu(true)
      return
    }
  }
  // 技能菜单激活时优先处理菜单导航
  if (showSkillMenu.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      skillActiveIdx.value = (skillActiveIdx.value + 1) % Math.max(filteredSkills.value.length, 1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      skillActiveIdx.value = (skillActiveIdx.value - 1 + Math.max(filteredSkills.value.length, 1)) % Math.max(filteredSkills.value.length, 1)
      return
    }
    if (e.key === 'Enter' && filteredSkills.value.length > 0) {
      e.preventDefault()
      pickSkill(filteredSkills.value[skillActiveIdx.value])
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closeSkillMenu()
      return
    }
    // Tab 也用于选择
    if (e.key === 'Tab' && filteredSkills.value.length > 0) {
      e.preventDefault()
      pickSkill(filteredSkills.value[skillActiveIdx.value])
      return
    }
  }
  // 发送快捷键：enter=Enter 发送(Shift+Enter 换行)，shiftEnter=Shift+Enter 发送(Enter 换行)
  if (e.key === 'Enter') {
    const wantShift = settingsStore.settings.chat.sendShortcut === 'shiftEnter'
    if (wantShift ? e.shiftKey : !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }
}

function autoResize() {
  const el = inputRef.value
  if (el) {
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 100) + 'px'
  }
}

/** 是否可发送：有输入文字或有待发送附件（与按钮 disabled 保持一致） */
const canSend = computed(() =>
  inputText.value.trim().length > 0 || pendingFiles.value.length > 0,
)

// M18：草稿快照 — 发送后立即清空（保留"发送即清空"体验），父层 send 失败时
// 由 restoreDraft() 回填，避免长文/附件在"后端未就绪/无模型/建会话失败"早退路径丢失
const lastDraft = ref<{ text: string; skillIds: string[]; attachments: ChatAttachment[] } | null>(null)

function handleSend() {
  const text = inputText.value.trim()
  if ((!text && pendingFiles.value.length === 0)) return
  // AI 运行中时给出明确反馈，不再静默吞掉
  if (props.agentRunning) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  // 关闭可能打开的菜单
  closeSkillMenu()
  closePlusMenu()
  closePermMenu()
  // 复制 skillIds / attachments 后立即清空，避免下次发送残留
  const skillIds = [...selectedSkillIds.value]
  const attachments = [...pendingFiles.value]
  lastDraft.value = { text, skillIds, attachments }
  selectedSkillIds.value = []
  pendingFiles.value = []
  inputText.value = ''
  if (inputRef.value) inputRef.value.style.height = 'auto'
  emit('send', text, skillIds, attachments, activeMode.value, approvalMode.value)
}

/** M18：父层发送被拒绝（早退）时调用，把草稿回填输入框供用户重试 */
function restoreDraft() {
  if (!lastDraft.value) return
  const { text, skillIds, attachments } = lastDraft.value
  lastDraft.value = null
  inputText.value = text
  selectedSkillIds.value = skillIds
  pendingFiles.value = attachments
  nextTick(() => {
    autoResize()
    inputRef.value?.focus()
  })
}

// ── 历史事件 ──
function handleSwitch(id: string) {
  emit('switch-conversation', id)
  showHistoryPanel.value = false
}

function handleDelete(id: string) {
  emit('delete-conversation', id)
}

// ── 暴露方法（供父组件调用） ──
/** 预填输入框草稿（「问 AI」等外部入口注入问题草稿，用户可修改后发送） */
function prefill(text: string) {
  if (!text) return
  inputText.value = text
  nextTick(() => {
    autoResize()
    inputRef.value?.focus()
  })
}

defineExpose({ restoreDraft, prefill })
</script>

<style scoped>
.side-chat-panel {
  background: transparent;
  border-left: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
  font-family: var(--font-sans);
  position: relative;
}

/* Resize Handle */
.resize-handle {
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 4px;
  cursor: col-resize;
  z-index: 5;
  transition: background 0.15s;
}

.resize-handle:hover,
.resize-handle:active {
  background: var(--accent-primary);
}

/* Header */
.panel-header {
  height: 34px;
  display: flex;
  align-items: center;
  padding: 0 4px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  gap: 8px;
}

.header-spacer {
  flex: 1;
}

.panel-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.header-actions {
  display: flex;
  gap: 2px;
}

.icon-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
  transition: background 0.15s, color 0.15s;
}

.icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.icon-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.icon-btn.loading {
  pointer-events: none;
}

.icon-btn .spinner {
  animation: spin 0.8s linear infinite;
}

.icon-btn svg {
  width: 14px;
  height: 14px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Main Content */
.main-content {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  position: relative;
}

/* Empty State */
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
  gap: 12px;
}

.empty-icon-wrapper {
  width: 64px;
  height: 64px;
  background: var(--bg-hover);
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent-indigo);
  margin-bottom: 8px;
}

.empty-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.empty-desc {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0;
  text-align: center;
  line-height: 1.5;
}

/* Messages List */
.messages-list {
  padding: 12px 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* Input Section */
.input-section {
  border-top: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.input-container {
  padding: 0 4px;
  border-bottom: 1px solid var(--border-subtle);
  position: relative;
}

.chat-input {
  width: 100%;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  padding: 10px 0;
  resize: none;
  outline: none;
  min-height: 20px;
  max-height: 120px;
  line-height: 1.5;
  box-sizing: border-box;
}

.chat-input::placeholder {
  color: var(--text-tertiary);
  font-size: 12px;
  opacity: 0.7;
}

.chat-input:disabled {
  opacity: 0.5;
}

/* focus 指示环已移除：输入框自带 outline:none，focus-visible 的彩色
   outline 会在聚焦时画出明显的颜色框，与无边框输入区风格不符 */

/* ── 已选技能标签 ── */
.skill-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px 0 2px;
}

.tag-skill {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  background: var(--accent-indigo-bg);
  color: var(--accent-indigo-soft);
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  max-width: 160px;
}

.tag-skill svg {
  flex-shrink: 0;
  color: var(--accent-indigo);
}

.tag-skill .tag-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--accent-indigo-soft);
  cursor: pointer;
  padding: 0;
  margin-left: 2px;
  opacity: 0.6;
}

.tag-skill .tag-remove:hover {
  opacity: 1;
}

/* ── 附件 chips（上传文件）── */
.pending-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 4px 0;
}

.tag-file {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 220px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  font-size: 12px;
}

.tag-file svg {
  flex-shrink: 0;
  color: var(--text-tertiary);
}

.tag-file .file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tag-file .file-size {
  flex-shrink: 0;
  color: var(--text-tertiary);
  font-size: 11px;
}

.hidden-file-input {
  display: none;
}

/* ── 能力入口 + 号（工具栏最左，上传按钮左侧）── */
.plus-wrap {
  position: relative;
  display: flex;
}

.plus-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.plus-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.plus-btn:focus,
.plus-btn:focus-visible {
  outline: none;
  border: 0;
  appearance: none;
  -webkit-appearance: none;
}

.plus-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 展开态：启用色底，+ 号旋转 45° 变 ×（同一按钮闭合，不另设关闭动作） */
.plus-btn[aria-expanded="true"] {
  background: var(--accent-indigo-bg);
  color: var(--accent-indigo);
}

/* ── 权限模式选择器（工具条左侧，上传按钮之后）──
   下拉文字即当前模式的常驻显示；开关入口另在 + 菜单「计划」项，二者同源。
   与 .plus-btn 同底座（24px 高 / radius 5px），常驻文字故宽度自适应。 */
.perm-wrap {
  position: relative;
  display: flex;
}

.perm-btn {
  height: 24px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 6px;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--font-sans);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.perm-btn:hover:not(:disabled),
.perm-btn[aria-expanded="true"] {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.perm-btn:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: 1px;
}

.perm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.perm-btn .perm-ico {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
}

.perm-btn .perm-name {
  font-size: 12px;
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
}

.perm-btn .perm-caret {
  flex-shrink: 0;
  opacity: 0.65;
  transition: transform 0.18s ease;
}

/* 展开时箭头翻转 —— 与「+」号展开时旋转 45° 同属开合反馈 */
.perm-btn[aria-expanded="true"] .perm-caret {
  transform: rotate(180deg);
}

/* 向上展开：工具栏贴面板底部，向下会溢出（同 .plus-menu 的定位策略与层级） */
.perm-menu {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  width: 208px;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
  z-index: 50;
  transform-origin: bottom left;
  animation: plus-menu-in 0.14s ease-out;
}

.perm-opt {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--font-sans);
  text-align: left;
  cursor: pointer;
  transition: background 0.1s;
}

/* 键盘导航会把焦点移进选项，所以 :focus 也必须给可见反馈（只写 :hover 会「看不见光标」） */
.perm-opt:hover,
.perm-opt:focus {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.perm-opt:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: -2px;
}

.perm-opt .po-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.perm-opt .po-name {
  font-size: 12px;
  color: inherit;
  white-space: nowrap;
}

.perm-opt .po-desc {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

.perm-opt .po-check {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  color: var(--accent-primary);
  opacity: 0;
}

.perm-opt .po-check.show {
  opacity: 1;
}

/* ── 模式 chip（工具条左侧，审批下拉之后；目标/计划共用同一套样式）──
   与 .perm-btn 同底座（24px 高），但它是"当前生效的事"而非常驻控件，故给
   粉底描边的胶囊形态；× 是它唯一的关闭入口（能开也能关，才不是摆设）。
   两种模式不辨色，靠 .mc-name 的文字区分：本文件的 --accent-indigo 已被
   菜单选中/高亮占满，再给「计划」另起 hue 会撞色。 */
.chip-wrap {
  position: relative;
  display: flex;
}

.mode-chip {
  display: inline-flex;
  align-items: center;
  height: 24px;
  border-radius: 12px;
  background: var(--accent-pink-bg);
  border: 1px solid var(--accent-pink-border);
  overflow: hidden;
}

.mode-chip.is-open {
  border-color: var(--accent-pink);
}

.mc-body {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 100%;
  padding: 0 2px 0 8px;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  background: transparent;
  color: var(--accent-pink);
  font-family: var(--font-sans);
  font-size: 12px;
  cursor: pointer;
}

.mc-body:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: -2px;
}

.mc-name {
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
}

/* 实心点 = 对应工件（goal.md / plan.md）已落盘、每轮都在注入。只有姿态时不给点：
   点与不点分别对应「有工件」与「只是选了姿态」，二者不可混为一谈。 */
.mc-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-pink);
  flex-shrink: 0;
}

.mc-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 100%;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  background: transparent;
  color: var(--accent-pink);
  opacity: 0.7;
  cursor: pointer;
  transition: opacity 0.15s, background 0.15s;
}

.mc-close:hover {
  opacity: 1;
  background: var(--bg-hover);
}

.mc-close:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: -2px;
}

/* 详情浮层：向上展开（工具条贴面板底部），与 .perm-menu 同定位策略与层级 */
.chip-pop {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  width: 280px;
  padding: 10px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
  z-index: 50;
  transform-origin: bottom left;
  animation: plus-menu-in 0.14s ease-out;
}

.chip-pop .cp-head {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.chip-pop .cp-body {
  margin: 0;
  max-height: 180px;
  overflow-y: auto;
  font-family: var(--font-sans);
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.chip-pop .cp-desc {
  margin: 0 0 8px;
  font-size: 11px;
  line-height: 1.55;
  color: var(--text-tertiary);
}

.chip-pop .cp-act {
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--accent-pink-border);
  border-radius: 4px;
  background: var(--accent-pink-bg);
  color: var(--accent-pink);
  font-family: var(--font-sans);
  font-size: 12px;
  cursor: pointer;
}

.chip-pop .cp-act:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.chip-pop .cp-act:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: 1px;
}

.plus-btn svg {
  width: 14px;
  height: 14px;
  display: block;
  transition: transform 0.18s ease;
}

.plus-btn[aria-expanded="true"] svg {
  transform: rotate(45deg);
}

/* 向上展开：工具栏贴面板底部，向下会溢出面板；
   视觉与层级对齐既有 .skill-menu（--bg-elevated-solid + --border-default + z-index 50） */
.plus-menu {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  width: 224px;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
  z-index: 50;
  transform-origin: bottom left;
  animation: plus-menu-in 0.14s ease-out;
}

@keyframes plus-menu-in {
  from { opacity: 0; transform: translateY(4px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.plus-menu .menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 8px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  transition: background 0.1s;
}

.plus-menu .menu-item:hover,
.plus-menu .menu-item.is-active {
  background: var(--bg-hover);
}

.plus-menu .menu-item:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: -2px;
}

.plus-menu .mi-ico {
  flex-shrink: 0;
  width: 13px;
  height: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
}

.plus-menu .menu-item:hover .mi-ico,
.plus-menu .menu-item.is-active .mi-ico {
  color: var(--accent-indigo);
}

.plus-menu .mi-name {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
}

.plus-menu .mi-desc {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 子菜单指示 ›：标记「会展开一份清单」这一族（插件 / 技能）。
   注意它只表达**归属**，不表达「能开」—— 插件那行是 disabled，箭头也随整行置灰。
   （.mi-desc 的 margin-left:auto 已把 desc 及其后的箭头一起推到右端） */
.plus-menu .mi-arrow {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  color: var(--text-tertiary);
}

.plus-menu .menu-item:hover .mi-arrow,
.plus-menu .menu-item.is-active .mi-arrow {
  color: var(--accent-indigo);
}

/* 未接入的条目：置灰 + 不参与 hover 反馈，避免看起来可点 */
.plus-menu .menu-item.is-disabled,
.plus-menu .menu-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.plus-menu .menu-item:disabled:hover {
  background: transparent;
}

.plus-menu .menu-item:disabled:hover .mi-ico {
  color: var(--text-secondary);
}

/* disabled 的按钮仍会命中 :hover（CSS 层面不排除 :disabled），
   所以箭头必须显式回落，否则悬停时变强调色 —— 这行就成假启用了 */
.plus-menu .menu-item:disabled:hover .mi-arrow {
  color: var(--text-tertiary);
}

/* 已选中项（单选姿态）：图标与说明转为启用色 */
.plus-menu .menu-item.is-on .mi-ico,
.plus-menu .menu-item.is-on .mi-desc {
  color: var(--accent-indigo);
}

/* 单选选中态：行整体浅紫底 + 对勾显示，对应主流模式选择器的激活视觉 */
.plus-menu .menu-item.is-selected {
  background: var(--accent-indigo-bg);
}

.plus-menu .menu-item.is-selected .mi-name,
.plus-menu .menu-item.is-selected .mi-ico {
  color: var(--accent-indigo);
}

.plus-menu .mi-check {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent-indigo);
  opacity: 0;
  transition: opacity 0.12s;
}

.plus-menu .mi-check.show {
  opacity: 1;
}

@media (prefers-reduced-motion: reduce) {
  .plus-menu { animation: none; }
  .plus-btn svg { transition: none; }
  /* 权限菜单与它的箭头同样受此约束（沿用既有口径：只关动画/位移，颜色过渡保留） */
  .perm-menu { animation: none; }
  .perm-btn .perm-caret { transition: none; }
  /* chip 详情浮层的入场动画同受约束（.mc-close 只有颜色过渡，按本文件既有口径保留） */
  .chip-pop { animation: none; }
}

/* ── / 触发技能菜单 ── */
.skill-menu {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin-bottom: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
  z-index: 50;
  max-height: 280px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.skill-menu-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.skill-menu-header .menu-title {
  color: var(--accent-indigo-soft);
  font-weight: 500;
}

.skill-menu-header .menu-hint {
  font-size: 11px;
  color: var(--text-tertiary);
}

.skill-menu-list {
  overflow-y: auto;
  padding: 4px;
}

.skill-menu-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s;
  gap: 8px;
}

.skill-menu-item:hover,
.skill-menu-item.active {
  background: var(--accent-indigo-hover);
}

.skill-item-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.skill-item-name {
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.skill-item-desc {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.skill-item-cat {
  font-size: 11px;
  color: var(--text-tertiary);
  background: var(--bg-hover);
  padding: 1px 5px;
  border-radius: 3px;
  flex-shrink: 0;
}

.skill-menu-empty {
  padding: 12px;
  text-align: center;
  color: var(--text-tertiary);
  font-size: 11px;
}

/* Input Toolbar
   与 .input-container padding 一致（0 4px），水平起始位与输入文字对齐。
   按钮视觉区 12px，衬底左右各留 4px 空白 → padding: 4px 4px。 */
.input-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 4px;
  gap: 8px;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* 左侧承载 3 个控件（+ / 上传 / 权限模式，常驻文字约 110px）。
   面板可被拖到 minWidth=280px，那里一行放不下 —— 允许左侧自己换行，
   否则 nowrap 的文字会挤出按钮框、盖住旁边的控件（.input-toolbar 不换行）。 */
.toolbar-left {
  flex-wrap: wrap;
  row-gap: 6px;
  min-width: 0;
}

/* Model Selector */
.model-empty-hint {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 8px;
  border: 1px dashed #4d4d50;
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.15s, border-color 0.15s;
}
.model-empty-hint:hover,
.model-empty-hint:focus-visible {
  color: var(--accent-primary);
  border-color: var(--accent-primary);
  outline: none;
}
.model-empty-hint-action {
  color: var(--accent-primary);
  font-weight: 500;
}

.model-selector {
  position: relative;
}

.model-selector-btn {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 24px;
  background: transparent;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  color: var(--text-secondary);
  font-size: 11px;
  padding: 0 4px;
  cursor: pointer;
  font-family: var(--font-sans);
  white-space: nowrap;
  border-radius: 4px;
  transition: background 0.15s, color 0.15s;
}

.model-selector-btn:hover {
  color: var(--text-primary);
}

.model-selector-btn .model-name {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-selector-btn svg {
  width: 10px;
  height: 10px;
  flex-shrink: 0;
}

.model-dropdown {
  position: absolute;
  bottom: 100%;
  left: 50%;
  transform: translateX(-50%);
  margin-bottom: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  padding: 4px;
  min-width: 180px;
  max-width: 260px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  z-index: 30;
}

.model-dropdown-item {
  padding: 4px 8px;
  color: var(--text-primary);
  cursor: pointer;
  transition: background 0.1s;
  min-height: 32px;
  display: flex;
  align-items: center;
  border-radius: 4px;
}

.model-dropdown-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.model-dropdown-item.active {
  color: var(--text-primary);
  background: var(--accent-primary);
}

.model-item-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.model-dropdown-item .model-label {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: 0.2px;
}

/* Send Button */
.send-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  border-radius: 4px;
  background: var(--bg-hover);
  color: var(--text-primary);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
  flex-shrink: 0;
}

.send-btn:hover:not(:disabled) {
  background: var(--bg-active);
  color: var(--text-primary);
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.send-btn svg {
  width: 14px;
  height: 14px;
}

.abort-btn {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 4px;
  background: color-mix(in srgb, var(--accent-red) 15%, transparent);
  color: var(--accent-red);
  cursor: pointer;
  transition: all 0.15s;
  flex-shrink: 0;
}

.abort-btn:hover {
  background: color-mix(in srgb, var(--accent-red) 25%, transparent);
}

.abort-btn svg {
  width: 12px;
  height: 12px;
}

/* ── Toolbar buttons (scoped inside SideChatPanel, not relying on parent) ──
   统一底座：24×24 / radius 4 / 透明底 / 无框 —— +、上传、权限阀三个控件等高同线。
   icon-only 按钮不留横向 padding（语言型如「自动审批」在 .perm-btn 里另外管）。
   Electron + Chromium 会给 <button> 加原生 border/outline/appearance，必须逐项清零。 */
.toolbar-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.toolbar-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.toolbar-btn:focus,
.toolbar-btn:focus-visible {
  outline: none;
  border: 0;
}

.toolbar-btn.active {
  background: var(--accent-indigo-bg);
  color: var(--accent-indigo);
}

.toolbar-btn.active:hover {
  background: var(--accent-indigo-hover);
}

.toolbar-btn svg {
  width: 14px;
  height: 14px;
  display: block;
  flex-shrink: 0;
}

/* 启用自定义背景图时，让右侧面板透明 */
:global(.app.has-background) .side-chat-panel {
  background: transparent !important;
}
</style>
