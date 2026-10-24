/**
 * 长任务结构化 —— 前端纯逻辑核（spec 2026-10-24-task-structuring · 接缝 S3）
 *
 * 与后端 agent/task_structure.py 同一套 provenance 语义；刻意把这层"判断"从组件里
 * 抽出来（对齐仓库 exec-outcome-core / ssh-error-core 等 *-core 惯例）：启发式阈值、
 * 审批回落判定这些安全相关的规则得能被守卫测试逐条钉死，而不是埋在 .vue 里靠肉眼。
 *
 * 不变量（后端 §0 同源）：结构化碰不到审批闸门本身，只碰「意图边界」与「目标主机」。
 * 所以这里只负责：① 什么时候浮现入口（窄，不常驻、不自动烧 token）；
 * ② 采用一份含"新增写步骤"的结构稿时，把 auto 审批强制回落 normal。
 */
import type { ApprovalKey } from './run-modes'

/** provenance 四态 —— 与后端 _VALID_PROVENANCE 同枚举，改一处须同步。 */
export type Provenance = 'verbatim' | 'inferred' | 'completed' | 'added'
export type StepOpKind = 'read' | 'write'

export interface StructureStep {
  id: string
  text: string
  op_kind: StepOpKind
  provenance: Provenance
  requires_confirm: boolean
}

export interface StructureFieldSlot {
  value: string
  provenance: Provenance
  editable: boolean
}

export interface StructureAmbiguity {
  id: string
  question: string
  why: string
  options: string[]
  field_ref?: string | null
}

export interface StructureResponse {
  steps: StructureStep[]
  fields: {
    host: StructureFieldSlot
    environment: 'test' | 'prod'
    impact_scope: StructureFieldSlot
    rollback: StructureFieldSlot
    pre_checks: StructureFieldSlot
    change_window: StructureFieldSlot
  }
  ambiguities: StructureAmbiguity[]
  structured_text: string
  has_added_write_steps: boolean
  meta: { model_used: string; tokens_in: number; tokens_out: number }
}

/**
 * provenance 文案单点定义：输入卡、弹窗、来源徽标各写一份必然漂移（对齐 run-modes.ts
 * 的 APPROVAL_LABELS 口径）。穷举四态 —— 漏一态是编译错误，不是"那态悄悄没标签"。
 */
export const PROVENANCE_LABELS: Record<Provenance, string> = {
  verbatim: '原文',
  inferred: '推断',
  completed: '补全',
  added: '新增·需确认',
}

// ── 启发式触发阈值 ──

/** 结构化入口浮现的最小字符数：短指令（「重启 nginx」）不该看到这个按钮。
 *  单点常量，供测试钉边界。 */
export const STRUCT_MIN_CHARS = 120

const ACTION_VERBS = [
  '重启', '停止', '启动', '删除', '清理', '修改', '部署', '扩容', '回滚', '迁移',
  '备份', '重建', 'reload', 'restart', 'stop', 'start', 'kill', 'rm', 'systemctl',
  'apply', 'install', 'uninstall', 'rotate',
]
const CONNECTORS = ['然后', '接着', '再', '并且', '顺便', '同时', '把', '之后', '先', '最后']
const OPS_HINTS = ['服务', 'nginx', '磁盘', '内存', '日志', '进程', '端口', '配置', '容器', '数据库', '证书']

/** 是否已含显式分点（用户自己就写成了步骤列表 → 不需要我们再拆）。 */
function hasExplicitSteps(text: string): boolean {
  const lines = text.split('\n')
  let numbered = 0
  for (const line of lines) {
    if (/^\s*\d+[.、)]\s+/.test(line)) numbered += 1
  }
  return numbered >= 2
}

function countMatches(text: string, tokens: string[]): number {
  const low = text.toLowerCase()
  let n = 0
  for (const t of tokens) {
    if (low.includes(t.toLowerCase())) n += 1
  }
  return n
}

/**
 * 是否浮现「结构化」入口。运行中不浮现；纯前端零成本判定（点击前不发任何 LLM 请求）。
 * 命中条件：长度达标 且（≥2 个动作动词 ∨ 含连接词 ∨（含运维线索但用户没自己分点））。
 */
export function shouldOfferStructuring(draft: string, running = false): boolean {
  if (running) return false
  const trimmed = draft.trim()
  if (trimmed.length < STRUCT_MIN_CHARS) return false
  if (hasExplicitSteps(trimmed)) return false
  return (
    countMatches(trimmed, ACTION_VERBS) >= 2 ||
    countMatches(trimmed, CONNECTORS) >= 1 ||
    (countMatches(trimmed, OPS_HINTS) >= 1)
  )
}

/** 结构稿是否含"需确认的新增写步骤"（后端已收敛，这里只读聚合位；防御式兼看步骤标记）。 */
export function hasAddedWriteSteps(resp: Pick<StructureResponse, 'has_added_write_steps' | 'steps'>): boolean {
  if (resp.has_added_write_steps) return true
  return resp.steps.some(s => s.requires_confirm)
}

/**
 * 采用结构稿时的审批档裁决：含新增写步骤且当前是 auto → 强制回落 normal。
 * 理由：这些写动作是增强"扩"出来的、用户没在原话里点名，不该被"模型代判"顺带放行。
 * 返回实际应落到的档位（不改则原样返回）。
 */
export function resolveApprovalOnAccept(
  current: ApprovalKey,
  resp: Pick<StructureResponse, 'has_added_write_steps' | 'steps'>,
): ApprovalKey {
  return hasAddedWriteSteps(resp) && current === 'auto' ? 'normal' : current
}

/** 采用后需要提示"审批已回落"吗（仅在真的发生了回落时为真）。 */
export function willDowngradeApproval(
  current: ApprovalKey,
  resp: Pick<StructureResponse, 'has_added_write_steps' | 'steps'>,
): boolean {
  return resolveApprovalOnAccept(current, resp) !== current
}
