/**
 * 任务台执行落点：本地 / 工作树 / SSH。
 *
 * 三者收敛到同一个事实源 `ssh.activeSessionId` —— 后端只认 ssh_session_id，
 * visible_terminal 把命令交付回前端后由 useAgentDeliver 按 session.type 分流到
 * ptyWrite / sshWrite。所以切换落点不需要给 run 请求新增 workdir 字段，也不用
 * 动后端 harness / bridge 那一整条链。
 *
 * 「工作树」= 以该 git worktree 路径为 cwd 的本地 PTY 会话（createLocalTerminal
 * 的 cwd 参数原生支持，GitReposPanel「打开终端」走的是同一条路）。同一工作树
 * 复用已开的会话，避免每选一次就堆一个标签。落点类型从真实会话反查，用户在
 * 终端页切会话时 chip 会跟着变，不另存一份会漂的状态。
 */
import { computed, ref, watch } from 'vue'
import { acceptHMRUpdate, defineStore } from 'pinia'
import { useSSHStore } from './ssh'
import type { GitWorktree } from '../../shared/git-repos-core'

export type ExecTargetKind = 'local' | 'worktree' | 'ssh'

export interface WorktreeGroup {
  repo: string
  items: GitWorktree[]
  error?: string
}

/** 与 GitReposPanel「打开终端」一致的默认本地 shell。UI 也读它来判断
 *  「这个 shell 名值不值得显示」——默认值不占位，非默认（WSL）才提示。 */
export const DEFAULT_LOCAL_SHELL = 'PowerShell'
/** 登记表可能脏，一次最多探 20 个仓库，别让一次下拉卡住整条输入栏。 */
const MAX_REPOS = 20

/** 目录面的「目录记录」落盘位置。只存用户自己打开过的目录 —— 记录是选择的结果，
 *  不是对磁盘的扫描结果（2026-11-11 用户口径：默认不在项目就不读任何文件夹）。 */
const RECENT_DIRS_KEY = 'ckun.exec-target.recent-dirs'
const MAX_RECENT_DIRS = 8

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** 目录比较键：分隔符统一成 `/`、去尾斜杠、转小写。
 *
 *  同一个目录在系统里会有多种写法 —— 目录选择器给的 `D:\work\app`、pty 初始化回报的
 *  `D:/work/app/`、大小写不同的盘符，都是同一个地方。按原字符串比较就会在列表里
 *  摆出两行同名文件夹（2026-11-11 用户截图：cKunAgentSSH 出现两次，一个带勾一个不带）。
 *  本地落点只在本机（Windows 路径大小写不敏感），小写化不会把两个真实目录并成一个。 */
export function dirKey(p: string): string {
  return p.replace(/[\\/]+/g, '/').replace(/\/+$/, '').toLowerCase()
}

/** 单测跑在 node 环境（没有 localStorage）：读不到就当没有记录，不抛。 */
function readRecentDirs(): string[] {
  try {
    const raw = typeof localStorage === 'undefined' ? '' : localStorage.getItem(RECENT_DIRS_KEY)
    const arr: unknown = raw ? JSON.parse(raw) : []
    if (!Array.isArray(arr)) return []
    // 历史记录里可能已经躺着同一目录的两种写法：读的时候按 dirKey 归并，只留先出现的那条
    const seen = new Set<string>()
    const out: string[] = []
    for (const x of arr) {
      if (typeof x !== 'string' || !x) continue
      const k = dirKey(x)
      if (seen.has(k)) continue
      seen.add(k)
      out.push(x)
    }
    return out
  } catch {
    return []
  }
}

