<template>
  <div class="panel">
    <header class="panel-header">
      <h3>外观</h3>
      <p class="panel-desc">自定义界面主题、强调色与字体</p>
    </header>
    <div class="panel-body">
      <SettingRow label="主题" desc="切换整体界面主题">
        <select v-model="settings.appearance.theme" class="setting-select">
          <!-- 选项由 themeLabels（Record<Theme, string>）枚举：新增主题忘了配文案直接编译报错，
               而不是像原来那样靠手抄 <option> 与 useAppearance 的 Theme 对齐全靠人眼 -->
          <option v-for="(label, value) in themeLabels" :key="value" :value="value">{{ label }}</option>
        </select>
      </SettingRow>

      <SettingRow label="强调色" desc="主色调与高亮色">
        <div class="color-swatches">
          <button
            v-for="c in accentOptions"
            :key="c.value"
            class="swatch-btn"
            :class="{ active: settings.appearance.accent === c.value }"
            :style="{ background: c.color }"
            :title="c.label"
            @click="settings.appearance.accent = c.value"
          />
        </div>
      </SettingRow>

      <SettingRow label="界面字号" desc="整个界面的基础字号（终端字号、消息字号在各自页签单独设）">
        <select v-model.number="settings.appearance.uiFontSize" class="setting-select">
          <option :value="12">12px</option>
          <option :value="13">13px</option>
          <option :value="14">14px</option>
          <option :value="15">15px</option>
        </select>
      </SettingRow>

      <SettingRow label="动效" desc="界面过渡与动画效果">
        <ToggleSwitch v-model="settings.appearance.animation" />
      </SettingRow>

      <SettingRow label="主题渐变色" desc="背景加入强调色氛围渐变（顶部微光 → 主题底色）">
        <ToggleSwitch v-model="settings.appearance.themeGradientEnabled" />
      </SettingRow>

      <template v-if="settings.appearance.themeGradientEnabled">
        <SettingRow label="渐变强度" desc="渐变明显程度（0% = 纯色，100% = 最强）">
          <SettingSlider v-model="settings.appearance.themeGradientStrength" />
        </SettingRow>
        <!-- 开关 on 但强度 0（或 localStorage 脏值被兜成 0）时，实际渐变已消失；
             不"顺手"帮用户关开关（那是隐式 surprise），只把状态说破 -->
        <p
          v-if="!Number.isFinite(settings.appearance.themeGradientStrength) || settings.appearance.themeGradientStrength <= 0"
          class="gradient-zero-hint"
        >
          强度为 0：渐变实际未生效，可拖动滑块上调；若不需要渐变请关闭总开关。
        </p>

        <SettingRow label="渐变形态" desc="渐变的整体样式">
          <select v-model="settings.appearance.themeGradientMode" class="setting-select">
            <option v-for="(label, value) in gradientModeLabels" :key="value" :value="value">{{ label }}</option>
          </select>
        </SettingRow>

        <SettingRow v-if="settings.appearance.themeGradientMode !== 'glow'" label="渐变方向" desc="渐变方向（放射形态为起点位置）">
          <select v-model="settings.appearance.themeGradientDirection" class="setting-select">
            <option v-for="(label, value) in gradientDirectionLabels" :key="value" :value="value">{{ label }}</option>
          </select>
        </SettingRow>

        <SettingRow label="起始颜色" desc="渐变起点颜色（勾选则跟随强调色）">
          <div class="gradient-color-row">
            <input
              type="color"
              :value="settings.appearance.themeGradientStartColor || DEFAULT_GRADIENT_START"
              :disabled="followStart"
              @input="onStartColorInput"
            />
            <label class="gradient-follow">
              <input type="checkbox" v-model="followStart" />
              跟随强调色
            </label>
          </div>
        </SettingRow>

        <SettingRow label="结束颜色" desc="渐变终点颜色（勾选则跟随主题底色）">
          <div class="gradient-color-row">
            <input
              type="color"
              :value="settings.appearance.themeGradientEndColor || DEFAULT_GRADIENT_END"
              :disabled="followEnd"
              @input="onEndColorInput"
            />
            <label class="gradient-follow">
              <input type="checkbox" v-model="followEnd" />
              跟随主题底色
            </label>
          </div>
        </SettingRow>
      </template>

      <div class="bg-section">
        <SettingRow label="自定义背景图" desc="启用全窗口底层背景">
          <ToggleSwitch v-model="settings.appearance.backgroundEnabled" />
        </SettingRow>

        <SettingRow label="背景图片" desc="选择本地图片作为背景">
          <div class="bg-control">
            <button class="bg-btn" @click="selectBackgroundImage">
              {{ settings.appearance.backgroundImage ? '更换图片' : '选择图片' }}
            </button>
            <button
              v-if="settings.appearance.backgroundImage"
              class="bg-btn danger"
              @click="clearBackgroundImage"
            >
              清除
            </button>
          </div>
        </SettingRow>

        <div v-if="previewUrl" class="bg-preview-row">
          <img :src="previewUrl" class="bg-preview" alt="背景预览" />
        </div>

        <SettingRow label="背景透明度" desc="图片不透明度（0% = 完全隐藏）">
          <SettingSlider v-model="settings.appearance.backgroundOpacity" />
        </SettingRow>

        <SettingRow label="暗色遮罩" desc="保证文字和代码可读性">
          <SettingSlider v-model="settings.appearance.backgroundOverlay" />
        </SettingRow>
      </div>

      <div class="terminal-section">
        <SettingRow label="终端字号" desc="SSH 本地/远程终端文字大小">
          <SettingSlider v-model="settings.appearance.terminal.fontSize" :min="12" :max="18" unit="px" />
        </SettingRow>

        <SettingRow label="终端字体" desc="SSH 本地/远程终端等宽字体（未安装将回退）">
          <select v-model="settings.appearance.terminal.fontFamily" class="setting-select">
            <option v-for="f in terminalFontOptions" :key="f.value" :value="f.value">{{ f.label }}</option>
          </select>
        </SettingRow>

        <SettingRow label="AI 命令预测" desc="基于上下文智能预测下一步命令（需配置 AI 模型）">
          <ToggleSwitch v-model="settings.appearance.terminal.aiPredictionEnabled" />
        </SettingRow>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import SettingSlider from '@/components/settings/fields/SettingSlider.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'
