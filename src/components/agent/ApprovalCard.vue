<script setup lang="ts">
/**
 * ApprovalCard — 一张完整的授权卡，消费方是输入区上方的 ApprovalDock
 *
 * 从 AgentRunPanel 原样搬出，不是重写：读的数据（req）、四档动作的开放条件
 * （grantScopesForRisk）、prod 警示开关（E5）、超时卡仍可操作（裁决A）、说明文案
 * 全部原样带过来。审批文案属于"用户据此点按钮"的东西，抄第二份就会开始漂移。
 *
 * 审批是运行期的瞬时决策，不是执行记录：请求不写入 agentTrace / agent_runs，
 * run 收尾即清空；轨迹页回放的只有工具调用，没有审批卡。全仓只保留这一个
 * 逐条决策位 —— 同一条待决请求画两遍就是两把锁，而两张卡的能力可能不一样。
 *
 * 卡头上原来那枚「忽略」× 已删：它挂的条件是 `!agentStore.isRunning`，而卡片只活在
 * run 运行期间（done/error/abort/_reset 四条出口都把 permissionRequests 清空），
 * 条件永不为真 = 渲染不出来的控件。旁边 AgentRunPanel 人工介入条上的同款 × 是活的
 * （humanHelpRequests 只在 _reset 清，run 结束后仍留着）—— 照抄宿主的条件、没抄它的
 * 生命周期，就是这个家族的来路。要加回来得先定语义：运行中点掉 = 卡没了、后端仍在等
 * 裁决，120s 兜底按拒绝处理，而用户已经没有第二个决策位；只对**已超时**的卡（req.expired）
 * 开放才是安全形态。这属于裁决，不是顺手补。
 *
 * 结算走 useApprovalActions → store 的 approvePermission/denyPermission，
 * 与首页那条计数窄条的批量结算同一份逻辑。
 */
import { computed } from 'vue'
import type { PermissionRequest } from '@/stores/agent'
import { useAgentStore } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { useSettingsStore } from '@/stores/settings'
import { grantScopesForRisk, onceGrantBlocked } from '@/utils/approval-scopes'
import {
  approvalCommand,
  approvalRiskClass,
  approvalRiskLabel,
  approvalTargetLabel,
  approvalWritePreview,
} from '@/utils/approval-display'
import { useApprovalActions } from '@/composables/useApprovalActions'

const props = defineProps<{
  req: PermissionRequest
}>()

const sshStore = useSSHStore()
const { approve: grantPermission, deny: denyRequest } = useApprovalActions()

const req = computed(() => props.req)
/** E5：prod 专属警示的总开关（设置 → Agent 治理 → 运维策略） */
const prodConfirmOn = computed(() => useSettingsStore().settings.agent.prodConfirm)
const targetLabel = computed(() =>
  approvalTargetLabel(props.req, sshStore.sessions, sshStore.connections, '本机'))
const writePreview = computed(() => approvalWritePreview(props.req))
// 构造型写入的 command 是分类占位模板，不是要执行的命令；留给预览区表达
const command = computed(() => (writePreview.value ? '' : approvalCommand(props.req)))
const alwaysLabel = computed(() => {
  if (writePreview.value) return '始终允许写此路径'
  return command.value ? '始终允许此命令' : '始终允许'
})
/** 授权实际绑定的对象 —— 三档动作的 tooltip 都按它措辞，别说成「这条命令」欺骗路径级授权 */
const grantScope = computed(() => {
  if (writePreview.value) return '这个路径的本次写入'
  if (command.value) return '这行命令'
  return '这次工具调用'
})
const alwaysTitle = computed(() => {
  const target = writePreview.value ? '这个路径（之后写什么内容都算）'
    : command.value ? '这行命令' : '这个工具'
  // 主机限域：服务端反查到目标主机时，「始终允许」只写 `@host|键` 一行。
  // 原标题是「今后包括生产环境在内不再询问」—— 主机维度上线后这句话会骗人。
  if (req.value.host) {
    return `把${target}写入本机持久授权清单（仅限主机 ${req.value.host}）：换一台机器仍需重新确认`
  }
  return `把${target}写入本机持久授权清单：今后包括生产环境在内不再询问`
})
/** 工具级审批没有命令可言，别把它的说明写成「这条命令会做什么」 */
const explainLabel = computed(() => {
  if (writePreview.value) return '这次写入会做什么'
  if (req.value.explainSource === 'tool') return '这个工具会做什么'
  return '这条命令会做什么'
})
const hasArgs = computed(() => Object.keys(props.req.args || {}).length > 0)
const riskClass = computed(() => approvalRiskClass(props.req.risk))
// P2 修复（2026-10-11）：once 按 run 键控 —— 后端 run 终态后 once 授权无处消费
// （直接 409）。「允许本次」必须提前禁用，别让人点出一个假「已授权」。
// 卡片虽在四条终态出口清空，但 SSE 迟到/断连/settling 在途都留出点击窗口。
const agentStore = useAgentStore()
const onceBlocked = computed(() => onceGrantBlocked(agentStore.isRunning))
/**
 * 超时角标要说清「现在点批准会发生什么」。
 * 原写「授权仍可恢复」—— 用户读到的是"批了就跑了"，实际是批准只把待办解锁，
 * AI 还得重新发起同一条命令才会执行（后端到期不摘签名，注入文案也这么要求）。
 * run 已终态时更彻底：once 键无处消费（后端 409），这条待办只能靠新一轮运行接走。
 */
const expiredText = computed(() => (onceBlocked.value ? '已超时 · 本轮已结束' : '已超时 · 批准后仍需重新发起'))
const expiredTitle = computed(() => onceBlocked.value
  ? '本轮运行已结束，这条待办无处放行；请重新发起指令后再确认'
  : '兜底窗口内无人裁决，待办已保留（未按拒绝处理）；批准后 AI 需重新发起同一条命令才会执行')
