<template>
  <BaseModal title="新建目标" @close="emit('close')">
    <p class="gc-hint">
      写下这次任务要达成什么、怎么算成功。目标会贯穿后续每一轮执行，
      完成与否由你最终裁决。
    </p>
    <label class="gc-field">
      目标标题 <span class="gc-optional">选填，≤60 字</span>
      <input v-model="title" class="gc-input" maxlength="60" placeholder="例：恢复 502 故障" />
    </label>
    <label class="gc-field">
      目标陈述 <span class="gc-required">必填，≤2000 字</span>
      <textarea v-model="statement" class="gc-input" rows="3" maxlength="2000"
                placeholder="例：定位并修复线上 502，恢复服务且日志无新增错误" />
    </label>
    <label class="gc-field">
      成功标准 <span class="gc-optional">选填，每行一条，最多 10 条</span>
      <textarea v-model="criteriaText" class="gc-input" rows="4"
                placeholder="站点返回 200&#10;错误日志无新增 502&#10;健康检查通过" />
    </label>
    <label class="gc-field">
      背景与约束 <span class="gc-optional">选填，≤2000 字</span>
      <textarea v-model="context" class="gc-input" rows="2" maxlength="2000"
                placeholder="例：只改测试环境，生产需另行审批" />
    </label>
    <p v-if="error" class="gc-error">{{ error }}</p>
    <template #actions>
      <button class="gc-btn" @click="emit('close')">取消</button>
      <button class="gc-btn primary" :disabled="!canSubmit || submitting" @click="submit">
        {{ submitting ? '建立中…' : '建立目标' }}
      </button>
    </template>
  </BaseModal>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import BaseModal from '@/components/common/BaseModal.vue'
import { useGoalPlanStore } from '@/stores/goal-plan'

const emit = defineEmits<{ close: [] }>()
const goalPlan = useGoalPlanStore()

const title = ref('')
const statement = ref('')
const context = ref('')
const criteriaText = ref('')
const submitting = ref(false)
const error = ref('')

const canSubmit = computed(() => statement.value.trim().length > 0)

async function submit(): Promise<void> {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  error.value = ''
  const criteria = criteriaText.value.split('\n')
    .map(s => s.trim()).filter(Boolean).slice(0, 10)
  try {
    await goalPlan.createGoal({
      title: title.value.trim(),
      statement: statement.value.trim(),
      context: context.value.trim(),
      criteria,
    })
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.gc-hint {
  margin: 0 0 var(--spacing-12);
  color: var(--text-secondary);
  font-size: 0.8462rem;
  line-height: var(--line-height-normal);
}

.gc-field {
  display: block;
  margin-bottom: var(--spacing-10);
  color: var(--text-primary);
  font-size: 0.8462rem;
}

.gc-optional,
.gc-required {
  margin-left: var(--spacing-6);
  color: var(--text-tertiary);
  font-size: 0.7692rem;
}

.gc-required { color: var(--status-warn); }

.gc-input {
  display: block;
  width: 100%;
  box-sizing: border-box;
  margin-top: var(--spacing-4);
  padding: var(--spacing-6) var(--spacing-8);
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font: inherit;
  font-size: 0.8462rem;
  line-height: var(--line-height-normal);
  resize: vertical;
}

.gc-input:focus-visible { outline: 1px solid var(--accent-primary); }

.gc-error {
  margin: 0;
  color: var(--status-error);
  font-size: 0.8462rem;
}

.gc-btn {
  padding: var(--spacing-6) var(--spacing-14);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.8462rem;
  cursor: pointer;
}

.gc-btn.primary {
  border-color: var(--accent-primary);
  background: var(--accent-primary);
  color: var(--accent-on);
}

.gc-btn:disabled { opacity: 0.5; cursor: default; }
</style>
