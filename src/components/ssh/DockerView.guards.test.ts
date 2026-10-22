/**
 * DockerView 页面级守卫
 *
 * 没有组件测试环境（未装 @vue/test-utils/jsdom），这里钉的是代码形状：
 * 跨宿主写入、弹窗回包、轮询交叠这类竞态在人工点测里极难稳定复现，
 * 但它们都归结为「回包前有没有重新校验发起时的上下文」。
 */
import dockerViewRaw from './DockerView.vue?raw'
import sshViewRaw from '../../views/ssh/SshView.vue?raw'
import titleBarRaw from '../../layouts/TitleBar.vue?raw'
import iconDockerRaw from '../icons/IconDocker.vue?raw'
import { describe, expect, it } from 'vitest'

// 工作区是 CRLF 检出：统一成 LF 再切片，否则 '\n}\n' 找不到函数结尾
const src = dockerViewRaw.replace(/\r\n/g, '\n')
const sshViewSrc = sshViewRaw.replace(/\r\n/g, '\n')
const titleBarSrc = titleBarRaw.replace(/\r\n/g, '\n')
const iconDockerSrc = iconDockerRaw.replace(/\r\n/g, '\n')

function sliceFunction(header: string): string {
  const start = src.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = src.indexOf('\n}\n', start)
  expect(end).toBeGreaterThan(start)
  return src.slice(start, end)
}

describe('卷用量富集', () => {
  it('列表之外单独拉 docker system df -v 并合并回卷行', () => {
    expect(src).toContain('VOLUME_USAGE_CMD')
    expect(src).toContain('parseVolumeUsage')
    expect(src).toContain('mergeVolumeUsage')
    expect(sliceFunction('async function loadAll(): Promise<void> {')).toContain('enrichVolumeUsage')
  })
  it('详情弹窗以列表富集为回落，不再只认 inspect.Usage', () => {
    expect(src).toContain('volumeUsageSummary(')
    expect(src).not.toContain('volInspect.usage')
  })
})

describe('跨宿主、弹窗与轮询的陈旧写入', () => {
  for (const name of ['reloadContainers', 'reloadImages', 'reloadVolumes', 'reloadNetworks']) {
    it(`${name} 回包只在目标与序号都未变时写回`, () => {
      const body = sliceFunction(`async function ${name}(): Promise<void> {`)
      expect(body).toContain('const id = targetId.value')
      expect(body).toContain('listFresh(')
    })
  }

  it('loadAll 按目标代次/列表序号丢弃旧轮写回', () => {
    const body = sliceFunction('async function loadAll(): Promise<void> {')
    expect(body).toContain('pinList(')
    expect(body).toContain("listFresh('containers'")
    expect(body).toContain("listFresh('volumes'")
  })

  it('inspect 回包先校验弹窗仍开着且宿主未换', () => {
    for (const header of [
      'async function openVolumeDetail(v: VolumeRow) {',
      'async function openNetworkDetail(n: NetworkRow) {',
    ]) {
      const body = sliceFunction(header)
      expect(body).toContain('inspectSeq')
      expect(body).toContain('const id = targetId.value')
      expect(body).toContain('dockerExec(')
    }
  })

  it('容器详情的三条取数腿已拆到子组件，页面不再自持日志轮询', () => {
    expect(src).toContain('<DockerContainerDetail')
    expect(src).not.toContain('async function loadLogs(')
    expect(src).not.toContain('followTimer')
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
    //（实心鲸鱼剪影，DeepSeek 风）。
    expect(titleBarSrc).toContain('<IconDocker')
    expect(titleBarSrc).toContain('IconDocker.vue')
    expect(src).toContain("import IconDocker from '@/components/icons/IconDocker.vue'")
    expect(iconDockerSrc).toContain('M2.6 12.9C2.6 9.4 5.5 7 9.1 7')
  })
})

