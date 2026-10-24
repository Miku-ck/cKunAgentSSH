/**
 * DockerView 页面级守卫
 *
 * 没有组件测试环境（未装 @vue/test-utils/jsdom），这里钉的是代码形状：
 * 跨宿主写入、弹窗回包、轮询交叠这类竞态在人工点测里极难稳定复现，
 * 但它们都归结为「回包前有没有重新校验发起时的上下文」。
 *
 * 页面拆成「页面壳 + 执行底座 + 四个页签 + 三个弹窗」之后，每条不变量都归到
 * 现在真正持有它的那个文件上：竞态守卫在 ./docker/context.ts，模板类语义在
 * 各自的页签/弹窗，页面壳只留编排与页面级状态。
 */
import { describe, expect, it } from 'vitest'
import dockerViewRaw from './DockerView.vue?raw'
import contextRaw from './docker/context.ts?raw'
import containersTabRaw from './docker/DockerContainersTab.vue?raw'
import imagesTabRaw from './docker/DockerImagesTab.vue?raw'
import volumesTabRaw from './docker/DockerVolumesTab.vue?raw'
import networksTabRaw from './docker/DockerNetworksTab.vue?raw'
import pullDialogRaw from './docker/DockerPullDialog.vue?raw'
import volumeDetailRaw from './docker/DockerVolumeDetailDialog.vue?raw'
import networkDetailRaw from './docker/DockerNetworkDetailDialog.vue?raw'
import pickerRaw from './docker/DockerConnectionPicker.vue?raw'
import pageCssRaw from '../../styles/docker-page.css?raw'
import mainRaw from '../../main.ts?raw'
import sshViewRaw from '../../views/ssh/SshView.vue?raw'
import titleBarRaw from '../../layouts/TitleBar.vue?raw'
import iconDockerRaw from '../icons/IconDocker.vue?raw'

// 工作区是 CRLF 检出：统一成 LF 再切片，否则 '\n}\n' 找不到函数结尾
const lf = (s: string): string => s.replace(/\r\n/g, '\n')
const src = lf(dockerViewRaw)
const ctxSrc = lf(contextRaw)
const containersTabSrc = lf(containersTabRaw)
const imagesTabSrc = lf(imagesTabRaw)
const volumesTabSrc = lf(volumesTabRaw)
const networksTabSrc = lf(networksTabRaw)
const pullDialogSrc = lf(pullDialogRaw)
const volumeDetailSrc = lf(volumeDetailRaw)
const networkDetailSrc = lf(networkDetailRaw)
const pickerSrc = lf(pickerRaw)
const pageCssSrc = lf(pageCssRaw)
const mainSrc = lf(mainRaw)
const sshViewSrc = lf(sshViewRaw)
const titleBarSrc = lf(titleBarRaw)
const iconDockerSrc = lf(iconDockerRaw)

/** 以「行首就是 }」的函数结尾切一段；适用于页面壳里的顶层 async function */
function sliceFunction(text: string, header: string): string {
  const start = text.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = text.indexOf('\n}\n', start)
  expect(end).toBeGreaterThan(start)
  return text.slice(start, end)
}

/** 以任意标记收尾切一段：给嵌在工厂/组件里的函数用（它们的收尾缩进不为 0） */
function sliceUntil(text: string, header: string, until: string): string {
  const start = text.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = text.indexOf(until, start + header.length)
  expect(end, `找不到 ${header} 之后的 ${until}`).toBeGreaterThan(start)
  return text.slice(start, end)
}

