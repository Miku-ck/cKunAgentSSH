/**
 * 端口范围判据与文案（shared/port-core.ts）：主进程写侧/读侧与渲染层表单校验共用一份，
 * 边界与文案漂任何一处都会变成「UI 放行、主进程拒收」，所以钉死。
 */
import { describe, expect, it } from 'vitest'
import { isPort, portRangeError } from '../shared/port-core'

describe('isPort', () => {
  it('接受 1 与 65535 两个边界', () => {
    expect(isPort(1)).toBe(true)
    expect(isPort(65535)).toBe(true)
    expect(isPort(22)).toBe(true)
  })

  it('拒绝 0 / 越界 / 非整数', () => {
    expect(isPort(0)).toBe(false)
    expect(isPort(65536)).toBe(false)
    expect(isPort(-1)).toBe(false)
    expect(isPort(80.5)).toBe(false)
    expect(isPort(Number.NaN)).toBe(false)
  })

  it('拒绝非 number（脏 tunnels.json 里的 undefined / 字符串端口）', () => {
    expect(isPort(undefined)).toBe(false)
    expect(isPort(null)).toBe(false)
    expect(isPort('8080')).toBe(false)
    expect(isPort('')).toBe(false)
  })
})

describe('portRangeError', () => {
  it('不带字段前缀时给出通用句', () => {
    expect(portRangeError()).toBe('端口需为 1–65535 的整数')
  })

  it('带字段前缀时拼成完整句，读起来仍是人话', () => {
    expect(portRangeError('本地监听')).toBe('本地监听端口需为 1–65535 的整数')
    expect(portRangeError('远端绑定')).toBe('远端绑定端口需为 1–65535 的整数')
  })
})