describe('健康状态与计数', () => {
  it('unhealthy 归入异常：行警示与状态栏三计数互不重叠', () => {
    expect(src).toContain("c.health === 'unhealthy'")
    expect(src).toContain('abnormal: isAbnormal(c)')
    expect(src).toMatch(/const runningCount = computed\(\(\) =>[\s\S]*?c\.state === 'running' && !isAbnormal\(c\)/)
    expect(src).toMatch(/const abnormalCount = computed\(\(\) => containers\.value\.filter\(isAbnormal\)\.length\)/)
  })
  it('列表状态列对 unhealthy 用错误色，不沿用运行绿', () => {
    expect(src).toContain("unhealthy: c.health === 'unhealthy'")
  })
})

describe('镜像拉取与悬空清理', () => {
  it('容器详情使用拆出的子组件并回传关闭', () => {
    expect(src).toContain(':session-id="targetId"')
    expect(src).toContain('@close="selected = null"')
    expect(src).toContain("from './DockerContainerDetail.vue'")
    expect(src).not.toContain('class="logs-section"')
  })
  it('拉取走 600s 预算 + 审计，成功后重拉镜像列表', () => {
    expect(src).toContain('PULL_TIMEOUT_SEC = 600')
    const body = sliceFunction('async function doPull(): Promise<void> {')
    expect(body).toContain('pullCmd(')
    expect(body).toContain('PULL_TIMEOUT_SEC')
    expect(body).toContain('AUDIT_ACTION_DOCKER')
    expect(body).toContain('reloadImages(')
  })
  it('引用先校验，不给伪旗标与 shell 注入留口子', () => {
    expect(src).toContain('IMAGE_REF_RE')
    expect(src).toContain("startsWith('-')")
    expect(src).toContain('pullRefInvalid')
  })
  it('拉取进行中不可关闭弹窗', () => {
    expect(src).toMatch(/function closePull\(\): void \{[\s\S]*?pulling\.value/)
  })
  it('悬空清理只清 dangling，并复用通用 prune 计数', () => {
    // 按函数体断言：整文件的 toContain 分不清「悬空」与「未使用」两条腿各自用了
    // 哪条命令，把 imagePruneCmd 挪到未使用那条里它照样绿。
    const body = sliceFunction('async function doPruneImages(): Promise<void> {')
    expect(body).toContain('imagePruneCmd()')
    expect(body).toContain("'prune:images'")
    expect(body).toContain('countPrunedLines')
  })
})

describe('未使用镜像清理', () => {
  it('保留悬空入口，另给独立的未使用入口', () => {
    const body = sliceFunction('async function doPruneAllImages(): Promise<void> {')
    expect(body).toContain('imagePruneAllCmd()')
    expect(body).toContain("'prune:images-all'")
    expect(src).toContain('清理未使用')
  })
  it('确认框写明带标签镜像也在范围内，并给出可预判的计数口径', () => {
    const body = sliceFunction('async function doPruneAllImages(): Promise<void> {')
    expect(body).toContain('带标签的镜像也在范围内')
    expect(body).toContain('不可恢复')
    expect(body).toContain('unusedImageCount')
    expect(body).toContain('danglingCount')
  })
  it('docker ps 截断时引用状态降级为「—」，不把在用说成未使用', () => {
    expect(src).toContain('psTruncated')
    expect(src).toMatch(/unusedCountKnown = computed\(\(\) => !psTruncated\.value\)/)
    const body = sliceFunction('function imageUseHint(im: ImageRow)')
    expect(body).toContain("'—'")
    expect(body).toContain('unusedCountKnown.value')
  })
  it('镜像表有引用列，且按容器引用数展示', () => {
    expect(src).toContain('<th>引用</th>')
    expect(src).toContain('imageUseHint(im).text')
    expect(src).toContain('imageUsageCount')
  })
  it('清理成功后重拉镜像列表', () => {
    const body = sliceFunction('async function doPruneAllImages(): Promise<void> {')
    expect(body).toContain('reloadImages(')
    expect(body).toContain('countPrunedLines')
  })
})

describe('镜像导出 / 加载（纯远端）', () => {
  it('弹窗拆成子组件，不往 1600 行的页面里再堆一套表单', () => {
    expect(src).toContain("from './DockerImageTransferModal.vue'")
    expect(src).toContain('<DockerImageTransferModal')
    expect(src).toContain(':initial-dir="lastTransferDir"')
  })
  it('命令走 docker-core 构造，不在这里拼 shell 串', () => {
    expect(src).toContain('dockerSaveCmd(p.ref, p.path, p.gz)')
    expect(src).toContain('dockerLoadCmd(p.path)')
  })
  it('压缩格式由弹窗回传，不在页面里猜后缀', () => {
    expect(src).toContain("mode: 'export'; ref: string; path: string; gz: boolean")
    const body = sliceFunction('async function onTransferSubmit(')
    expect(body).toContain('p.gz')
  })
  it('gz 用同目录临时文件中转，退出码由 save && gzip 的真实结果决定，不靠大小猜', () => {
    const body = sliceFunction('async function onTransferSubmit(')
    expect(body).not.toContain('GZ_MIN_BYTES')
    expect(body).toContain('saveTempPath(p.path)')
  })
  it('用 classifyExit 三态判定，不套 dockerErrorFromResult（后者把 unknown 当失败抛）', () => {
    const body = sliceFunction('async function onTransferSubmit(')
    expect(body).toContain('classifyExit(res.exitCode)')
    expect(body).not.toContain('dockerErrorFromResult')
    expect(body).toContain("'unknown'")
  })
  it('save/load 用满 600s 预算并写审计', () => {
    expect(src).toContain('TRANSFER_TIMEOUT_SEC = 600')
    const body = sliceFunction('async function transferExec(')
    expect(body).toContain('TRANSFER_TIMEOUT_SEC')
    expect(body).toContain('AUDIT_ACTION_DOCKER')
    expect(body).toContain('writeDockerAudit(')
  })
  it('成败只看退出码；大小读不到只影响文案，不把成功报成失败', () => {
    const body = sliceFunction('async function onTransferSubmit(')
    expect(body).toContain("if (outcome === 'failed') throw transferFail")
    expect(body).toContain('statSize(')
    expect(body).not.toContain('!size')
  })
  it('超时/断链没有回包，也要带出可能残留的路径（含 gz 临时文件）', () => {
    const body = sliceFunction('async function onTransferSubmit(')
    expect(body).toContain('可能残留半截文件')
    expect(body).toContain('base.includes(p.path)')
  })
  it('失败文案带出可能残留的路径，不让人以为目录是干净的', () => {
    const body = sliceFunction('function transferFail(')
    expect(body).toContain('可能残留半截文件')
    expect(body).toContain('path')
  })
  it('执行中不可关闭：关面板不等于停掉远端进程', () => {
    expect(src).toMatch(/function closeTransfer\(\): void \{[\s\S]*?transferRunning\.value/)
  })
  it('成功后重拉镜像列表（新载入的镜像要立刻能看到）', () => {
    const body = sliceFunction('async function onTransferSubmit(')
    expect(body).toContain('reloadImages(')
  })
  it('行内入口用箱子图标，不跟拉取镜像的向下箭头撞脸', () => {
    expect(src).toContain('title="导出到远端目录（docker save）"')
    expect(src).toContain('openExport(im)')
  })
})

describe('动作锁与键盘可达', () => {
  it('任一动作进行中，所有资源动作按钮都禁用（不是只锁发起行）', () => {
    expect(src).not.toMatch(/:disabled="busy === /)
  })
  it('行内 Enter/Space 只在行本身聚焦时开详情，不劫持操作按钮', () => {
    expect(src).toContain('@keydown.enter.self.prevent')
    expect(src).toContain('@keydown.space.self.prevent')
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
    expect(sshViewSrc).toContain(`:active="activeNavPage === 'docker'"`)
  })
  it('SSH 单次输出截断会在状态栏明示', () => {
    expect(src).toContain('isOutputTruncated')
    expect(src).toContain('输出已截断')
  })
})

describe('底部状态栏与网络栏', () => {
  it('同步时间在每类资源回包后都更新，并标明实际取数的资源', () => {
    const labels: Record<string, string> = {
      reloadContainers: 'docker ps',
      reloadImages: 'docker images',
      reloadVolumes: 'docker volume ls',
      reloadNetworks: 'docker network ls',
    }
    for (const [name, label] of Object.entries(labels)) {
      const body = sliceFunction(`async function ${name}(): Promise<void> {`)
      expect(body, name).toContain('markSynced(')
      expect(body, name).toContain(label)
    }
    expect(sliceFunction('async function loadAll(): Promise<void> {')).toContain('markSynced(')
    expect(src).not.toContain("'docker ps · '")
    expect(src).toContain('已同步')
  })
  it('预置网络不提供必然失败的删除入口', () => {
    expect(src).toContain('isBuiltinNetwork(n.name)')
    expect(src).toContain('isBuiltinNetwork(selectedNetwork.name)')
  })
  it('无容器连接不再宣称可安全删除', () => {
    expect(src).not.toContain('可安全删除')
    expect(src).toContain('当前无容器连接')
  })
})
