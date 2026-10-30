/**
 * K8s 页面的接线与并发守卫
 *
 * 沿用 DockerView.guards.test.ts 的路子：没有组件测试环境，钉源码形状。
 * 这里最值钱的是跨文件对账 —— 忙锁键、注册点、CSS 归属这类事实在两个文件里
 * 各写一半，单边看都对，错一边的表现是「选中行不再高亮」「图标永远不亮」
 * 这种没人会去查的哑故障。
 */
import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import viewRaw from './K8sView.vue?raw'
import ctxRaw from './k8s/context.ts?raw'
import nodesRaw from './k8s/K8sNodesTab.vue?raw'
import workloadsRaw from './k8s/K8sWorkloadsTab.vue?raw'
import podsRaw from './k8s/K8sPodsTab.vue?raw'
import servicesRaw from './k8s/K8sServicesTab.vue?raw'
import eventsRaw from './k8s/K8sEventsTab.vue?raw'
import detailRaw from './k8s/K8sResourceDetail.vue?raw'
import scaleRaw from './k8s/K8sScaleDialog.vue?raw'
import pageCssRaw from '../../styles/k8s-page.css?raw'
import coreRaw from '../../stores/k8s-core.ts?raw'
import mainRaw from '../../main.ts?raw'
import sshViewRaw from '../../views/ssh/SshView.vue?raw'
import titleBarRaw from '../../layouts/TitleBar.vue?raw'
import navRaw from '../../composables/useSshNavigation.ts?raw'
import shortcutsRaw from '../../composables/useGlobalShortcuts.ts?raw'

const lf = (s: string): string => s.replace(/\r\n/g, '\n')
const src = lf(viewRaw)
const ctxSrc = lf(ctxRaw)
const detailSrc = lf(detailRaw)
const cssSrc = lf(pageCssRaw)

function sliceUntil(text: string, header: string, until: string): string {
  const start = text.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = text.indexOf(until, start + header.length)
  expect(end).toBeGreaterThan(start)
  return text.slice(start, end)
}

describe('注册点一处不能漏', () => {
  it('NavPage 联合、toggle、快捷键、标题条、App、SshView 挂载、命令面板、CSS 登记全在', () => {
    expect(lf(navRaw)).toContain("'docker' | 'k8s' | 'git'")
    expect(lf(navRaw)).toContain("togglePage('k8s')")
    expect(lf(shortcutsRaw)).toContain("{ page: 'k8s', num: 0 }")
    // 数字键正则不收 0 的话，Ctrl+Shift+0 永远不会命中上面的表
    expect(lf(shortcutsRaw)).toContain('/^Digit([0-9])$/')
    expect(lf(titleBarRaw)).toContain("activePage === 'k8s'")
    expect(lf(titleBarRaw)).toContain("emit('action-k8s')")
    expect(src).toContain('provideK8sHost()')
    expect(lf(sshViewRaw)).toContain("pageMounted('k8s')")
    expect(lf(sshViewRaw)).toContain(`:active="activeNavPage === 'k8s'"`)
    expect(lf(sshViewRaw)).toContain("import('@/components/ssh/K8sView.vue')")
    expect(lf(mainRaw)).toContain("import './styles/k8s-page.css'")
  })
})

