/**
 * SFTP Store — 管理 SFTP 双栏文件传输状态
 * 左栏(本地)走现有 fs* IPC,右栏(远程)走 sftp:* IPC
 *
 * 结构（docs/structure-refactor-plan.md P1）：本 store 只持有会话态与打开竞态
 * 守卫，文件面与传输面分别拆到 composables/useSftpFiles.ts 与
 * composables/useSftpTransfer.ts，这里组合二者并把公开接口原样再暴露 ——
 * 消费方（组件/测试）看到的接口与拆分前完全一致。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useToastStore } from './toast'
import { unwrapIpcErrorEnvelope } from '@/api/client'
import { useSftpFiles } from '@/composables/useSftpFiles'
import { MAX_CONCURRENT_CEIL, useSftpTransfer } from '@/composables/useSftpTransfer'

// B26/门面上限：SftpTransferDialog 从这里取天花板，转发保持导入路径不变
export { MAX_CONCURRENT_CEIL }
export type { TransferItem } from '@/composables/useSftpTransfer'

// 打开会话的代次。connect / connectFromTerminal 都要等一整套握手 + 列目录，期间
// 用户可以再发起一次（`online` 还是 false，图标与「连接远程」都照常可点），两条并发
// 完成时落败者会把全局身份抹成空串 —— 真正活着的那条会话因此再也关不掉（disconnect
// 见 sshSessionId 为空直接跳过 sftpClose/sshDisconnect），地址栏与列表还会分属两台主机。
let openSeq = 0

export const useSftpStore = defineStore('sftp', () => {
  const toast = useToastStore()

  /** 安全获取 electronAPI，未就绪时抛出明确错误 */
  function getApi(): ElectronAPI {
    if (!window.electronAPI) throw new Error('Electron API 未就绪')
    return window.electronAPI
  }

  // ─── 会话状态 ─────────────────────────────────
  const sshSessionId = ref('')
  const connectionName = ref('')
  const connectionHost = ref('')
  const online = ref(false)
  /** 标记当前 SFTP 是否复用终端 SSH 会话（复用时 disconnect 不关闭 SSH 连接） */
  const reusingTerminal = ref(false)
  /** 当前会话对应的连接配置 id（主进程会话池解析）。断开后保留：离线时也要能
   *  「清除已结束」本主机的历史记录。空串 = 尚未取得或会话不可归属。 */
  const activeConnectionId = ref('')

  // ─── 剪贴板（跨侧复制粘贴） ─────────────────────
  const clipboard = ref<{ side: 'local' | 'remote'; paths: string[] } | null>(null)

  // ─── 文件面 / 传输面（组合，见模块头注释） ──────
  const files = useSftpFiles({ getApi, sshSessionId: () => sshSessionId.value, online: () => online.value, toast })
  const transfer = useSftpTransfer({
    getApi,
    sshSessionId: () => sshSessionId.value,
    online: () => online.value,
    activeConnectionId: () => activeConnectionId.value,
    files: {
      localEntries: files.localEntries,
      remoteEntries: files.remoteEntries,
      localCwd: files.localCwd,
      remoteCwd: files.remoteCwd,
      localRefresh: files.localRefresh,
      remoteRefresh: files.remoteRefresh,
      expandLocalDir: files.expandLocalDir,
      expandRemoteDir: files.expandRemoteDir,
    },
  })

  // ─── 绑定 / 解绑 SSH 会话 ────────────────────
  /** 会话就绪后解析归属 id 并恢复该主机的上传历史。失败只告警不抛：
   *  记录是附加能力，不能因为它让一条已经连上的会话变成失败。 */
  async function afterSessionReady(myOpen: number): Promise<void> {
    try {
      if (myOpen !== openSeq) return
      const sid = sshSessionId.value
      const connId = sid ? await getApi().sftpGetSessionConnectionId(sid) : null
      if (myOpen !== openSeq) return
      activeConnectionId.value = connId || ''
      await transfer.restoreUploads()
    } catch (e) {
      console.warn('[sftp] 恢复上传记录失败:', e)
    }
  }

  /** 直接通过 SSH 连接 + SFTP 子系统打开远程会话(独立于终端会话) */
  async function connect(connection: SSHConnectionRecord): Promise<void> {
    if (online.value) await disconnect()
    const myOpen = ++openSeq
    reusingTerminal.value = false
    const plain = JSON.parse(JSON.stringify(connection))
    const { id, name } = await getApi().sshConnectSftp(plain)
    if (myOpen !== openSeq) {
      // 握手期间后起的打开已接管全局身份：这条新建的连接没人引用，就地拆掉并静默
      // 退出。写全局状态会把新会话的身份改写成旧主机，抛错还会多弹一次失败。
      try { await getApi().sshDisconnect(id) } catch { }
      return
    }
    sshSessionId.value = id
    connectionName.value = name || `${connection.username}@${connection.host}`
    connectionHost.value = `${connection.username}@${connection.host}:${connection.port}`
    try {
      await getApi().sftpOpen(id)
      if (myOpen !== openSeq) {
        try { await getApi().sftpClose(id) } catch { }
        try { await getApi().sshDisconnect(id) } catch { }
        return
      }
      online.value = true
      files.remoteCwd.value = '/'
      await files.loadRemote('/')
      await afterSessionReady(myOpen)
    } catch (err: any) {
      if (myOpen !== openSeq) {
        try { await getApi().sshDisconnect(id) } catch { }
        return
      }
      online.value = false
      files.remoteError.value = unwrapIpcErrorEnvelope(err?.message || '') || 'SFTP 打开失败'
      // 失败时关闭 SSH
      try { await getApi().sshDisconnect(id) } catch { }
      // 只在身份仍是本次打开设进去的那条时清空：否则抹掉的是后起会话的身份
      if (sshSessionId.value === id) sshSessionId.value = ''
      throw err
    }
  }

  /**
   * 复用已有终端 SSH 会话打开 SFTP 子系统，并跳转到终端当前工作目录
   * 不新建 SSH 连接，disconnect 时只关闭 SFTP 通道，不断开 SSH
   */
  async function connectFromTerminal(
    terminalSshId: string,
    name: string,
    host: string,
  ): Promise<void> {
    if (online.value) await disconnect()
    const myOpen = ++openSeq
    reusingTerminal.value = true
    sshSessionId.value = terminalSshId
    connectionName.value = name
    connectionHost.value = host
    try {
      await getApi().sftpOpen(terminalSshId)
      if (myOpen !== openSeq) {
        // 后起那次已接管全局状态。只有当前身份不是本终端时才回收本终端的 SFTP
        // 子系统 —— 两次点开同一台时，关掉会把接管者一起打死。
        if (sshSessionId.value !== terminalSshId) {
          try { await getApi().sftpClose(terminalSshId) } catch { }
        }
        return
      }
      online.value = true
      // 获取终端当前工作目录：优先用 OSC 7 跟踪值，为空则 exec pwd
      let cwd = await getApi().sshGetCwd(terminalSshId)
      if (!cwd) {
        const result = await getApi().sshExec(terminalSshId, 'pwd')
        cwd = (result.stdout || '').trim()
      }
      if (myOpen !== openSeq) return
      const target = cwd || '/'
      files.remoteCwd.value = target
      await files.loadRemote(target)
      await afterSessionReady(myOpen)
    } catch (err: any) {
      if (myOpen !== openSeq) {
        // 旧代的失败不改全局在线态、也不弹给用户；自己那条子系统若没被接管就顺手回收
        if (sshSessionId.value !== terminalSshId) {
          try { await getApi().sftpClose(terminalSshId) } catch { }
        }
        return
      }
      online.value = false
      files.remoteError.value = unwrapIpcErrorEnvelope(err?.message || '') || 'SFTP 打开失败'
      // 复用模式下不关闭 SSH 连接（终端仍需要）
      try { await getApi().sftpClose(terminalSshId) } catch { }
      if (sshSessionId.value === terminalSshId) sshSessionId.value = ''
      reusingTerminal.value = false
      throw err
    }
  }

  async function disconnect(): Promise<void> {
    if (sshSessionId.value) {
      try { await getApi().sftpClose(sshSessionId.value) } catch { }
      // 复用终端模式不关闭 SSH 连接（终端仍需要），仅关闭 SFTP 子系统
      if (!reusingTerminal.value) {
        try { await getApi().sshDisconnect(sshSessionId.value) } catch { }
      }
    }
    sshSessionId.value = ''
    connectionName.value = ''
    connectionHost.value = ''
    online.value = false
    reusingTerminal.value = false
    // 清空远程列表态并使在途列目录失效（含 selectedRemote 清空与 remoteError 复位）
    files.clearRemoteForDisconnect()
    // 同理推进打开代次：断线时若在途的 connect 还在握手，那一次回来后不该把
    // online/sshSessionId 重新点亮成一台已经不存在的主机
    openSeq++
    // 跨主机的远程剪贴板必须一起清：不清则换主机后「粘贴」会把 A 机的绝对路径
    // cp -r 到 B 机上（canPaste 只比 side，比不出主机）
    clipboard.value = null
    // m4 修复的补充：transferSeq 在 store 生命周期内单调递增，不再随 disconnect 重置，
    // 保证断线重连后新传输记录 id 不会与旧记录重复
    // 传输面收尾：清待删定时器、在途任务按类别取消/暂停、复制行清空（原 disconnect 逻辑）
    transfer.handleDisconnect()
  }

  // ─── 剪贴板操作 ───────────────────────────────
  function copyToClipboard(side: 'local' | 'remote', paths: string[]): void {
    clipboard.value = { side, paths: [...paths] }
  }

  return {
    // state
    sshSessionId, connectionName, connectionHost, online, activeConnectionId,
    clipboard,
    ...files,
    ...transfer,
    // session
    connect, connectFromTerminal, disconnect,
    // clipboard
    copyToClipboard,
  }
})