import { TERMINAL_FONT_OPTIONS } from '@/composables/useTerminalConfig'
import { ACCENT_MAP, type Accent, type GradientDirection, type GradientMode, type Theme } from '@/composables/useAppearance'

const settingsStore = useSettingsStore()
const settings = settingsStore.settings
const toast = useToastStore()
const previewUrl = ref('')

// ── 主题渐变自定义（颜色跟随默认的占位色仅用于色板显示） ──
const DEFAULT_GRADIENT_START = '#818cf8'
const DEFAULT_GRADIENT_END = '#0f0f0f'
const followStart = ref(!settings.appearance.themeGradientStartColor)
const followEnd = ref(!settings.appearance.themeGradientEndColor)

function onStartColorInput(e: Event) {
  settings.appearance.themeGradientStartColor = (e.target as HTMLInputElement).value
  followStart.value = false
}

function onEndColorInput(e: Event) {
  settings.appearance.themeGradientEndColor = (e.target as HTMLInputElement).value
  followEnd.value = false
}

// 勾选“跟随默认”时清空自定义色（空串 = 跟随）
watch(followStart, (v) => { if (v) settings.appearance.themeGradientStartColor = '' })
watch(followEnd, (v) => { if (v) settings.appearance.themeGradientEndColor = '' })
// 防竞态：快速连续换图时，旧请求可能后完成覆盖新请求的结果
let previewLoadSeq = 0

async function loadPreview(fileName: string) {
  if (!fileName) {
    previewUrl.value = ''
    return
  }
  if (!window.electronAPI?.fsLoadBackgroundImage) {
    previewUrl.value = ''
    return
  }
  const mySeq = ++previewLoadSeq
  try {
    const newUrl = await window.electronAPI.fsLoadBackgroundImage(fileName)
    // 防竞态：若期间已有更新的请求发起，丢弃本次结果
    if (mySeq !== previewLoadSeq) return
    previewUrl.value = newUrl
  } catch (e: any) {
    if (mySeq !== previewLoadSeq) return
    toast.error(e.message || '加载预览失败')
    previewUrl.value = ''
  }
}

async function selectBackgroundImage() {
  if (!window.electronAPI?.fsSelectImage) {
    toast.error('当前环境不支持文件选择')
    return
  }
  try {
    const result = await window.electronAPI.fsSelectImage()
    if (result?.fileName) {
      settings.appearance.backgroundImage = result.fileName
      settings.appearance.backgroundEnabled = true
      // 递增版本号：fs:selectImage 生成的文件名固定为 background.xxx，
      // 连续两次选同格式图片时文件名不变，App.vue 的 watch 不会触发；
      // 用版本号强制触发重新加载 data URL
      // 预览图由下方 watch 监听 [backgroundImage, backgroundVersion] 自动刷新，无需显式调用
      settings.appearance.backgroundVersion++
    }
  } catch (e: any) {
    toast.error(e.message || '选择图片失败')
  }
}