describe('忙锁键与行键跨文件对账', () => {
  it('负载表比的键，就是页壳下发时用的键', () => {
    const tab = lf(workloadsRaw)
    expect(tab).toContain('busy === workloadRowKey(w)')
    expect(tab).toContain('selectedKey === workloadRowKey(w)')
    // 两个动作各自 runAction 的第一个实参
    expect(sliceUntil(src, 'async function doRestart', 'async function doScale')).toContain('workloadRowKey(w),')
    expect(sliceUntil(src, 'async function doDeleteJob', '\n</script>')).toContain('workloadRowKey(w),')
  })
  it('Pod 表同理', () => {
    const tab = lf(podsRaw)
    expect(tab).toContain('busy === podRowKey(p)')
    expect(tab).toContain('selectedKey === podRowKey(p)')
    expect(sliceUntil(src, 'async function doDeletePod', 'async function doDeleteJob')).toContain('podRowKey(p),')
  })
  it('行键只在 k8s-core 定义一次，页签与页壳都从那里取', () => {
    for (const [name, text] of [['workloads', lf(workloadsRaw)], ['pods', lf(podsRaw)], ['services', lf(servicesRaw)]] as const) {
      expect(text, `${name} 页签自己拼了行键`).not.toMatch(/`( \$\{[a-z]\.kind\}|.*\$\{[a-z]\.namespace\}\/)/)
    }
    expect(src).not.toContain('${w.kind}/${w.namespace}/${w.name}')
    expect(src).not.toContain('${p.namespace}/${p.name}')
  })
})

describe('四个窄动作的纪律', () => {
  it('全部先确认，且都经 actionExec（审计 + 60s 预算）', () => {
    for (const fn of ['doRestart', 'doScale', 'doDeletePod', 'doDeleteJob']) {
      expect(src, `${fn} 缺确认`).toContain('await confirm({')
    }
    const run = sliceUntil(src, 'async function runAction(', 'async function doRestart')
    expect(run).toContain('host.actionExec(cmd)')
    expect(run).toContain('host.withBusyKey(key')
    // 失败上浮为错误而不是静默：审计已经写了，界面也得说一句
    expect(run).toContain('host.toastErr(e)')
  })
  it('确认框写的是后果，不是一句「确定吗」', () => {
    expect(src).toContain('ACTION_CONSEQUENCE.rolloutRestart')
    expect(src).toContain('ACTION_CONSEQUENCE.deletePod')
    expect(src).toContain('ACTION_CONSEQUENCE.deleteJob')
    expect(lf(scaleRaw)).toContain('ACTION_CONSEQUENCE.scale')
  })
})

describe('节点用量的第二条腿', () => {
  it('写回前验新鲜度：中途换主机时，旧主机的同名节点（k1/k2）不能把用量并进新主机的行', () => {
    const body = sliceUntil(src, 'async function enrichNodeUsage(', '\n}')
    expect(body).toContain("host.listFresh('nodes', pin)")
    expect(body).toContain('mergeNodeUsage(')
    // 取不到用量是常态（没有 metrics-server）：这条腿失败必须静默，不许红一条带
    expect(body).toContain('catch {')
  })
})

describe('详情弹窗的并发与退出路径', () => {
  it('三条取数腿每个写回点都先验新鲜度', () => {
    for (const leg of ['detail', 'events', 'logs']) {
      const hits = detailSrc.match(new RegExp(`if \\(!fresh\\('${leg}', n\\)\\) return`, 'g')) ?? []
      expect(hits.length, `${leg} 腿的写回点没都验新鲜度`).toBeGreaterThanOrEqual(2)
    }
    expect(detailSrc).toContain('if (disposed || host.targetId.value !== props.sessionId) return false')
  })
  it('日志腿是轮询 tail + 定时器收尾，不是一个永不结束的 exec', () => {
    // 命令在 k8s-core 拼，所以「不带 -f」钉在持有它的那个文件上；这里钉生命周期
    expect(detailSrc).toContain('FOLLOW_INTERVAL_MS')
    expect(detailSrc).toContain('setInterval(() => { void loadLogs() }')
    expect(detailSrc).toContain('stopFollow()')
    expect(detailSrc).toContain('disposed = true')
    const builder = sliceUntil(lf(coreRaw), 'export function logsCmd(', '\n}')
    expect(builder).toContain('--tail=')
    expect(builder).not.toMatch(/ -f |--follow/)
  })
  it('接了共享焦点件，Esc 有退出路径（与全仓 role=dialog 同一约定）', () => {
    for (const text of [detailSrc, lf(scaleRaw)]) {
      expect(text).toContain('useModalFocusTrap')
      expect(text).toContain('activate()')
      expect(text).toContain('deactivate()')
      expect(text).toContain("'Escape'")
      expect(text).toContain('tabindex="-1"')
      expect(text).toContain('window.removeEventListener')
    }
  })
})

describe('没有命名空间这根轴', () => {
  it('界面不出现 ns 选择器，也不出现「当前命名空间」这种页面状态', () => {
    expect(src).not.toContain('aria-label="命名空间"')
    expect(src).not.toContain('nsListFailed')
    expect(src).not.toContain('ALL_NS')
    expect(src).not.toMatch(/^\s*const \{[^}]*\bnamespace\b/m)
  })
  it('收窄靠搜索框，一次同时匹配名称与命名空间', () => {
    expect(src).toContain('按名称 / 命名空间过滤')
    const filter = sliceUntil(src, 'function byName<', '\n}')
    expect(filter).toContain('r.name')
    expect(filter).toContain('r.namespace')
    // 事件表没有 name 字段，但它多一列命名空间：过滤要跟着扩
    expect(src).toContain('[e.namespace, e.object, e.reason, e.message]')
  })
  it('切主机重取整轮，除此之外没有第二根轴要监听', () => {
    expect(src).toContain('watch(targetId, () => { void loadAll() }, { immediate: true })')
    expect(src).not.toContain('watch([targetId, namespace]')
  })
  it('列表命令不带 -n：范围是 -A，第二种取数范围没有状态可以驱动它', () => {
    for (const rel of ['WORKLOADS_CMD', 'PODS_CMD', 'SERVICES_CMD']) expect(src).toContain(rel)
    expect(src).toContain('showNormalEvents.value ? EVENTS_CMD_WITH_NORMAL : EVENTS_CMD')
  })
  it('事件只看 Warning 只在服务端做一次，界面不重复过滤', () => {
    // 兜一层本地过滤会让「谁在收窄」变成两说；--field-selector 是 API server 认的保证
    expect(src).not.toContain("e.type !== 'Normal'")
    expect(src).toContain('没有 Warning 事件')
    expect(src).toContain('watch(showNormalEvents')
  })
})

describe('执行归底座，页签只画', () => {
  it('五个页签与详情都不碰 electronAPI / sshExec', () => {
    for (const [name, text] of [
      ['节点', lf(nodesRaw)], ['负载', lf(workloadsRaw)], ['Pod', lf(podsRaw)],
      ['服务', lf(servicesRaw)], ['事件', lf(eventsRaw)],
    ] as const) {
      expect(text, `${name} 页签自己在跑命令`).not.toMatch(/electronAPI|sshExec|k8sExec/)
    }
    // 详情的三条腿经底座走（审计与截断记账都在那里）
    expect(detailSrc).not.toContain('electronAPI')
    expect(detailSrc).toContain('host.k8sExec(')
    expect(src).not.toContain('electronAPI')
  })
  it('命令串只在 k8s-core 拼，页面与底座不出现 kubectl 命令模板', () => {
    // 只拦「以 kubectl 开头的模板串」；状态栏里 label 那种展示用的 'kubectl get nodes'
    // 字样是给人看的取数说明，不是命令来源，允许存在。
    for (const [name, text] of [['页面壳', src], ['详情', detailSrc], ['底座', ctxSrc]] as const) {
      expect(text, `${name} 自己拼了 kubectl 命令`).not.toMatch(/`\s*kubectl/)
    }
  })
})