export const useExecTargetStore = defineStore('execTarget', () => {
  const ssh = useSSHStore()
  /** sessionId → 该会话是以哪个 worktree 为 cwd 开的。只在本次应用生命周期内有效。 */
  const worktreeBySession = ref<Record<string, GitWorktree>>({})
  const groups = ref<WorktreeGroup[]>([])
  /** 「仓库」chip 选中的路径；空 = 跟随当前工作树，没有工作树时取第一个仓库。 */
  const selectedRepo = ref('')
  const loading = ref(false)
  const error = ref('')
  /** 目录面列的就是这一份：用户打开过的目录，最近在前。空 = 还没打开过任何文件夹，
   *  目录面只剩「不在项目中工作」与「打开文件夹…」两行（不预读会话 / 工作树 / 仓库）。 */
  const recentDirs = ref<string[]>(readRecentDirs())

  const hasRepos = computed(() => groups.value.length > 0)
  const worktreeCount = computed(() => groups.value.reduce((n, g) => n + g.items.length, 0))

  /** 当前落点从真实会话反查；会话已关 / 无会话一律 null，不臆造选中态。 */
  const kind = computed<ExecTargetKind | null>(() => {
    const id = ssh.activeSessionId
    if (!id) return null
    const session = ssh.sessions.find(s => s.id === id)
    if (!session) return null
    if (worktreeBySession.value[id]) return 'worktree'
    return session.type === 'remote' ? 'ssh' : 'local'
  })

  const activeWorktree = computed(() =>
    ssh.activeSessionId ? worktreeBySession.value[ssh.activeSessionId] ?? null : null,
  )

  /** 当前落点**绑定的**那个目录：工作树路径 / 本地会话建会话时给的 cwd。
   *  故意不读实时 cwd —— 「不在项目中工作」的会话也会同步上来主目录，那是终端自己
   *  在跑，不是用户选的文件夹；拿它当绑定目录，主目录就会被当成一个"项目"（用户
   *  2026-11-11 反馈：默认不在项目却读出了文件夹，还和「不在项目中」同时打勾）。 */
  const activeBoundDir = computed(() => {
    const id = ssh.activeSessionId
    if (!id) return ''
    const wt = worktreeBySession.value[id]
    if (wt) return wt.path
    const s = ssh.sessions.find(x => x.id === id)
    return s && s.type === 'local' ? (s.cwd || '') : ''
  })

  function persistRecentDirs(): void {
    try {
      if (typeof localStorage !== 'undefined') localStorage.setItem(RECENT_DIRS_KEY, JSON.stringify(recentDirs.value))
    } catch {
      // 存储不可用（隐私模式 / 配额满）只影响下次启动的候选，不影响本次切换
    }
  }

  /** 打开过一个目录就记住它：去重（按 dirKey，同一目录的不同写法算一条）、最近在前、最多 8 条。 */
  function rememberDir(path: string): void {
    if (!path) return
    const k = dirKey(path)
    recentDirs.value = [path, ...recentDirs.value.filter(p => dirKey(p) !== k)].slice(0, MAX_RECENT_DIRS)
    persistRecentDirs()
  }

  /** 删掉一条目录记录：只影响这个候选列表，不关会话、不动磁盘。 */
  function removeRecentDir(path: string): void {
    const k = dirKey(path)
    recentDirs.value = recentDirs.value.filter(p => dirKey(p) !== k)
    persistRecentDirs()
  }

  /** 本地落点实际使用的 shell（PowerShell / WSL…）：从激活会话读，不臆造。
   *  无会话 / 远程会话返回空。**不上 chip** —— 本地落点就是「就在这个文件夹」，
   *  默认 shell 是 PowerShell 属实现细节，印在底栏只占宽不增信息（2026-11-10）。 */
  const activeShell = computed(() => {
    const id = ssh.activeSessionId
    if (!id) return ''
    const s = ssh.sessions.find(x => x.id === id)
    return s && s.type === 'local' ? (s.shell || '') : ''
  })

  /** 第二枚 chip 的读数：「我在哪个目录」。三态常驻，只换语义 ——
   *  本地读会话 cwd 的末级名，工作树读仓库名，SSH 读远程目录的末级名（全路径见
   *  activeDirPath）。这枚不能省：没有它，「本地」和「工作树」看起来就是两个无名
   *  的地方（2026-11-10 用户以参考实现纠正：本地也要显示当前文件夹）。
   *
   *  本地**没有绑定目录**时返回空串，不编「主目录」（2026-11-11 用户口径）：
   *  主目录不是用户选的项目，写在 chip 上等于把所有「没选目录」的状态都谎报成
   *  同一个位置。空值的文案由界面决定（现在那枚 chip 变成「打开文件夹」入口）。 */
  /** 当前远程落点的会话是否已经不在了（主进程移除 / 重连放弃 / 探测反证）。
   *  chip 与 title 都要据此改口，别再把它画成一个活着的 home 目录。 */
  const activeDirUnavailable = computed(() => {
    if (kind.value !== 'ssh') return false
    const s = ssh.sessions.find(x => x.id === ssh.activeSessionId)
    return !!s && (!!s.reconnectGaveUp || !!s.unavailable)
  })

  const activeDirLabel = computed(() => {
    const k = kind.value
    const id = ssh.activeSessionId
    if (k === 'worktree') return activeRepoLabel.value
    // 目录一律走 sessionCwd：实时 cwd 优先（用户 cd 之后它才是真相），初始 cwd 兜底。
    // SSH 只取**末级目录名**：这枚 chip 和「本地」那枚一样宽，印 `/home/ckun/www/app`
    // 会被挤掉半截；全路径进 title（activeDirPath）。读不到才落 `~`。
    if (k === 'ssh') {
      // 主进程侧已无此会话（探测反证 / 重连放弃）：不能再印 `~`，那会让一个死目标
      // 看起来像活着的 home 目录（2026-11-11 用户截图：连过的会话早被主进程移除了，
      // chip 还挂着 `~`，误以为是「连上了但没取到目录」）。
      if (activeDirUnavailable.value) return '未连接'
      const cwd = id ? ssh.sessionCwd(id) : ''
      return cwd ? repoLabel(cwd) : '~'
    }
    if (k === 'local') {
      // 没有绑定目录 = 不在项目中工作：不读终端实时 cwd（那是主目录，不是用户选的）
      if (!activeBoundDir.value) return ''
      const cwd = id ? ssh.sessionCwd(id) : ''
      return cwd ? repoLabel(cwd) : ''
    }
    return ''
  })

  /** 目录 chip 的完整路径：chip 上只印末级名，悬停要能看到自己在哪台机器的哪个目录。
   *  SSH 读远程 cwd（OSC 7 / 一次性 pwd 兜底），本地与工作树读绑定目录。 */
  const activeDirPath = computed(() => {
    const k = kind.value
    const id = ssh.activeSessionId
    if (k === 'ssh') return id ? ssh.sessionCwd(id) : ''
    if (k === 'worktree') return activeRepoPath.value
    return activeBoundDir.value
  })

  /** 落在远程时补一次 pwd：OSC 7 只由终端页轮询上报，刚连上就回首页时它是空的，
   *  不补的话目录 chip 会一直显示 `~`（用户 2026-11-11 反馈「连接成功但不显示目录」）。
   *  切回同一台已探测过的会话时 sessionCwd 已有值，ensureTerminalCwd 直接返回，不重复往返。
   *
   *  两种会话不发探测：重连已放弃、或已被反证为不可用 —— 对它们发 IPC 只会换来一条
   *  SESSION_NOT_FOUND 错误日志；重连成功后 ssh store 会清掉这两个标记，目标重新变成
   *  有效 id，这个 watch 于是再补一次。 */
  const cwdProbeTarget = computed(() => {
    if (kind.value !== 'ssh') return ''
    const id = ssh.activeSessionId
    const s = ssh.sessions.find(x => x.id === id)
    if (!s || s.reconnectGaveUp || s.unavailable) return ''
    return id
  })
  watch(cwdProbeTarget, (id) => { if (id) void ssh.ensureTerminalCwd(id) }, { immediate: true })

  /** 幽灵会话自动回拨：每个会话 id 只试一次。
   *
   *  用户看到的现象是「连上了，但目录 chip 不显示文件夹」，真正原因是主进程池里早就
   *  没有这条会话（标签是渲染层残留）。只把 chip 改成「未连接」并不够 —— 首页没有任何
   *  地方能把这条连接重新拨起来，用户只能去终端页关标签重连。这里替他做掉这一次：
   *  连上之后 ssh store 会清掉 unavailable，cwdProbeTarget 立刻重新生效并补 pwd，
   *  chip 也就在下一帧显示远端真实目录。
   *
   *  失败不静默：写进 landing.error（目录面里看得见），并保留「未连接」读数 —— 网络真的
   *  断着时，反复自动重试只会把同一个失败刷屏，一次就够，之后由用户点「重新连接」。 */
  const redialTried = new Set<string>()
  watch(
    () => {
      if (kind.value !== 'ssh') return ''
      const id = ssh.activeSessionId
      const s = id ? ssh.sessions.find(x => x.id === id) : undefined
      // 两种「chip 上说未连接」的成因都算：探测反证的幽灵会话、自动重连放弃的会话。
      // 它们在主进程里都没有可用连接，也都是首页这一枚 chip 自己没法自愈的状态。
      return s && (s.unavailable || s.reconnectGaveUp) ? id : ''
    },
    (id) => {
      if (!id || redialTried.has(id)) return
      redialTried.add(id)
      void ssh.redial(id).catch((e: unknown) => {
        error.value = `重新连接失败：${messageOf(e)}`
      })
    },
    { immediate: true },
  )

  /** 只有工作树落点才有「分支」这第三枚 chip。本地是「就在这个文件夹」，
   *  SSH 是「另一台机器上的某个目录」—— 分支对两者都无意义。 */
  const showBranch = computed(() => kind.value === 'worktree')

  /** SSH 落点的主读数：连接名优先（用户自己起的名字认得出），退到会话的
   *  username@host。非远程落点返回空 —— 调用方据此走「选择落点」占位，不臆造。 */
  const activeRemoteLabel = computed(() => {
    const id = ssh.activeSessionId
    if (!id) return ''
    const s = ssh.sessions.find(x => x.id === id)
    if (!s || s.type !== 'remote') return ''
    const conn = s.connectionId ? ssh.connections.find(c => c.id === s.connectionId) : undefined
    return conn?.name || s.shell || ''
  })

  /** 顶栏「仓库 / 分支」两枚 chip 的读数：始终从真实状态派生，不另存一份会漂的选中态。 */
  const activeRepoPath = computed(() => {
    if (selectedRepo.value) return selectedRepo.value
    const wt = activeWorktree.value
    if (wt) {
      const owner = groups.value.find(g => g.items.some(w => w.path === wt.path))
      if (owner) return owner.repo
    }
    return groups.value[0]?.repo || ''
  })
  const activeGroup = computed(() =>
    groups.value.find(g => g.repo === activeRepoPath.value) ?? null,
  )
  const activeRepoLabel = computed(() => repoLabel(activeRepoPath.value))
  const activeBranch = computed(() => {
    const wt = activeWorktree.value
    if (wt) return wt.branch || (wt.detached ? 'detached HEAD' : 'bare')
    const items = activeGroup.value?.items || []
    const first = items.find(w => w.main) || items[0]
    return first ? (first.branch || (first.detached ? 'detached HEAD' : 'bare')) : '—'
  })

  function selectRepo(path: string): void {
    selectedRepo.value = path
  }

  function repoLabel(path: string): string {
    const parts = path.split(/[\\/]/).filter(Boolean)
    return parts[parts.length - 1] || path
  }

  async function loadWorktrees(): Promise<void> {
    const api = window.electronAPI
    if (!api) {
      error.value = '工作树仅在桌面端可用'
      groups.value = []
      return
    }
    loading.value = true
    error.value = ''
    try {
      const reg = await api.gitreposGetRegistry()
      const repos = (reg.local || []).slice(0, MAX_REPOS)
      groups.value = await Promise.all(repos.map(async (repo): Promise<WorktreeGroup> => {
        try {
          return { repo, items: await api.gitreposListWorktrees(repo) }
        } catch (e) {
          // 单仓失败不该让其它仓库整片消失 —— 逐仓留原因，UI 里单独标灰
          return { repo, items: [], error: messageOf(e) }
        }
      }))
      if (!repos.length) error.value = '还没有登记本机仓库，可点「登记本机仓库」'
    } catch (e) {
      error.value = messageOf(e)
      groups.value = []
    } finally {
      loading.value = false
    }
  }

  /** 登记一个本机仓库（目录选择器 + addLocal），随后刷新列表；取消返回 false。 */
  async function registerRepo(): Promise<boolean> {
    const api = window.electronAPI
    if (!api) return false
    error.value = ''
    try {
      const picked = await api.gitreposPickDirectory()
      if (!picked) return false
      await api.gitreposAddLocal(picked)
      await loadWorktrees()
      return true
    } catch (e) {
      error.value = messageOf(e)
      return false
    }
  }

  /** 「本地」：优先复用已开的普通本地会话，没有就开一个默认 cwd 的。 */
  async function chooseLocal(): Promise<void> {
    error.value = ''
    try {
      const existing = ssh.sessions.find(s => s.type === 'local' && !worktreeBySession.value[s.id])
      if (existing) {
        ssh.switchSession(existing.id)
        return
      }
      await ssh.createLocalTerminal(DEFAULT_LOCAL_SHELL)
    } catch (e) {
      error.value = messageOf(e)
    }
  }

  /** 「工作树」：选中即以该路径为 cwd 开本地 PTY；同路径已有会话就直接切过去。 */
  async function chooseWorktree(w: GitWorktree): Promise<void> {
    error.value = ''
    try {
      const hit = Object.entries(worktreeBySession.value).find(
        ([id, wt]) => wt.path === w.path && ssh.sessions.some(s => s.id === id),
      )
      if (hit) {
        ssh.switchSession(hit[0])
        return
      }
      const id = await ssh.createLocalTerminal(DEFAULT_LOCAL_SHELL, w.path)
      worktreeBySession.value = { ...worktreeBySession.value, [id]: w }
      const owner = groups.value.find(g => g.items.some(x => x.path === w.path))
      if (owner) selectedRepo.value = owner.repo
    } catch (e) {
      error.value = messageOf(e)
    }
  }

  /** 「工作树」不带参数地选中：取当前仓库的主工作树（没有主仓就取第一枚可用的）。
   *  菜单里点「工作树」只是选落点，不逼着人立刻指定分支 —— 分支由上一步出现的
   *  分支 chip 再换（2026-11-10 用户口径：点一下不该钻进二级目录）。 */
  async function chooseWorktreeDefault(): Promise<void> {
    error.value = ''
    const group = activeGroup.value ?? groups.value.find(g => g.items.length > 0) ?? null
    if (!group) {
      error.value = '还没有登记本机仓库，点「登记本机仓库…」'
      return
    }
    const usable = group.items.filter(w => !w.locked && !w.prunable)
    const target = usable.find(w => w.main) || usable[0] || null
    if (!target) {
      error.value = '这个仓库没有可用的工作树'
      return
    }
    await chooseWorktree(target)
  }

  /** 「本地」换目录：以该路径为 cwd 开本地 PTY；同路径已有会话就直接切过去，
   *  不为点一次文件夹堆一个标签。判定按 sessionCwd 而不是 session.cwd —— 用户
   *  在终端里 cd 到某个目录后，那个会话就是「这个目录的会话」。 */
  async function chooseLocalDir(path: string): Promise<void> {
    error.value = ''
    try {
      const hit = ssh.sessions.find(s => s.type === 'local' && !worktreeBySession.value[s.id] && dirKey(ssh.sessionCwd(s.id)) === dirKey(path))
      if (hit) {
        ssh.switchSession(hit.id)
        rememberDir(path)
        return
      }
      await ssh.createLocalTerminal(DEFAULT_LOCAL_SHELL, path)
      rememberDir(path)
    } catch (e) {
      error.value = messageOf(e)
    }
  }

  /** 「不在项目中工作」：回到一个不绑定任何目录的本地会话（落在主目录）。
   *  与 chooseLocal 的区别：后者只要求「不是工作树会话」，带目录的会话也算；
   *  这里要的是**没有初始 cwd** 那种，点完 chip 上不该还挂着上一个文件夹。
   *  判定用静态 cwd（建会话时给的），不用实时 cwd —— 无目录会话同步上来的
   *  实时 cwd 就是主目录，拿它判会每次都新开一个终端。 */
  async function chooseLocalHome(): Promise<void> {
    error.value = ''
    try {
      const hit = ssh.sessions.find(s => s.type === 'local' && !worktreeBySession.value[s.id] && !s.cwd)
      if (hit) {
        ssh.switchSession(hit.id)
        return
      }
      await ssh.createLocalTerminal(DEFAULT_LOCAL_SHELL)
    } catch (e) {
      error.value = messageOf(e)
    }
  }

  /** 目录选择器：选中即在那个目录落一个本地会话；取消返回 false。 */
  async function pickLocalDir(): Promise<boolean> {
    const api = window.electronAPI
    if (!api) return false
    error.value = ''
    try {
      const picked = await api.gitreposPickDirectory()
      if (!picked) return false
      await chooseLocalDir(picked)
      return true
    } catch (e) {
      error.value = messageOf(e)
      return false
    }
  }

  /** 「连接 WSL」：与终端页的 LOCAL_SHELLS 同一条通路，WSL 未装发行版的错误
   *  由 createLocalTerminal 抛出后落到 error，菜单里直接看得见，不静默失败。 */
  async function chooseWsl(): Promise<void> {
    error.value = ''
    try {
      await ssh.createLocalTerminal('WSL')
    } catch (e) {
      error.value = messageOf(e)
    }
  }

  /**
   * 新建工作树（参考 Trae 的任务驱动形态）：主进程自动 `git worktree add -b`
   * 建分支 + 仓库旁目录，成功后刷新列表并立刻把落点切过去——用户的下一个
   * 指令就落在新分支上。创建失败（分支重名 / 目录已存在）落 error，菜单可见。
   */
  async function createWorktree(repo: string, branch: string): Promise<GitWorktree | null> {
    const api = window.electronAPI
    if (!api) {
      error.value = '工作树仅在桌面端可用'
      return null
    }
    error.value = ''
    try {
      const wt = await api.gitreposCreateWorktree(repo, branch)
      await loadWorktrees()
      await chooseWorktree(wt)
      return wt
    } catch (e) {
      error.value = messageOf(e)
      return null
    }
  }

  /** 清理失效工作树记录（git worktree prune）：只清 git 的管理数据，随后刷新列表。 */
  async function pruneWorktrees(repo: string): Promise<boolean> {
    const api = window.electronAPI
    if (!api) return false
    error.value = ''
    try {
      await api.gitreposPruneWorktrees(repo)
      await loadWorktrees()
      return true
    } catch (e) {
      error.value = messageOf(e)
      return false
    }
  }

  function chooseSsh(sessionId: string): void {
    ssh.switchSession(sessionId)
  }

  return {
    kind, activeWorktree, activeShell, activeDirLabel, activeDirPath, activeDirUnavailable, showBranch, activeRemoteLabel, groups, hasRepos, worktreeCount, loading, error,
    activeRepoPath, activeRepoLabel, activeGroup, activeBranch, selectedRepo,
    recentDirs, activeBoundDir, rememberDir, removeRecentDir,
    loadWorktrees, registerRepo, selectRepo, chooseLocal, chooseWorktree, chooseWorktreeDefault, chooseWsl, chooseSsh,
    chooseLocalDir, chooseLocalHome, pickLocalDir,
    createWorktree, pruneWorktrees,
  }
})

// 开发期热更本文件时，把新增的 action 补进**已存在**的 store 实例。
// 没有这一句：模块被替换、Pinia 仍持旧 setup 的实例，新 action 在运行中的窗口里
// 是 undefined（表现为 `landing.chooseLocalDir is not a function`），改完还得整窗重载。
if (import.meta.hot) {
  import.meta.hot.accept(acceptHMRUpdate(useExecTargetStore, import.meta.hot))
}