async function clearBackgroundImage() {
  const fileName = settings.appearance.backgroundImage
  if (fileName && window.electronAPI?.fsDeleteBackgroundImage) {
    try {
      await window.electronAPI.fsDeleteBackgroundImage(fileName)
    } catch (e: any) {
      // Bug 3 修复：删除文件失败时中止清空操作，保持设置与磁盘文件一致，避免磁盘残留且 UI 指向空
      toast.error(e.message || '删除背景图文件失败，已中止清空')
      return
    }
  }
  settings.appearance.backgroundImage = ''
  settings.appearance.backgroundEnabled = false
  previewUrl.value = ''
}

// 监听 [backgroundImage, backgroundVersion]：backgroundVersion 用于处理同格式图片换图场景
// （文件名不变如 background.png → background.png，仅版本号变化）
watch(
  () => [
    settings.appearance.backgroundImage,
    settings.appearance.backgroundVersion,
  ] as const,
  ([fileName]) => loadPreview(fileName)
)
onMounted(() => loadPreview(settings.appearance.backgroundImage))

// 文案表按 Record<联合, string> 写：漏一条 = 编译错误，多一条 = 类型错误。
// 强调色的 hex 直接取 ACCENT_MAP（原先面板另抄一份 hex，蓝色已经和实际下发的
// #0078d4 不一致：色板显示 #58a6ff，点下去界面却是另一个蓝）。
const themeLabels: Record<Theme, string> = {
  dark: '深色',
  light: '浅色',
  'blue-screen': '蓝屏',
  forest: '森林',
  mint: '薄荷',
  bee: '蜜蜂',
  coral: '珊瑚',
  system: '跟随系统',
}

const accentLabels: Record<Accent, string> = {
  blue: '蓝色',
  purple: '紫色',
  green: '绿色',
  orange: '橙色',
  white: '白色',
  forest: '森林',
  mint: '薄荷',
  bee: '蜜蜂',
  coral: '珊瑚',
}

const accentOptions = (Object.keys(accentLabels) as Accent[]).map(value => ({
  value,
  label: accentLabels[value],
  color: ACCENT_MAP[value].hex,
}))

const gradientModeLabels: Record<GradientMode, string> = {
  glow: '顶部微光',
  linear: '线性渐变',
  radial: '放射渐变',
}

const gradientDirectionLabels: Record<GradientDirection, string> = {
  tb: '上 → 下',
  bt: '下 → 上',
  lr: '左 → 右',
  rl: '右 → 左',
  tlbr: '左上 → 右下',
  trbl: '右上 → 左下',
}

// 终端字体选项
const terminalFontOptions = TERMINAL_FONT_OPTIONS
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
/* 面板骨架与 .setting-select 见 settings-panel-base.css */

/* 颜色样本 */
.color-swatches {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.swatch-btn {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 2px solid transparent;
  cursor: pointer;
  transition: border-color 0.15s ease, transform 0.15s ease;
  outline: none;
  padding: 0;
}
.swatch-btn:hover { transform: scale(1.15); }
.swatch-btn.active {
  border-color: #ffffff;
  box-shadow: 0 0 0 2px rgba(var(--accent-primary-rgb), 0.4);
}

.gradient-zero-hint {
  margin: -4px 0 8px;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-secondary);
  background: var(--bg-input);
  border-left: 2px solid var(--accent-primary);
  border-radius: 3px;
}

/* 渐变颜色行 */
.gradient-color-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.gradient-color-row input[type="color"] {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--border-input);
  border-radius: 4px;
  background: var(--bg-input);
  cursor: pointer;
}
.gradient-color-row input[type="color"]:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.gradient-follow {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}
.gradient-follow input[type="checkbox"] {
  width: 13px;
  height: 13px;
  accent-color: var(--accent-primary);
  margin: 0;
}

/* 背景图设置区域 */
.bg-section {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}

/* 终端设置区域 */
.terminal-section {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}

.bg-control {
  display: flex;
  align-items: center;
  gap: 8px;
}

.bg-btn {
  height: 28px;
  padding: 0 12px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
  outline: none;
}

.bg-btn:hover {
  border-color: var(--border-default);
  background: var(--bg-hover);
}

.bg-btn.danger {
  color: var(--danger-text);
  border-color: var(--border-input);
}

.bg-btn.danger:hover {
  border-color: var(--danger-text);
  background: rgba(248, 81, 73, 0.1);
}

.bg-preview-row {
  padding: 8px 0 8px 0;
  display: flex;
  justify-content: flex-end;
}

.bg-preview {
  width: 120px;
  height: 72px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-input);
  background: var(--bg-input);
}
</style>