describe('样式归属', () => {
  it('共享原子全部收在 .k8s-view / .k8s-dt / .k8s-scale 之下', () => {
    expect(cssSrc).toContain('.k8s-view .icon-btn')
    expect(cssSrc).toContain('.k8s-view .list-empty')
    // 裸写的通用类名会串到别的页面（.list / .badge 这类在 SftpPane、TunnelView 都有各自版本）
    expect(cssSrc).not.toMatch(/^\.(list|list-empty|icon-btn|mono|muted|badge|ck|spin)\s*[,{]/m)
    // 弹窗骨架复用 docker-modal.css 那一份，不在这里抄第二遍
    expect(cssSrc).not.toContain('.dt-overlay {')
    expect(cssSrc).not.toContain('.dt-dialog {')
  })
  it('转圈动画自带独立 keyframes 名，不与 docker 页的 dk-rot 抢', () => {
    expect(cssSrc).toContain('@keyframes k8s-rot')
    expect(cssSrc).not.toContain('dk-rot')
  })
})

describe('新增文件的 .vue 导入必须落到真实文件', () => {
  // 为什么要有这条：env.d.ts 里 `declare module '*.vue'` 把任何 .vue 导入都当 any，
  // 路径写错 vue-tsc 照样过。本次就写错过一次（./icons/IconK8s.vue 少算一层目录），
  // 表现是标题条图标点亮、页面整块不出现 —— 静态检查全绿，只有真跑一遍才暴露。
  const srcRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
  const files = [
    'components/ssh/K8sView.vue',
    'components/ssh/k8s/context.ts',
    'components/ssh/k8s/K8sNodesTab.vue',
    'components/ssh/k8s/K8sWorkloadsTab.vue',
    'components/ssh/k8s/K8sPodsTab.vue',
    'components/ssh/k8s/K8sServicesTab.vue',
    'components/ssh/k8s/K8sEventsTab.vue',
    'components/ssh/k8s/K8sResourceDetail.vue',
    'components/ssh/k8s/K8sScaleDialog.vue',
    'components/ssh/remote-target.ts',
  ]
  for (const rel of files) {
    const abs = resolve(srcRoot, rel)
    const text = lf(readFileSync(abs, 'utf8'))
    const specs = [...text.matchAll(/from\s+['"]([^'"]+\.vue)['"]/g)].map(m => m[1])
    it(`${rel}：${specs.length} 个 .vue 导入都在磁盘上`, () => {
      for (const spec of specs) {
        const target = spec.startsWith('@/')
          ? resolve(srcRoot, spec.slice(2))
          : resolve(dirname(abs), spec)
        expect(existsSync(target), `${rel} 的导入 ${spec} 指向不存在的文件`).toBe(true)
      }
    })
  }
})

describe('截断与错误带的分工', () => {
  it('整机级错误只在页顶说一次，单类失败由列表自己说', () => {
    expect(src).toContain('isHostLevelError(e?.code)')
    expect(src).toContain('v-if="hostError"')
    expect(ctxSrc).toContain('toK8sError(e)')
  })
  it('「没有 k8s」说成状态而不是故障，措辞判据留在 k8s-core', () => {
    // 判据要能在纯函数里测：页面只提供 hostError 与 contextName 两个已落定的读数
    expect(src).toContain('isNotDetectedState(hostError.value, contextName.value)')
    expect(src).toContain('未检测到 k8s：')
    expect(src).toContain(':role="notDetected ? \'status\' : \'alert\'"')
  })
  it('context 先于五个列表取：措辞靠它定夺，并行会让同一屏换两次说法', () => {
    const body = sliceUntil(src, 'async function loadAll(', 'function refresh()')
    expect(body.indexOf('await readK8sContext(host)')).toBeGreaterThan(-1)
    expect(body.indexOf('await readK8sContext(host)')).toBeLessThan(body.indexOf('Promise.allSettled'))
  })
  it('1M 截断必须在状态栏明示，不能让用户把缺行当全量', () => {
    expect(src).toContain('v-if="truncated"')
    expect(src).toContain('输出已截断')
    expect(src).toContain('1M 字符上限')
    expect(sliceUntil(src, 'async function loadAll(', 'function refresh()')).toContain('truncated.value = false')
  })
})
