/**
 * S5-11：本地复制的取消登记表。
 *
 * 这条表是 `sftp:cancel`（一条通道服务两类任务）里属于 fs:copy 的那半，所以它的
 * 关键性质不是「能不能置位」，而是**残项不会误伤下一次复用同一 id 的复制** ——
 * transferId 由渲染端计数器生成，跨会话会重复。
 */
import { beforeEach, describe, expect, it } from 'vitest'

import {
  activeCopyCount,
  beginCopy,
  endCopy,
  requestCopyCancel,
} from './copy-cancel'

describe('copy-cancel（S5-11 本地复制协作式取消）', () => {
  beforeEach(() => {
    // 表是模块级的（主进程只有一份），逐条用例清干净再跑
    for (const id of ['t1', 't2', 't3']) endCopy(id)
    expect(activeCopyCount()).toBe(0)
  })

  it('取消命中在跑的复制：工作对象被循环读到的那一份置位', () => {
    const state = beginCopy('t1')
    expect(state.cancelled).toBe(false)
    expect(requestCopyCancel('t1')).toBe(true)
    expect(state.cancelled).toBe(true)
  })

  it('未登记的 id 返回 false —— sftp:cancel 还得把它交给 SFTP 传输那条腿', () => {
    expect(requestCopyCancel('nobody')).toBe(false)
  })

  it('收尾后同一 id 重开是一条全新复制，不会被上一次取消误杀', () => {
    const first = beginCopy('t1')
    requestCopyCancel('t1')
    endCopy('t1')
    const second = beginCopy('t1')
    expect(second.cancelled).toBe(false)
    expect(requestCopyCancel('t1')).toBe(true)
    expect(second.cancelled).toBe(true)
    expect(second).not.toBe(first)
  })

  it('收尾必须真的摘掉表项，否则表随复制次数无界增长', () => {
    beginCopy('t1')
    beginCopy('t2')
    expect(activeCopyCount()).toBe(2)
    endCopy('t1')
    endCopy('t2')
    expect(activeCopyCount()).toBe(0)
  })

  it('并发两条复制时取消各归各的 id', () => {
    const a = beginCopy('t1')
    const b = beginCopy('t2')
    requestCopyCancel('t2')
    expect(b.cancelled).toBe(true)
    expect(a.cancelled).toBe(false)
  })
})
