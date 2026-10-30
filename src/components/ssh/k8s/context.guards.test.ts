/**
 * K8s 底座的竞态与下发守卫
 *
 * 与 DockerView.guards.test.ts 同一套路子：没有组件测试环境，钉的是源码形状。
 * 这里守的三条都是「人工点测极难稳定复现、但一旦破掉就会给出错误读数」的那类：
 *   1. 换宿主必须作废在途回包（否则 A 集群的 Pod 会盖进已切到 B 的页面）；
 *   2. 回包只在 pin 仍新鲜时写回；
 *   3. 变更动作必须走审计入口，且只能落在行上那个具体 ns。
 */
import { describe, expect, it } from 'vitest'
import ctxRaw from './context.ts?raw'

const lf = (s: string): string => s.replace(/\r\n/g, '\n')
const src = lf(ctxRaw)

function sliceUntil(text: string, header: string, until: string): string {
  const start = text.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = text.indexOf(until, start + header.length)
  expect(end).toBeGreaterThan(start)
  return text.slice(start, end)
}

describe('换宿主的失效语义', () => {
  it('底座里没有 namespace 状态：列表一律 -A，取数范围只由宿主决定', () => {
    expect(src).not.toMatch(/const namespace = ref/)
    expect(src).not.toMatch(/\bnamespace\.value\b/)
    expect(src).not.toContain('nsListFailed')
  })
  it('新鲜度判据只有 id 与 seq 两格：A→B→A 来回切也能作废先前那批在途回包', () => {
    const body = sliceUntil(src, 'function listFresh(', '\n  }')
    expect(body).toContain('p.id === targetId.value')
    expect(body).toContain('p.seq === listSeq[kind]')
  })
  it('ListPin 不带 ns：把命名空间塞进 pin 再逐字段比对，是「界面有一根轴」时的做法', () => {
    const pin = sliceUntil(src, 'export interface ListPin {', '}')
    expect(pin).toContain('id: string')
    expect(pin).toContain('seq: number')
    expect(pin).not.toContain('namespace')
    expect(pin).not.toContain('epoch')
  })
})

describe('取数只在新鲜时写回', () => {
  it('reload 的成功与失败两条路径都先验新鲜度', () => {
    const body = sliceUntil(src, 'async function reload(): Promise<void> {', 'return { rows, error, reload }')
    expect(body).toContain('const pin = pinList(spec.kind, id)')
    expect(body.match(/if \(!listFresh\(spec\.kind, pin\)\) return/g)).toHaveLength(2)
  })
  it('失败如实上浮，不清空成「这里没有资源」的假象', () => {
    const body = sliceUntil(src, 'async function reload(): Promise<void> {', 'return { rows, error, reload }')
    // 存的是整个 K8sError：页面要靠 code 分辨「整机供不了 kubectl」与「只有这一类读不到」
    expect(body).toContain('error.value = toK8sError(e)')
  })
  it('命令在发起时才取：事件页的「含 Normal」改的是取数范围，构造时定死会读成上一轮', () => {
    const body = sliceUntil(src, 'async function reload(): Promise<void> {', 'return { rows, error, reload }')
    expect(body).toContain('spec.cmd()')
  })
})

describe('变更动作的下发口径', () => {
  it('动作走 actionExec：60s 预算 + 审计键，读路径不写审计', () => {
    const body = sliceUntil(src, 'function actionExec(cmd: string): Promise<string> {', '\n  }')
    expect(body).toContain('ACTION_TIMEOUT_SEC')
    expect(body).toContain('AUDIT_ACTION_K8S')
    expect(src).toContain("if (!id || !api) throw")
  })
  it('EXEC_TIMEOUT 记 executed 而不是 failed（命令可能已落地，记 failed 会诱导再执行一次）', () => {
    const body = sliceUntil(src, 'async function k8sExec(', 'function actionExec')
    expect(body).toContain('auditOutcomeForDispatchError')
    expect(body).toContain('auditResultForExit')
  })
  it('kubectl 非零退出仍记 executed，错误码附在 result 里供检索', () => {
    const body = sliceUntil(src, 'async function k8sExec(', 'function actionExec')
    expect(body).toContain("k8s_error=")
    expect(body).toContain("if (kErr) throw kErr")
  })
})

describe('与 Docker 页共用同一份远程目标底座', () => {
  it('目标与动作锁来自 useRemoteTarget，本文件不再声明一份', () => {
    expect(src).toContain("from '../remote-target'")
    expect(src).toContain('useRemoteTarget()')
    expect(src).toContain('...remoteTarget,')
    expect(src).not.toContain('sshStore.connectRemote(')
    // 刻意不抽的那一半：命令执行与审计各页一套，差异不该靠参数塞进公共层
    expect(src).toContain('function k8sExec(')
  })
  it('截断判据与主进程同源，不各自持一份字面量', () => {
    expect(src).toContain("from '../../../../shared/exec-outcome-core'")
    expect(src).toContain('isOutputTruncated(')
  })
  it('底座只从 k8s-core 取命令构造器，自己不拼 kubectl 串', () => {
    expect(src).toContain("from '@/stores/k8s-core'")
    // 命令串一律来自 k8s-core；本文件里的 kubectl 字样只许出现在注释与状态栏标签里
    expect(src).not.toMatch(/`\s*kubectl/)
  })
})
