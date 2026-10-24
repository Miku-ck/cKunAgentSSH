/**
 * 长任务结构化前端判定测试（spec 2026-10-24-task-structuring · 接缝 S3）
 *
 * 钉两件事：
 * 1. 启发式入口"窄"：短指令不浮现、长度边界（119/120）按单点常量精确判定、
 *    用户已自己分点的长文不再重复拆；运行中恒不浮现。
 * 2. 审批回落：含"新增写步骤"的结构稿被采用时，auto 强制回落 normal —— 这是
 *    §0 不变量在前端的落点（增强扩出来的写动作不该被模型代判顺带放行）。
 *
 * 断言必须能失败：把 STRUCT_MIN_CHARS 改小、把回落判定写成恒等、或对 added 写
 * 步骤不回落，本文件都要变红。
 */
import { describe, expect, it } from 'vitest'
import {
  PROVENANCE_LABELS,
  STRUCT_MIN_CHARS,
  hasAddedWriteSteps,
  resolveApprovalOnAccept,
  shouldOfferStructuring,
  willDowngradeApproval,
  type StructureResponse,
} from './task-structure-core'

const LONG_FUZZY =
  '我们现在有一台跑 nginx 的服务器最近总是无端端地变得很慢然后时不时直接 502 挂了，' +
  '我怀疑多半是磁盘写满或者内存被打爆导致的，你能不能先帮我看看现在这台机器上 nginx 服务和系统资源的整体状况，' +
  '确认一下到底是磁盘还是内存的问题，然后如果是磁盘满了就帮我把那些早就该清理掉的旧日志和无用的临时文件清理一下腾出空间，' +
  '最后再把 nginx 重启一下确保它能正常对外提供服务，整个过程千万别把现有的配置文件和业务数据给弄丢了'

function resp(over: Partial<StructureResponse>): Pick<StructureResponse, 'has_added_write_steps' | 'steps'> {
  return { has_added_write_steps: false, steps: [], ...over }
}

describe('shouldOfferStructuring 启发式窄入口', () => {
  it('短指令不浮现', () => {
    expect(shouldOfferStructuring('重启 nginx')).toBe(false)
  })

  it('长度边界：恰好达标才浮现（含运维线索）', () => {
    const pad = 'a'.repeat(STRUCT_MIN_CHARS - 'nginx'.length)
    expect(shouldOfferStructuring(pad + 'nginx')).toBe(true)
    expect(shouldOfferStructuring('a'.repeat(STRUCT_MIN_CHARS - 1 - 'nginx'.length) + 'nginx')).toBe(false)
  })

  it('长模糊需求浮现', () => {
    expect(shouldOfferStructuring(LONG_FUZZY)).toBe(true)
  })

  it('用户已自己分点的长文不再重复拆', () => {
    const already = LONG_FUZZY + '\n1. 先看状态\n2. 再清理\n3. 最后重启'
    expect(shouldOfferStructuring(already)).toBe(false)
  })

  it('运行中恒不浮现', () => {
    expect(shouldOfferStructuring(LONG_FUZZY, true)).toBe(false)
  })
})

describe('审批回落判定', () => {
  it('含新增写步骤 + auto → 回落 normal', () => {
    const r = resp({ has_added_write_steps: true })
    expect(resolveApprovalOnAccept('auto', r)).toBe('normal')
    expect(willDowngradeApproval('auto', r)).toBe(true)
  })

  it('无新增写步骤 + auto → 保持 auto（不误伤）', () => {
    expect(resolveApprovalOnAccept('auto', resp({}))).toBe('auto')
    expect(willDowngradeApproval('auto', resp({}))).toBe(false)
  })

  it('含新增写步骤 + normal → 不动（已是最严档）', () => {
    expect(resolveApprovalOnAccept('normal', resp({ has_added_write_steps: true }))).toBe('normal')
  })

  it('防御式：聚合位漏标但步骤带 requires_confirm 也识别为有新增写步骤', () => {
    const r = resp({
      has_added_write_steps: false,
      steps: [{ id: 's1', text: '重启', op_kind: 'write', provenance: 'added', requires_confirm: true }],
    })
    expect(hasAddedWriteSteps(r)).toBe(true)
    expect(resolveApprovalOnAccept('auto', r)).toBe('normal')
  })
})

describe('provenance 文案穷举', () => {
  it('四态都有标签，无空串', () => {
    expect(Object.keys(PROVENANCE_LABELS).sort()).toEqual(
      ['added', 'completed', 'inferred', 'verbatim'],
    )
    expect(Object.values(PROVENANCE_LABELS).every(v => v.length > 0)).toBe(true)
  })
})
