<script setup lang="ts">
/**
 * ApprovalCard — 一张完整的授权卡（现在只有过程区内联这一个消费方）
 *
 * 从 AgentRunPanel 原样搬出，不是重写：读的数据（req）、四档动作的开放条件
 * （grantScopesForRisk）、prod 警示开关（E5）、超时卡仍可操作（裁决A）、说明文案
 * 全部原样带过来。搬的唯一动机是运行视图要在事件流里就地批同一条请求 ——
 * 卡体模板留在面板里就等于要抄第二遍，而审批文案这种"用户据此点按钮"的东西
 * 抄一份就会开始漂移。
 *
 * 首页队列原先自己画了一套摘要形态的卡（三档，没有 session 档），C2 裁决后撤掉了：
 * 过程搬进首页使两屏并成一屏，同一条待决请求上下各现一次、两个决策位，而下面这套
 * 是更强的那套（四档 + prod 警示 + 忽略）。逐条决策归这里，队列只留批量与追溯。
 *
 * 结算走 useApprovalActions → store 的 approvePermission/denyPermission，
 * 与首页那条计数窄条的批量结算同一份逻辑。
 */
import { computed } from 'vue'
import { useAgentStore, type PermissionRequest } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { useSettingsStore } from '@/stores/settings'
import { grantScopesForRisk } from '@/utils/approval-scopes'
import { approvalCommand, approvalRiskLabel, approvalTargetLabel } from '@/utils/approval-display'
import { useApprovalActions } from '@/composables/useApprovalActions'

const props = defineProps<{
  req: PermissionRequest
}>()

const agentStore = useAgentStore()
const sshStore = useSSHStore()
const { approve: grantPermission, deny: denyRequest } = useApprovalActions()

const req = computed(() => props.req)
/** E5：prod 专属警示的总开关（设置 → Agent 治理 → 运维策略） */
const prodConfirmOn = computed(() => useSettingsStore().settings.agent.prodConfirm)
const targetLabel = computed(() => approvalTargetLabel(props.req, sshStore.sessions, sshStore.connections))
const command = computed(() => approvalCommand(props.req))
</script>

<template>
  <div class="ap-card ap-perm">
    <div class="ap-head">
      <svg class="ap-ic ap-perm-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      <span class="ap-ttl ap-perm-ttl">AI 请求执行确认</span>
      <span class="ap-risk" :class="`risk-${req.risk}`">{{ approvalRiskLabel(req.risk) }}</span>
      <span v-if="req.expired" class="ap-risk-pill ap-timeout-pill">已超时 · 授权仍可恢复</span>
      <span v-if="req.environment" class="ap-env" :class="`env-${req.environment}`">
        {{ req.environment === 'prod' ? '生产环境' : '测试环境' }}
      </span>
      <span class="ap-sp"></span>
      <button
        v-if="!agentStore.isRunning"
        class="ap-icon-btn"
        title="忽略"
        aria-label="忽略该请求"
        @click="agentStore.dismissPermission(req.id)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="ap-body">
      <!-- 阅读顺序：我要做的事 → 后果 → 目标机器 -->
      <!-- E5：prod 提示受「生产环境额外确认」开关控制。关掉只是不再多问一句，
           风险判定本身（dangerous 审批 / prod 预授权 allowlist / hardline 阻断）不受影响 -->
      <div v-if="req.environment === 'prod' && prodConfirmOn" class="ap-prod-warn">
        目标为生产环境主机，误操作影响线上业务，请格外确认后再授权。
      </div>
      <div v-if="req.task" class="ap-keyval" :title="req.task">
        <span class="ap-k">你的请求</span>
        <span class="ap-v ap-2line">{{ req.task }}</span>
      </div>
      <div v-if="req.explain" class="ap-keyval">
        <span class="ap-k">这条命令会做什么</span>
        <span class="ap-v">{{ req.explain }}</span>
        <span v-if="req.explainSource === 'llm'" class="ap-tag-inline">AI 生成，仅供参考</span>
      </div>
      <div v-if="targetLabel" class="ap-keyval">
        <span class="ap-k">目标主机</span>
        <code class="ap-code">{{ targetLabel }}</code>
      </div>
      <div class="ap-msg">{{ req.message }}</div>
      <div class="ap-keyval">
        <span class="ap-k">工具:</span>
        <code class="ap-code">{{ req.tool }}</code>
        <span class="ap-k ap-k-gap">可逆:</span>
        <span class="ap-v">{{ req.reversible ? '是' : '否' }}</span>
        <template v-if="command">
          <span class="ap-k ap-k-gap">授权命令:</span>
          <code class="ap-code ap-code-normal">{{ command }}</code>
        </template>
      </div>
      <details v-if="req.args && Object.keys(req.args).length > 0" class="ap-args">
        <summary>参数</summary>
        <pre>{{ JSON.stringify(req.args, null, 2) }}</pre>
      </details>
    </div>
    <div class="ap-actions">
      <button class="ap-btn ap-btn-grant" :disabled="req.settling" @click="grantPermission(req.id, 'once')">
        {{ req.settling ? '处理中...' : '允许本次' }}
      </button>
      <button
        v-if="grantScopesForRisk(req.risk).includes('session')"
        class="ap-btn ap-btn-session"
        :disabled="req.settling"
        @click="grantPermission(req.id, 'session')"
      >
        本会话允许
      </button>
      <button class="ap-btn ap-btn-always" :disabled="req.settling" @click="grantPermission(req.id, 'always')">
        {{ command ? '始终允许此命令' : '始终允许' }}
      </button>
      <button class="ap-btn ap-btn-deny" :disabled="req.settling" @click="denyRequest(req.id)">拒绝</button>
    </div>
    <div class="ap-hint">
      <template v-if="command">
        「允许本次」只对这行命令生效，其他高风险命令仍会逐条询问；
        「本会话允许」对本会话内同一命令免问（后端重启即失效）；
        「始终允许」只把这行命令（含其中的目标路径）写入本机持久授权清单，
        今后包括生产环境在内的同一命令不再询问、换一条仍会询问 ——
        拿不准就先选「允许本次」。
      </template>
      <template v-else-if="grantScopesForRisk(req.risk).includes('session')">
        「本会话允许」对本会话内该工具免问（后端重启即失效），其他会话不受影响。
      </template>
      拒绝后本次运行内 AI 不会再请求同一条命令。
    </div>
  </div>
</template>
