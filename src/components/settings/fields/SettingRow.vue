<template>
  <div class="setting-row">
    <div class="setting-label">
      <span class="label-text">{{ label }}</span>
      <InfoTip v-if="desc" :text="desc" />
    </div>
    <div class="setting-control">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import InfoTip from '@/components/common/InfoTip.vue'

defineProps<{
  label: string
  desc?: string
}>()
</script>

<style scoped>
.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 36px;
  padding: var(--spacing-8) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.setting-row:last-child {
  border-bottom: none;
}
.setting-label {
  flex: 1;
  min-width: 0;
  margin-right: var(--spacing-16);
  display: flex;
  align-items: center;
  gap: var(--spacing-6);
}
.label-text {
  font-size: 1rem;
  color: var(--text-primary);
  line-height: 1.4;
}
.setting-control {
  flex-shrink: 0;
}
</style>
