<template>
  <div class="panel">
    <header class="panel-header">
      <h3>外观</h3>
      <p class="panel-desc">自定义界面主题、强调色与字体</p>
    </header>
    <div class="panel-body">
      <SettingRow label="主题" desc="切换整体界面主题">
        <select v-model="settings.appearance.theme" class="setting-select">
          <option value="dark">深色</option>
          <option value="light">浅色</option>
          <option value="blue-screen">蓝屏</option>
          <option value="forest">森林</option>
          <option value="mint">薄荷</option>
          <option value="bee">蜜蜂</option>
          <option value="coral">珊瑚</option>
          <option value="system">跟随系统</option>
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

      <SettingRow label="字体大小" desc="界面整体字号">
        <select v-model.number="settings.appearance.fontSize" class="setting-select">
          <option :value="12">12px</option>
          <option :value="13">13px</option>
          <option :value="14">14px</option>
          <option :value="15">15px</option>
        </select>
      </SettingRow>

      <SettingRow label="侧边栏宽度" desc="Work 模式左侧栏宽度">
        <div class="slider-row">
          <input
            type="range"
            v-model.number="settings.appearance.sidebarWidth"
            min="200"
            max="320"
            step="4"
            class="setting-range"
          />
          <span class="slider-value">{{ settings.appearance.sidebarWidth }}px</span>
        </div>
      </SettingRow>

      <SettingRow label="紧凑模式" desc="减少内边距，显示更多内容">
        <ToggleSwitch v-model="settings.appearance.compact" />
      </SettingRow>

      <SettingRow label="动效" desc="界面过渡与动画效果">
        <ToggleSwitch v-model="settings.appearance.animation" />
      </SettingRow>

      <SettingRow label="主题渐变色" desc="背景加入强调色氛围渐变（顶部微光 → 主题底色）">
        <ToggleSwitch v-model="settings.appearance.themeGradientEnabled" />
      </SettingRow>

      <template v-if="settings.appearance.themeGradientEnabled">
        <SettingRow label="渐变强度" desc="渐变明显程度（0% = 纯色，100% = 最强）">
          <div class="slider-row">
            <input
              type="range"
              v-model.number="settings.appearance.themeGradientStrength"
              min="0"
              max="100"
              step="1"
              class="setting-range"
            />
            <span class="slider-value">{{ settings.appearance.themeGradientStrength }}%</span>
          </div>
        </SettingRow>

        <SettingRow label="渐变形态" desc="渐变的整体样式">
          <select v-model="settings.appearance.themeGradientMode" class="setting-select">
            <option value="glow">顶部微光</option>
            <option value="linear">线性渐变</option>
            <option value="radial">放射渐变</option>
          </select>
        </SettingRow>

        <SettingRow v-if="settings.appearance.themeGradientMode !== 'glow'" label="渐变方向" desc="渐变方向（放射形态为起点位置）">
          <select v-model="settings.appearance.themeGradientDirection" class="setting-select">
            <option value="tb">上 → 下</option>
            <option value="bt">下 → 上</option>
            <option value="lr">左 → 右</option>
            <option value="rl">右 → 左</option>
            <option value="tlbr">左上 → 右下</option>
            <option value="trbl">右上 → 左下</option>
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
          <div class="slider-row">
            <input
              type="range"
              v-model.number="settings.appearance.backgroundOpacity"
              min="0"
              max="100"
              step="1"
              class="setting-range"
            />
            <span class="slider-value">{{ settings.appearance.backgroundOpacity }}%</span>
          </div>
        </SettingRow>

        <SettingRow label="暗色遮罩" desc="保证文字和代码可读性">
          <div class="slider-row">
            <input
              type="range"
              v-model.number="settings.appearance.backgroundOverlay"
              min="0"
              max="100"
              step="1"
              class="setting-range"
            />
            <span class="slider-value">{{ settings.appearance.backgroundOverlay }}%</span>
          </div>
        </SettingRow>
      </div>

      <div class="terminal-section">
        <SettingRow label="终端字号" desc="SSH 本地/远程终端文字大小">
          <div class="slider-row">
            <input
              type="range"
              v-model.number="settings.appearance.terminal.fontSize"
              min="12"
              max="18"
              step="1"
              class="setting-range"
            />
            <span class="slider-value">{{ settings.appearance.terminal.fontSize }}px</span>
          </div>
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
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'
import { TERMINAL_FONT_OPTIONS } from '@/composables/useTerminalConfig'

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

type Accent = 'blue' | 'purple' | 'green' | 'orange' | 'white' | 'forest' | 'mint' | 'bee' | 'coral'

const accentOptions: { value: Accent; label: string; color: string }[] = [
  { value: 'blue',   label: '蓝色', color: '#58a6ff' },
  { value: 'purple', label: '紫色', color: '#c586c0' },
  { value: 'green',  label: '绿色', color: '#4ec9b0' },
  { value: 'orange', label: '橙色', color: '#ce9178' },
  { value: 'white',  label: '白色', color: '#ffffff' },
  { value: 'forest', label: '森林', color: '#2ea44f' },
  { value: 'mint',   label: '薄荷', color: '#5eead4' },
  { value: 'bee',    label: '蜜蜂', color: '#f5c542' },
  { value: 'coral',  label: '珊瑚', color: '#ff7b72' },
]

// 终端字体选项
const terminalFontOptions = TERMINAL_FONT_OPTIONS
</script>

<style scoped>
.panel { height: 100%; display: flex; flex-direction: column; }
.panel-header { margin-bottom: 20px; flex-shrink: 0; }
.panel-header h3 {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.panel-desc {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
}
.panel-body {
  flex: 1;
  overflow-y: auto;
  padding-right: 4px;
}

/* 下拉选择 */
.setting-select {
  height: 28px;
  padding: 0 24px 0 8px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm, 4px);
  color: var(--text-primary);
  font-size: 12px;
  font-family: inherit;
  outline: none;
  cursor: pointer;
  -webkit-appearance: none;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12' fill='none' stroke='%238b949e' stroke-width='1.5' stroke-linecap='round'%3E%3Cpolyline points='3 5 6 8 9 5'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 6px center;
  transition: border-color 0.15s ease;
}
.setting-select:hover { border-color: var(--border-default); }
.setting-select:focus { border-color: var(--accent-primary); }

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

/* 滑块行 */
.slider-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.setting-range {
  width: 120px;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--bg-slider);
  border-radius: 2px;
  outline: none;
  cursor: pointer;
}
.setting-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--accent-primary);
  border: 2px solid var(--bg-input);
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.4);
}
.slider-value {
  font-size: 12px;
  color: var(--text-secondary);
  min-width: 40px;
  text-align: right;
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
  border-radius: var(--radius-sm, 4px);
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
  color: var(--text-danger, #f85149);
  border-color: var(--border-input);
}

.bg-btn.danger:hover {
  border-color: var(--text-danger, #f85149);
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
  border-radius: var(--radius-sm, 4px);
  border: 1px solid var(--border-input);
  background: var(--bg-input);
}
</style>
