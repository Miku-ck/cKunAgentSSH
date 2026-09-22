<template>
  <div class="panel">
    <header class="panel-header">
      <h3>记忆</h3>
      <p class="panel-desc">Agent 长期记忆的写入策略与容量上限 —— 影响每次运行注入到 system prompt 的语义条目</p>
    </header>

    <div class="panel-body">
      <SettingRow label="记忆总开关" desc="关闭后 Agent 运行时不再注入历史记忆，也不会从对话沉淀新记忆">
        <ToggleSwitch v-model="memory.enabled" />
      </SettingRow>

      <SettingRow label="沉淀策略" desc="auto：任务完成后由模型自主提炼；manual：仅显式指令时写入">
        <select v-model="memory.strategy" class="setting-select">
          <option value="auto">自动（auto）</option>
          <option value="manual">手动（manual）</option>
        </select>
      </SettingRow>

      <SettingRow label="容量上限" desc="保留最近多少条 semantic 记忆，超出后按最旧最少使用清理">
        <div class="slider-row">
          <input
            type="range"
            v-model.number="memory.maxItems"
            min="10"
            max="500"
            step="10"
            class="setting-range"
          />
          <span class="slider-value">{{ memory.maxItems }} 条</span>
        </div>
      </SettingRow>

      <div class="section-divider" />

      <div class="memory-stats">
        <span class="stats-label">当前记忆状态</span>
        <span class="stats-value">{{ statsText }}</span>
      </div>
      <p class="foot-hint">
        修改即时同步到后端；同步失败时自动回滚到上一次成功值。已有记忆的浏览与删除请通过 AI 面板顶部的"记忆"入口。
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * A2：记忆面板 —— 把 settings.memory 从"存在但无 UI 入口"补齐为可达可改。
 *
 * 三个字段全部通过 settings store 的 deep watch 自动同步（含 R6 回滚基线），
 * 面板自身不再直接调 updateMemorySettings，避免与 watcher 双写。
 * 记忆体本身（列表/删除/prune）已在 AgentMemoryView（AI 面板浮层）里，不重复。
 */
import { computed, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import ToggleSwitch from '@/components/settings/fields/ToggleSwitch.vue'

const settingsStore = useSettingsStore()
const memory = computed(() => settingsStore.settings.memory)

// stats 后端已有 /agent/memory/stats；这里只做零依赖的口径显示，
// 拉取失败或未上线时降级为"未知"，不让面板整体空态。
const statsLine = computed(() => {
  const m = memory.value
  return m.enabled
    ? `启用 · ${m.strategy === 'auto' ? '自动沉淀' : '仅手动写入'} · 上限 ${m.maxItems} 条`
    : '已关闭 · Agent 运行时不注入也不写入'
})
const statsText = statsLine

onMounted(() => {
  // loadMemorySettings 已在应用启动路径调用；面板打开时若字段仍为默认（enabled=true/strategy=auto/maxItems=100），
  // 再补拉一次以覆盖"启动时后端尚未就绪 → 静默降级到 localStorage"的场景。
  void settingsStore.loadMemorySettings()
})
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
.setting-select {
  height: 28px;
  padding: 0 24px 0 8px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
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
.slider-row { display: flex; align-items: center; gap: 12px; }
.setting-range {
  width: 160px; height: 4px;
  -webkit-appearance: none; appearance: none;
  background: var(--bg-slider);
  border-radius: 2px; outline: none; cursor: pointer;
}
.setting-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--accent-primary);
  border: 2px solid var(--bg-input);
  box-shadow: 0 1px 3px rgba(0,0,0,0.35);
  cursor: pointer;
}
.slider-value {
  font-size: 12px; color: var(--text-secondary);
  min-width: 48px; text-align: right;
}
.section-divider {
  height: 1px; background: var(--border-subtle);
  margin: 20px 0;
}
.memory-stats {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px;
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  font-size: 12px;
}
.stats-label { color: var(--text-secondary); }
.stats-value { color: var(--text-primary); font-weight: 500; }
.foot-hint {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  line-height: 1.5;
}
</style>
