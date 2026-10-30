/**
 * 一键懒人模式的链路守卫（读源码断言，不启动 Electron）。
 *
 * 「点一下就开始加速」这条体验链跨了五个文件：core 纯函数 → service.autoTune →
 * main IPC → preload → env.d.ts → store.masterEnable。任何一环被删/被改名，界面都
 * 只会表现为「还是用上次那个镜像」或「没有可用镜像」—— 两种都不是崩溃，跑单测
 * 也看不出来，只能靠这里把形态钉死。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), 'utf-8').replace(/\r\n/g, '\n')

const core = read('../shared/github-accel-core.ts')
const service = read('./github-accel.ts')
const main = read('./main.ts')
const preload = read('./preload.ts')
const envDts = read('../src/env.d.ts')
const store = read('../src/stores/github-accel.ts')
const panel = read('../src/components/ssh/git/GitAccelPanel.vue')

describe('一键懒人模式链路', () => {
  it('core 提供淘汰阈值 / 自动采纳上限与三个判定函数', () => {
    expect(core).toContain('export const PROBE_RETIRE_STREAK = 3')
    expect(core).toContain('export const AUTO_ADOPT_LIMIT = 2')
    expect(core).toContain('export const AUTO_OK_TARGET = 3')
    expect(core).toContain('export function nextFailStreak')
    expect(core).toContain('export function shouldRetire')
    expect(core).toContain('export function pickBestProbe')
    expect(core).toContain('export function pickAutoAdopt')
  })

  it('service.autoTune 串行写盘：淘汰内置只停用、每次启动刷新候选池', () => {
    expect(service).toContain('async autoTune(): Promise<AutoTuneResult>')
    expect(service).toContain('return this.enqueue(async () => {')
    expect(service).toContain("retired.push({ id: p.id, name: p.name, action: 'disabled' })")
    expect(service).toContain("retired.push({ id: p.id, name: p.name, action: 'removed' })")
    // 每次启动都拉一次候选池（不是「全部失效才去发现」），且用短超时防被墙拖死
    expect(service).toContain('const pool = await this.fetchMirrorCandidates({ agent, timeoutMs: STARTUP_AGGREGATOR_TIMEOUT_MS })')
    expect(service).not.toContain('if (!rows.some(r => r.ok)) {')
    // 可用不足才实测候选：够用时只多一个 GET
    expect(service).toContain('const need = AUTO_OK_TARGET - rows.filter(r => r.ok).length')
    // 连续失败计数必须落盘（跨会话累计），否则永远凑不满阈值
    expect(service).toContain('probeStats: keptStats')
  })

  it('IPC 三层齐全：main 注册 / preload 暴露 / env.d.ts 声明', () => {
    expect(main).toContain("handleTrusted('ghaccel:autoTune'")
    expect(main).toContain('githubAccel!.autoTune()')
    expect(preload).toContain("ghaccelAutoTune: () => ipcRenderer.invoke('ghaccel:autoTune')")
    expect(envDts).toContain('ghaccelAutoTune: () => Promise<{')
  })

  it('总控先调优再写 git insteadOf：不再「沿用上次选择的镜像」', () => {
    // 只看 masterEnable 函数体：switchActiveMirror 也调 ghaccelGitEnable，全局搜会误判
    const master = store.slice(store.indexOf('async function masterEnable('), store.indexOf('async function masterDisable('))
    const tune = master.indexOf('ghaccelAutoTune()')
    const enable = master.indexOf('ghaccelGitEnable(')
    expect(tune).toBeGreaterThan(0)
    expect(enable).toBeGreaterThan(0)
    expect(tune).toBeLessThan(enable)
    // 调优结果必须优先于「上次选择」，否则调优白做；实测无可用时也不能硬选死镜像
    expect(store).toContain('const prefix = tune ? tune.best?.prefix : (remembered')
  })

  it('主界面只留一键启动：手动添加/发现/网页镜像站入口全部下线', () => {
    // 手动维护清单的入口已删除，不能再出现在面板源码里
    expect(panel).not.toContain('<details class="advanced">')
    expect(panel).not.toContain('placeholder="添加代理前缀')
    expect(panel).not.toContain('发现公共镜像')
    expect(panel).not.toContain('添加网页镜像站')
    expect(panel).not.toContain('webMirrors')
    // 镜像源面板保留的是「停用/恢复 + 全部测速」
    expect(panel).toContain('全部测速')
  })

  it('总控卡不堆说明文字：只留状态行与结果提示', () => {
    // 要点清单/长段说明都属于冗余，总控卡只留一行状态 + 一行结果
    expect(panel).not.toContain('class="master-facts"')
    expect(panel).not.toContain('一键开关三项措施')
    expect(panel).not.toContain('自动选优')
    // 成功提示：主句一行 + 结果标签（已选 X / 淘汰 N 个）
    expect(panel).toContain("masterNote.value = '加速已全部开启'")
    expect(panel).toContain('masterTags.value = notes.flatMap(toTags)')
  })
})
