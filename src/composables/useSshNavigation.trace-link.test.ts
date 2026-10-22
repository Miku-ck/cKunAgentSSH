/** 消息气泡「轨迹」直达：run 号 + 递增序号交给运维轨迹页定位 */
import { describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const { useSshNavigation } = await import('@/composables/useSshNavigation')

function mount() {
  setActivePinia(createPinia())
  return useSshNavigation(() => {}, {})
}

describe('openTrace：从消息跳运维轨迹', () => {
  it('切到轨迹页并带上 run 号', () => {
    const nav = mount()
    nav.openTrace('run-1')
    expect(nav.activeNavPage.value).toBe('audit')
    expect(nav.homeTabActive.value).toBe(false)
    expect(nav.auditEntry.value).toEqual({ runId: 'run-1', seq: 1 })
  })

  it('同一个 run 连点两次也递增 seq，驱动表格重新定位', () => {
    const nav = mount()
    nav.openTrace('run-1')
    nav.openTrace('run-1')
    expect(nav.auditEntry.value).toEqual({ runId: 'run-1', seq: 2 })
  })

  it('从独立功能页进入也落到轨迹页', () => {
    const nav = mount()
    nav.toggleDocker()
    expect(nav.activeNavPage.value).toBe('docker')
    nav.openTrace('run-2')
    expect(nav.activeNavPage.value).toBe('audit')
    expect(nav.homeTabActive.value).toBe(false)
  })
})

describe('toggleAudit：从轨迹 tab 进入', () => {
  it('不带 run 号，并递增 seq 驱动列表清掉直达筛选', () => {
    const nav = mount()
    nav.openTrace('run-1')
    nav.goHome()
    nav.toggleAudit()
    expect(nav.activeNavPage.value).toBe('audit')
    expect(nav.auditEntry.value).toEqual({ runId: '', seq: 2 })
  })

  it('已经在该页时点 tab 是离开，不产生新的进入指令', () => {
    const nav = mount()
    nav.toggleAudit()
    const entered = nav.auditEntry.value
    nav.toggleAudit()
    expect(nav.activeNavPage.value).toBe('home')
    expect(nav.auditEntry.value).toBe(entered)
  })
})
