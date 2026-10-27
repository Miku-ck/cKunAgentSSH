<template>
  <div class="setting-row">
    <div class="setting-label">
      <span class="label-text">{{ label }}</span>
      <span v-if="desc" class="label-desc">{{ desc }}</span>
    </div>
    <div class="setting-control">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
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
}
.label-text {
  display: block;
  font-size: 1rem;
  color: var(--text-primary);
  line-height: 1.4;
}
.label-desc {
  display: block;
  font-size: 0.8462rem;
  color: var(--text-secondary);
  margin-top: var(--spacing-2);
  line-height: 1.4;
}
.setting-control {
  flex-shrink: 0;
}
</style>
