/**
 * S4-07 通道审计接线守卫
 *
 * 没有组件测试环境（未装 @vue/test-utils/jsdom），这里钉的是代码形状：
 * 两条人工通道（批量下发 / Docker 变更动作）必须在动作之后追加审计，
 * 且审计失败只提示、不阻断也不回滚已发生的远端动作。
 *
 * 纯函数与请求体构造由 src/composables/remoteAudit.test.ts 覆盖。
 */
import bulkRaw from './BulkCommandModal.vue?raw'
import dockerRaw from './DockerView.vue?raw'
import { describe, expect, it } from 'vitest'

const bulkSrc = bulkRaw.replace(/\r\n/g, '\n')
const dockerSrc = dockerRaw.replace(/\r\n/g, '\n')

function sliceFunction(src: string, header: string): string {
  const start = src.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = src.indexOf('\n}\n', start)
  expect(end).toBeGreaterThan(start)
  return src.slice(start, end)
}

describe('批量下发', () => {
  const run = sliceFunction(bulkSrc, 'async function run() {')

  it('每个目标都写 bulk_ssh_exec；超时按 executed 分类', () => {
    expect(bulkSrc).toContain('AUDIT_ACTION_BULK')
    expect(run).toContain('buildRemoteAuditRecord')
    expect(run).toContain("queueAudit('executed'")
    expect(run).toContain('auditOutcomeForDispatchError')
  })

  it('结果先入列再写审计：审计不挡进度与结果展示', () => {
    expect(run.indexOf('results.value.push')).toBeLessThan(
      run.indexOf("queueAudit('executed'"),
    )
  })

  it('审计失败聚合成一次警告，而不是逐台刷 toast（也不回滚命令）', () => {
    expect(run).toContain('writeRemoteAudit')
    expect(run).toContain('Promise.all(auditTasks)')
    expect(run).toContain('toast.warning')
  })

  it('等待审计落在 running 归位之后，不让审计拖住执行态', () => {
    expect(run.indexOf('running.value = false')).toBeLessThan(
      run.indexOf('await Promise.all(auditTasks)'),
    )
  })
})

describe('Docker 变更动作', () => {
  const dockerExec = sliceFunction(dockerSrc, 'async function dockerExec(')
  const actionExec = sliceFunction(dockerSrc, 'function actionExec(')

  it('只有动作入口带审计，读路径（PS/IMAGES/VOLUMES…）不带', () => {
    expect(actionExec).toContain('AUDIT_ACTION_DOCKER')
    expect(dockerExec).toContain('auditAction')
  })

  it('审计目标在调用时解析，跨宿主切换后不串到新目标', () => {
    expect(dockerExec).toContain('dockerAuditTarget(id)')
  })

  it('sshExec 回包就记 executed（docker 非零也执行过）；抛错按超时/失败分类', () => {
    expect(dockerExec.indexOf('api.sshExec')).toBeLessThan(
      dockerExec.indexOf('auditResultForExit'),
    )
    expect(dockerExec).toContain('auditOutcomeForDispatchError')
  })

  it('写审计采用 fire-and-forget，失败只提示，不改动作返回值', () => {
    expect(dockerSrc).toContain('writeRemoteAudit')
    expect(dockerSrc).toContain('toast.warning')
    expect(dockerExec).not.toContain('await writeRemoteAudit')
  })
})
