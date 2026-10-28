<template>
  <div class="connection-page">
    <div class="section-heading">
      <h2>连接</h2>
    </div>
    <section class="connection-group" aria-labelledby="connection-channels">
      <h3 id="connection-channels">渠道</h3>
      <QqChannelCard :personas="personas" />
    </section>
  </div>
</template>

<script setup lang="ts">
import QqChannelCard from './QqChannelCard.vue'
import type { BotPersona } from '@/api/bot'

defineProps<{ personas: BotPersona[] }>()
</script>

<style scoped>
.connection-page { flex: 1; min-height: 0; overflow-y: auto; padding: var(--spacing-24); }
.section-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 22px; }
.section-heading h2 { margin: 0; font-size: 1.3846rem; }
.connection-group { display: flex; flex-direction: column; gap: var(--spacing-10); }
.connection-group h3 { margin: 0; font-size: 1.0769rem; }
</style>
