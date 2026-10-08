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
          class="field-hint"
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
              :value="settings.appearance.themeGradientStartColor || followedStart"
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
              :value="settings.appearance.themeGradientEndColor || followedEnd"
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

        <SettingRow label="终端背景" desc="本地/远程终端的背景色方案">
          <select v-model="settings.appearance.terminal.backgroundMode" class="setting-select">
            <option v-for="(label, value) in terminalBgModeLabels" :key="value" :value="value">{{ label }}</option>
          </select>
        </SettingRow>

        <template v-if="settings.appearance.terminal.backgroundMode === 'solid'">
          <SettingRow label="背景颜色" desc="终端纯色背景">
            <div class="gradient-color-row">
              <input type="color" v-model="settings.appearance.terminal.backgroundColor" />
              <span class="bg-color-text">{{ settings.appearance.terminal.backgroundColor }}</span>
            </div>
          </SettingRow>

          <SettingRow label="背景透明度" desc="低于 100% 时与主题底色或全局背景图混合">
            <SettingSlider v-model="settings.appearance.terminal.backgroundOpacity" :min="0" :max="100" unit="%" />
          </SettingRow>

          <!-- 终端 ANSI 主题按约定固定暗色（见 useTerminalConfig 头注），底色却是用户自选的：
               浅色落在深底上还有救（半透明混合），选到不透明的浅底就是白字糊白底。
               不替他改色（那是隐式 surprise），只把状态说破 —— 同上方渐变强度为 0 那条做法。 -->
          <p v-if="terminalBgUnreadable" class="field-hint">
            所选背景过浅：终端文字（固定浅色 ANSI）会读不出来。请把背景改深一档、降低透明度，
            或把「终端背景」切回跟随主题。
          </p>
        </template>

        <SettingRow label="AI 命令预测" desc="基于上下文智能预测下一步命令（需配置 AI 模型）">
          <ToggleSwitch v-model="settings.appearance.terminal.aiPredictionEnabled" />
        </SettingRow>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, computed, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import SettingSlider from '@/components/settings/fields/SettingSlider.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'
import { TERMINAL_FONT_OPTIONS, TERMINAL_FOREGROUND } from '@/composables/useTerminalConfig'
import { ACCENT_MAP, blendHex, contrastRatio, isHexColor, type Accent, type GradientDirection, type GradientMode, type TerminalBgMode, type Theme } from '@/utils/appearance-tokens'

const settingsStore = useSettingsStore()
const settings = settingsStore.settings
const toast = useToastStore()
const previewUrl = ref('')

// ── 主题渐变自定义 ──
// 勾了"跟随"时色板该显示什么：读 documentElement 上那两枚令牌的**当前计算值**，
// 而不是再抄一份占位 hex —— 原来抄的 #0f0f0f 在浅色/手记主题下与真实的终点色
// （白 / 米纸）完全相反，用户看着一块黑去判断一个白底的渐变。
const FOLLOW_FALLBACK = { start: '#818cf8', end: '#0f0f0f' }
const followedStart = ref(FOLLOW_FALLBACK.start)
const followedEnd = ref(FOLLOW_FALLBACK.end)

function refreshFollowedSwatches() {
  const cs = getComputedStyle(document.documentElement)
  const read = (name: string, fallback: string) => {
    const v = cs.getPropertyValue(name).trim()
    return isHexColor(v) ? v : fallback
  }
  followedStart.value = read('--accent-primary', FOLLOW_FALLBACK.start)
  followedEnd.value = read('--bg-base', FOLLOW_FALLBACK.end)
}
// useAppearance 的 watcher 在 App.vue 里更早注册，同一次改动会先它跑完；
// post flush + 挂载各读一次，覆盖"面板打开时已经是某个主题"这条路
watch(() => [settings.appearance.theme, settings.appearance.accent], refreshFollowedSwatches, { flush: 'post' })
onMounted(refreshFollowedSwatches)

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
  atelier: '手记',
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
  verm: '朱砂',
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

/**
 * 自选的终端纯色底与固定浅色 ANSI 前景（TERMINAL_FOREGROUND）是否还读得出来。
 *
 * 透明度必须参与计算：40% 的白落在主题的深终端底上仍是灰，按原色算就是误报。
 * 底层取各套主题 --bg-terminal 里最浅的一档（浅色/手记的 #17181c）作保守下限 ——
 * 真实的底只会更深，算出来的对比度只会更宽，所以这条提示不会漏报也不会多报。
 * 阈值沿用 contrastOnAccent 的 3:1（半粗字形的可读线），不另立一套判据。
 */
const TERMINAL_UNDERLAY = '#17181c'
const terminalBgUnreadable = computed(() => {
  const t = settings.appearance.terminal
  if (t.backgroundMode !== 'solid' || !isHexColor(t.backgroundColor)) return false
  return contrastRatio(
    TERMINAL_FOREGROUND,
    blendHex(t.backgroundColor, t.backgroundOpacity, TERMINAL_UNDERLAY),
  ) < 3
})

const terminalBgModeLabels: Record<TerminalBgMode, string> = {
  auto: '跟随主题与全局背景',
  solid: '自定义纯色',
}
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
  /* 描边取卡面自身的色而不是写死白：浅色主题的卡片就是白的，白环等于没有选中态；
     「白色」还是一枚可选强调色，白圈套白块更是彻底隐形 */
  border-color: var(--bg-elevated-solid);
  box-shadow: 0 0 0 2px rgba(var(--accent-primary-rgb), 0.4);
}

/* 状态说破条（渐变强度为 0 / 终端纯色底过浅共用）：不改用户的设置，只讲清现在实际是什么效果 */
.field-hint {
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
/* 终端背景色当前值文本（色板取色器的回报） */
.bg-color-text {
  font-size: 11px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
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
  background: color-mix(in srgb, var(--danger-text) 10%, transparent);
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