describe('四类资源取数同构', () => {
  it('工厂内 pin 住发起时的宿主与序号，只在仍新鲜时写回', () => {
    const body = sliceUntil(ctxSrc, 'async function reload(): Promise<void> {', 'return { rows, error, reload }')
    expect(body).toContain('const id = targetId.value')
    expect(body).toContain('pinList(spec.kind, id)')
    expect(body).toContain('if (!listFresh(spec.kind, pin)) return')
    // 失败也要过同一道校验：慢回包不能把已切走宿主的列表清空
    expect(body.match(/listFresh\(spec\.kind, pin\)/g)?.length).toBe(2)
  })

  it('回包写回时顺带记账本次实际取数的资源，并交给 after 钩子', () => {
    const body = sliceUntil(ctxSrc, 'async function reload(): Promise<void> {', 'return { rows, error, reload }')
    expect(body).toContain('markSynced(spec.kind, spec.label)')
    expect(body).toContain('spec.after?.(id, pin)')
  })

  it('页面对四类资源各下一份规格，命令与标签一一对应', () => {
    const specs: Array<[string, RegExp]> = [
      ['containers', /kind: 'containers',[\s\S]{0,300}?label: 'docker ps'/],
      ['images', /kind: 'images', cmd: IMAGES_CMD, parse: parseImages, label: 'docker images'/],
      ['volumes', /kind: 'volumes',[\s\S]{0,300}?label: 'docker volume ls'/],
      ['networks', /kind: 'networks', cmd: NETWORKS_CMD, parse: parseNetworks, label: 'docker network ls'/],
    ]
    for (const [kind, re] of specs) {
      expect(src, kind).toMatch(re)
    }
  })

  it('listFresh 同时校验宿主与同类序号', () => {
    const body = sliceUntil(ctxSrc, 'function listFresh(kind: TabKey, p: ListPin): boolean {', '\n  }')
    expect(body).toContain('p.epoch === targetEpoch')
    expect(body).toContain('p.id === targetId.value')
    expect(body).toContain('p.seq === listSeq[kind]')
  })

  it('loadAll 按目标代次/列表序号丢弃旧轮写回', () => {
    const body = sliceFunction(src, 'async function loadAll(): Promise<void> {')
    expect(body).toContain('pinList(')
    expect(body).toContain("listFresh('containers'")
    expect(body).toContain("listFresh('volumes'")
    // 代次校验挡住「切宿主后又跑了一遍 loadAll」的交叠
    expect(body).toContain('gen !== loadGen')
  })

  it('切宿主递增 epoch，作废全部在途回包', () => {
    const body = sliceUntil(src, 'watch(targetId, () => {', '{ immediate: true })')
    // epoch 必须先于重取：否则本轮 loadAll 的 pin 会拿到新代次，旧回包照样写回
    expect(body.indexOf('bumpEpoch()')).toBeLessThan(body.indexOf('loadAll()'))
    expect(body).toContain('void loadAll()')
  })
})

describe('卷用量富集', () => {
  it('列表之外单独拉 docker system df -v 并合并回卷行', () => {
    expect(src).toContain('VOLUME_USAGE_CMD')
    expect(src).toContain('parseVolumeUsage')
    expect(src).toContain('mergeVolumeUsage')
    const body = sliceFunction(src, 'async function loadAll(): Promise<void> {')
    expect(body).toContain('enrichVolumeUsage')
    // 首屏不等它：用量可能比列表慢一个量级
    expect(src).toContain('after: (id, pin) => { void enrichVolumeUsage(id, pin) }')
  })
  it('详情弹窗以列表富集为回落，不再只认 inspect.Usage', () => {
    expect(volumeDetailSrc).toContain('volumeUsageSummary(')
    expect(volumeDetailSrc).not.toContain('volInspect.usage')
  })
})

describe('跨宿主、弹窗与轮询的陈旧写入', () => {
  it('inspect 回包先校验弹窗仍开着且宿主未换', () => {
    const cases: Array<[string, string]> = [
      [volumeDetailSrc, 'async function openVolumeDetail(v: VolumeRow): Promise<void> {'],
      [networkDetailSrc, 'async function openNetworkDetail(n: NetworkRow): Promise<void> {'],
    ]
    for (const [text, header] of cases) {
      const body = sliceFunction(text, header)
      expect(body, header).toContain('const id = targetId.value')
      expect(body, header).toContain('dockerExec(')
      // 卸载守卫：关掉弹窗后慢回包不该再写这份状态
      expect(body, header).toContain('!alive || targetId.value !== id')
    }
    expect(volumeDetailSrc).toContain('onUnmounted(() => { alive = false })')
    expect(networkDetailSrc).toContain('onUnmounted(() => { alive = false })')
  })

  it('容器详情的三条取数腿已拆到子组件，页面不再自持日志轮询', () => {
    expect(src).toContain('<DockerContainerDetail')
    expect(src).not.toContain('async function loadLogs(')
    expect(src).not.toContain('followTimer')
  })

  it('拉取成功只在发起宿主仍是当前目标时重拉', () => {
    const body = sliceFunction(src, 'function onPulled(id: string): void {')
    expect(body).toContain('targetId.value === id')
    expect(body).toContain('imageList.reload()')
    expect(src).toContain('@pulled="onPulled"')
  })

  it('页签与弹窗只画不跑命令：执行面单点在底座', () => {
    for (const [name, text] of Object.entries({
      DockerContainersTab: containersTabSrc,
      DockerImagesTab: imagesTabSrc,
      DockerVolumesTab: volumesTabSrc,
      DockerNetworksTab: networksTabSrc,
      DockerPullDialog: pullDialogSrc,
      DockerVolumeDetailDialog: volumeDetailSrc,
      DockerNetworkDetailDialog: networkDetailSrc,
      DockerConnectionPicker: pickerSrc,
    })) {
      expect(text, name).not.toContain('sshExec(')
      expect(text, name).not.toContain('useSSHStore')
    }
  })
})

