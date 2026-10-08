<script setup lang="ts">
/**
 * AgentTraceInline — Qoder 风格的 Agent 执行过程内联渲染
 *
 * 设计原则：
 * - 无卡片、无侧边时间线、无分节标签
 * - 状态用文字表达，不用彩色圆点
 * - 扁平日志流：工具日志
 * - 深度思考统一由外层 ThinkingBlock 展示，此处不再重复渲染
 */
import { computed } from 'vue'
import type { AgentTraceSnapshot } from '@/api/client'

const props = withDefaults(defineProps<{
  trace: AgentTraceSnapshot
  isLive?: boolean
}>(), {
  isLive: false,
})

// 工具调用不进这个判定：条件就是 isLive 且无 reasoning，前端没有 hasToolCalls 这一项
const isPreparing = computed(() => props.isLive && !props.trace.reasoning?.trim())
</script>

<template>
  <div class="agent-trace-inline">
    <!-- 扁平日志体：当前仅承载准备中占位提示 -->
    <div class="trace-body">
      <div v-if="isPreparing" class="preparing-row">
        <span class="preparing-text">思考中</span>
        <span class="preparing-dots">
          <span></span><span></span><span></span>
        </span>
      </div>

      <!-- 工具调用情况不在前端显示 -->
    </div>
  </div>
</template>

<style scoped>
.agent-trace-inline {
  margin-bottom: 12px;
  font-family: var(--font-sans);
}

/* 日志体：无缩进，直接承接 */
.trace-body {
  padding: 4px 0 0 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.preparing-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 0;
  font-size: 13px;
  color: var(--accent-primary);
}

.preparing-text {
  font-weight: 400;
}

.preparing-dots {
  display: inline-flex;
  gap: 3px;
}

.preparing-dots span {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--accent-primary);
  animation: preparing-bounce 1.4s infinite ease-in-out both;
}

.preparing-dots span:nth-child(1) {
  animation-delay: -0.32s;
}

.preparing-dots span:nth-child(2) {
  animation-delay: -0.16s;
}

@keyframes preparing-bounce {
  0%, 80%, 100% {
    transform: scale(0.6);
    opacity: 0.4;
  }
  40% {
    transform: scale(1);
    opacity: 1;
  }
}

</style>