</script>

<template>
  <div class="ap-card ap-perm">
    <div class="ap-head">
      <span class="ap-ttl ap-perm-ttl">AI 请求执行确认</span>
      <span class="ap-risk" :class="riskClass">{{ approvalRiskLabel(req.risk) }}</span>
      <span v-if="req.expired" class="ap-risk-pill ap-timeout-pill" :title="expiredTitle">
        {{ expiredText }}
      </span>
      <span v-if="req.environment" class="ap-env" :class="`env-${req.environment}`">
        {{ req.environment === 'prod' ? '生产环境' : '测试环境' }}
      </span>
      <span class="ap-sp"></span>
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
        <span class="ap-k">{{ explainLabel }}</span>
        <span class="ap-v">{{ req.explain }}</span>
        <span v-if="req.explainSource === 'llm'" class="ap-tag-inline">AI 生成，仅供参考</span>
      </div>
      <div v-if="targetLabel" class="ap-keyval">
        <span class="ap-k">目标主机</span>
        <code class="ap-code">{{ targetLabel }}</code>
      </div>
      <!-- 预登记范围：「始终允许」不再是无条件全局授权，主机可见时必须写清作用域 -->
      <div v-if="req.host" class="ap-keyval">
        <span class="ap-k">预登记范围</span>
        <span class="ap-v">仅主机 <code class="ap-code">{{ req.host }}</code>，换机器仍需确认</span>
      </div>
      <!-- 环境标签的来源：服务端按主机连接配置判定的不显示（默认可信）；
           客户端自报、服务端没核实过的必须写出来 —— 否则一个被声明成 test 的
           prod 主机，审批人只会看到「测试环境」四个字。 -->
      <div
        v-if="req.environmentSource && req.environmentSource !== 'connection'"
        class="ap-keyval ap-env-src"
        :title="req.environmentSourceLabel"
      >
        <span class="ap-k">环境来源</span>
        <span class="ap-v">{{ req.environmentSourceLabel || '客户端声明（未核实）' }}</span>
      </div>
      <div v-if="writePreview" class="ap-write">
        <div class="ap-keyval">
          <span class="ap-k">{{ req.tool === 'file_edit' ? '编辑文件' : '写入文件' }}</span>
          <code class="ap-code ap-code-normal">{{ writePreview.path }}</code>
        </div>
        <template v-if="writePreview.oldText !== undefined">
          <div class="ap-diff-block">
            <span class="ap-k">替换前</span>
            <pre class="ap-diff ap-diff-old">{{ writePreview.oldText }}</pre>
          </div>
          <div class="ap-diff-block">
            <span class="ap-k">替换后</span>
            <pre class="ap-diff ap-diff-new">{{ writePreview.newText }}</pre>
          </div>
        </template>
        <div v-else-if="writePreview.content !== undefined" class="ap-diff-block">
          <span class="ap-k">写入内容</span>
          <pre class="ap-diff">{{ writePreview.content || '（空文件）' }}</pre>
        </div>
        <div v-if="writePreview.truncated" class="ap-preview-note">预览已截断，完整内容见下方「技术详情」。</div>
        <div class="ap-write-note">
          授权只绑定目标路径，不校验本次写入内容；同一路径换一段内容不会再询问。
        </div>
      </div>
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
      <!-- 英文判定 reason 与原始参数收进折叠区：正文留给用户读结论，
           排查要用的原文一条不少，但不该占据第一屏 -->
      <details v-if="hasArgs || req.message" class="ap-args">
        <summary>技术详情</summary>
        <div v-if="req.message" class="ap-tech-line">{{ req.message }}</div>
        <pre v-if="hasArgs">{{ JSON.stringify(req.args, null, 2) }}</pre>
      </details>
    </div>
    <div class="ap-actions">
      <button
        class="ap-btn ap-btn-grant"
        :disabled="req.settling || onceBlocked"
        :title="onceBlocked ? '本轮运行已结束，once 授权无处生效；请重新发起指令后再确认' : `只对本次${grantScope}生效，下次仍会询问`"
        @click="grantPermission(req.id, 'once')"
      >
        {{ req.settling ? '处理中...' : '允许本次' }}
      </button>
      <button
        v-if="grantScopesForRisk(req.risk).includes('session')"
        class="ap-btn ap-btn-session"
        :disabled="req.settling"
        title="本会话内同一目标不再询问（换主机仍需确认）；后端重启即失效，其他会话不受影响"
        @click="grantPermission(req.id, 'session')"
      >
        本会话允许
      </button>
      <button
        class="ap-btn ap-btn-always"
        :disabled="req.settling"
        :title="alwaysTitle"
        @click="grantPermission(req.id, 'always')"
      >
        {{ alwaysLabel }}
      </button>
      <button
        class="ap-btn ap-btn-deny"
        :disabled="req.settling"
        title="拒绝后本次运行内 AI 不会再请求同一目标"
        @click="denyRequest(req.id)"
      >拒绝</button>
    </div>
    <div class="ap-hint">
      {{ req.host
        ? `「始终允许」只写入主机 ${req.host} 的持久授权清单，换一台机器仍需重新确认；拿不准选「允许本次」。`
        : '「始终允许」会写入本机持久授权清单，之后包括生产环境在内都不再询问 —— 拿不准选「允许本次」。' }}
    </div>
  </div>
</template>

<style scoped>
/* 与 ApprovalDock 的 .ad-body 同高：长写入预览在正文里滚动，动作栏与说明条常驻 */
.ap-perm { max-height: min(38vh, 340px); }
</style>