describe('Docker 空态与入口共用鲸鱼图形', () => {
  it('无远程会话的空态画鲸鱼，而不是只有一行字', () => {
    const start = src.indexOf('v-if="!remoteSessions.length"')
    expect(start, '找不到无远程会话的空态分支').toBeGreaterThanOrEqual(0)
    const block = src.slice(start, src.indexOf('<!-- 有目标主机', start))
    expect(block).toContain('<IconDocker')
    expect(block).toContain('尚未连接远程主机')
  })

  it('顶栏与空态读同一枚 IconDocker，不各画一份', () => {
    // 合并方案 A 定稿：Docker 顶栏按钮与页头/空态同源共用 IconDocker
    //（🐋 大鲸鱼：胖圆身、双叉尾、喷气孔水花、挖空眼）。
    expect(titleBarSrc).toContain('<IconDocker')
    expect(titleBarSrc).toContain('IconDocker.vue')
    expect(src).toContain("import IconDocker from '@/components/icons/IconDocker.vue'")
    expect(iconDockerSrc).toContain('M2.4 13.4C2.4 9.9 5.5 7.4 9.4 7.4')
    // 挖空眼依赖 evenodd 打孔，丢了它眼睛会变成实心点
    expect(iconDockerSrc).toContain('fill-rule="evenodd"')
  })
})

describe('健康状态与计数', () => {
  it('unhealthy 归入异常：行警示与状态栏三计数互不重叠', () => {
    // 判据只有一处定义，父级排序/计数与页签配色必须共用它
    expect(containersTabSrc).toContain('abnormal: isAbnormal(c)')
    expect(containersTabSrc).toContain("isAbnormal } from '@/stores/docker-core'")
    expect(src).toMatch(/const runningCount = computed\(\(\) =>[\s\S]*?c\.state === 'running' && !isAbnormal\(c\)/)
    expect(src).toMatch(/const abnormalCount = computed\(\(\) => containers\.value\.filter\(isAbnormal\)\.length\)/)
  })
  it('列表状态列对 unhealthy 用错误色，不沿用运行绿', () => {
    expect(containersTabSrc).toContain("unhealthy: c.health === 'unhealthy'")
    expect(containersTabSrc).toMatch(/\.st-dot\.unhealthy \{ background: var\(--status-error-text\)/)
    expect(containersTabSrc).toMatch(/\.c-state\.unhealthy \{ color: var\(--status-error-text\)/)
  })
})

describe('镜像拉取与悬空清理', () => {
  it('容器详情使用拆出的子组件并回传关闭', () => {
    expect(src).toContain(':session-id="targetId"')
    expect(src).toContain('@close="selected = null"')
    expect(src).toContain("from './docker/DockerContainerDetail.vue'")
    expect(src).not.toContain('class="logs-section"')
  })
  it('拉取走 600s 预算 + 审计，成功后由父级重拉镜像列表', () => {
    expect(pullDialogSrc).toContain('PULL_TIMEOUT_SEC = 600')
    const body = sliceFunction(pullDialogSrc, 'async function doPull(): Promise<void> {')
    expect(body).toContain('pullCmd(')
    expect(body).toContain('PULL_TIMEOUT_SEC')
    expect(body).toContain('AUDIT_ACTION_DOCKER')
    // 弹窗不知道列表归谁管，只把发起宿主一并抛出
    expect(body).toContain("emit('pulled', id)")
    expect(body).not.toContain('reloadImages(')
  })
  it('引用先校验，不给伪旗标与 shell 注入留口子', () => {
    expect(pullDialogSrc).toContain('IMAGE_REF_RE')
    expect(pullDialogSrc).toContain("startsWith('-')")
    expect(pullDialogSrc).toContain('pullRefInvalid')
  })
  it('拉取进行中不可关闭弹窗', () => {
    const body = sliceFunction(pullDialogSrc, 'function closePull(): void {')
    expect(body).toContain('pulling.value')
  })
  it('悬空清理只清 dangling，并复用通用 prune 计数', () => {
    // 按函数体断言：整文件的 toContain 分不清「悬空」与「未使用」两条腿各自用了
    // 哪条命令，把 imagePruneCmd 挪到未使用那条里它照样绿。
    const body = sliceFunction(src, 'async function doPruneImages(): Promise<void> {')
    expect(body).toContain('imagePruneCmd()')
    expect(body).toContain("'prune:images'")
    expect(body).toContain('countPrunedLines')
  })
})

describe('未使用镜像清理', () => {
  it('保留悬空入口，另给独立的未使用入口', () => {
    const body = sliceFunction(src, 'async function doPruneAllImages(): Promise<void> {')
    expect(body).toContain('imagePruneAllCmd()')
    expect(body).toContain("'prune:images-all'")
    expect(src).toContain('清理未使用')
  })
  it('确认框写明带标签镜像也在范围内，并给出可预判的计数口径', () => {
    const body = sliceFunction(src, 'async function doPruneAllImages(): Promise<void> {')
    expect(body).toContain('带标签的镜像也在范围内')
    expect(body).toContain('不可恢复')
    expect(body).toContain('unusedImageCount')
    expect(body).toContain('danglingCount')
  })
  it('docker ps 截断时引用状态降级为「—」，不把在用说成未使用', () => {
    expect(src).toContain('psTruncated')
    expect(src).toMatch(/unusedCountKnown = computed\(\(\) => !psTruncated\.value\)/)
    const body = sliceFunction(imagesTabSrc, 'function imageUseHint(im: ImageRow)')
    expect(body).toContain("'—'")
    expect(body).toContain('props.usageKnown')
  })
  it('镜像表有引用列，且按容器引用数展示', () => {
    expect(imagesTabSrc).toContain('<th>引用</th>')
    expect(imagesTabSrc).toContain('imageUseHint(im).text')
    expect(src).toContain('imageUsageCount')
  })
  it('清理成功后重拉镜像列表', () => {
    const body = sliceFunction(src, 'async function doPruneAllImages(): Promise<void> {')
    expect(body).toContain('imageList.reload()')
    expect(body).toContain('countPrunedLines')
  })
})

describe('镜像导出 / 加载（纯远端）', () => {
  it('弹窗拆成子组件，不往页面里再堆一套表单', () => {
    expect(src).toContain("from './docker/DockerImageTransferModal.vue'")
    expect(src).toContain('<DockerImageTransferModal')
    expect(src).toContain(':initial-dir="lastTransferDir"')
  })
  it('命令走 docker-core 构造，不在这里拼 shell 串', () => {
    expect(src).toContain('dockerSaveCmd(p.ref, p.path, p.gz)')
    expect(src).toContain('dockerLoadCmd(p.path)')
  })
  it('压缩格式由弹窗回传，不在页面里猜后缀', () => {
    expect(src).toContain("mode: 'export'; ref: string; path: string; gz: boolean")
    const body = sliceFunction(src, 'async function onTransferSubmit(')
    expect(body).toContain('p.gz')
  })
  it('gz 用同目录临时文件中转，退出码由 save && gzip 的真实结果决定，不靠大小猜', () => {
    const body = sliceFunction(src, 'async function onTransferSubmit(')
    expect(body).not.toContain('GZ_MIN_BYTES')
    expect(body).toContain('saveTempPath(p.path)')
  })
  it('用 classifyExit 三态判定，不套 dockerErrorFromResult（后者把 unknown 当失败抛）', () => {
    const body = sliceFunction(src, 'async function onTransferSubmit(')
    expect(body).toContain('classifyExit(res.exitCode)')
    expect(body).not.toContain('dockerErrorFromResult')
    expect(body).toContain("'unknown'")
  })
  it('save/load 用满 600s 预算并写审计', () => {
    expect(src).toContain('TRANSFER_TIMEOUT_SEC = 600')
    const body = sliceFunction(src, 'async function transferExec(')
    expect(body).toContain('TRANSFER_TIMEOUT_SEC')
    expect(body).toContain('AUDIT_ACTION_DOCKER')
    expect(body).toContain('writeDockerAudit(')
  })
  it('成败只看退出码；大小读不到只影响文案，不把成功报成失败', () => {
    const body = sliceFunction(src, 'async function onTransferSubmit(')
    expect(body).toContain("if (outcome === 'failed') throw transferFail")
    expect(body).toContain('statSize(')
    expect(body).not.toContain('!size')
  })
  it('超时/断链没有回包，也要带出可能残留的路径（含 gz 临时文件）', () => {
    const body = sliceFunction(src, 'async function onTransferSubmit(')
    expect(body).toContain('可能残留半截文件')
    expect(body).toContain('base.includes(p.path)')
  })
  it('失败文案带出可能残留的路径，不让人以为目录是干净的', () => {
    const body = sliceFunction(src, 'function transferFail(')
    expect(body).toContain('可能残留半截文件')
    expect(body).toContain('path')
  })
  it('执行中不可关闭：关面板不等于停掉远端进程', () => {
    const body = sliceFunction(src, 'function closeTransfer(): void {')
    expect(body).toContain('transferRunning.value')
  })
  it('成功后重拉镜像列表（新载入的镜像要立刻能看到）', () => {
    const body = sliceFunction(src, 'async function onTransferSubmit(')
    expect(body).toContain('imageList.reload()')
  })
  it('行内入口用箱子图标，不跟拉取镜像的向下箭头撞脸', () => {
    expect(imagesTabSrc).toContain('title="导出到远端目录（docker save）"')
    expect(src).toContain('@export="openExport"')
  })
})

describe('动作锁与键盘可达', () => {
  it('任一动作进行中，所有资源动作按钮都禁用（不是只锁发起行）', () => {
    expect(src).not.toMatch(/:disabled="busy === /)
    for (const text of [containersTabSrc, imagesTabSrc, volumesTabSrc, networksTabSrc]) {
      expect(text).toContain(':disabled="!!busy')
    }
  })
  it('行内 Enter/Space 只在行本身聚焦时开详情，不劫持操作按钮', () => {
    expect(containersTabSrc).toContain('@keydown.enter.self.prevent')
    expect(containersTabSrc).toContain('@keydown.space.self.prevent')
  })
})

describe('页面隐藏与截断提示', () => {
  it('Docker 页接收 active，隐藏时收起全部弹窗', () => {
    expect(src).toContain('defineProps<{ active?: boolean }>()')
    expect(src).toContain('props.active')
    const body = src.slice(src.indexOf('watch(() => props.active'), src.indexOf('function refresh()'))
    expect(body).toContain('selected.value = null')
    expect(body).toContain('selectedVolume.value = null')
    expect(body).toContain('selectedNetwork.value = null')
    expect(body).toContain('showPull.value = false')
    expect(body).toContain('showConnPicker.value = false')
    expect(sshViewSrc).toContain(`:active="activeNavPage === 'docker'"`)
  })
  it('SSH 单次输出截断会在状态栏明示', () => {
    expect(src).toContain('isOutputTruncated')
    expect(src).toContain('输出已截断')
  })
})

describe('底部状态栏与网络栏', () => {
  it('同步时间记在底座里，页面只读 lastSync', () => {
    expect(ctxSrc).toContain('markSynced(')
    expect(src).toContain('lastSync')
    expect(src).toContain('已同步')
    expect(src).not.toContain("'docker ps · '")
  })
  it('预置网络不提供必然失败的删除入口', () => {
    expect(networksTabSrc).toContain('isBuiltinNetwork(n.name)')
    expect(networkDetailSrc).toContain('isBuiltinNetwork(network.name)')
    expect(src).toContain('isBuiltinNetwork(n.name)')
  })
  it('无容器连接不再宣称可安全删除', () => {
    expect(networkDetailSrc).not.toContain('可安全删除')
    expect(networkDetailSrc).toContain('当前无容器连接')
  })
})

describe('样式归属', () => {
  it('docker-page.css 已登记进 main.ts', () => {
    expect(mainSrc).toContain("import './styles/docker-page.css'")
  })
  it('页签共享原子全部收在 .docker-view 之下，不裸写通用类名', () => {
    // 裸写 .icon-btn / .mono / .list 会串到全仓其它视图（各视图另有自己的同名 scoped 版本）
    expect(pageCssSrc).toContain('.docker-view .icon-btn')
    expect(pageCssSrc).toContain('.docker-view .mono')
    expect(pageCssSrc).toContain('.docker-view .list')
    expect(pageCssSrc).not.toMatch(/^\.(icon-btn|mono|list|list-empty|spin|c-acts|use-hint|label-hint|ptable)\b/m)
  })
  it('每个页签的根 class 都在 .docker-view 里，样式表才落得到', () => {
    expect(src).toContain('<div class="docker-view">')
  })
})
