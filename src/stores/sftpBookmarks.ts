/**
 * SFTP 路径收藏 — localStorage 持久化
 *
 * 本地与远程互相隔离：本地一份在所有连接间共享，远程按 connectionHost 分档。
 * 与 quickCommands 同模式（localStorage + 逐项过滤），但多了一层作用域分档。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { genId } from '@/utils/id'

export type PathBookmark = {
  id: string
  /** 展示名，默认取路径末段；存下来是为了将来支持重命名 */
  label: string
  path: string
  order: number
}

export const STORAGE_KEY = 'ckun-ssh-sftp-bookmarks'
export const MAX_BOOKMARKS_PER_SCOPE = 50

type BookmarkFile = {
  local: PathBookmark[]
  remote: Record<string, PathBookmark[]>
}

/** 路径末段作为默认名。根路径（`/`、`C:\`）去掉尾分隔符后为空，原样回落。 */
export function labelFromPath(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, '')
  if (!trimmed) return path.slice(0, 1) || '/'
  const idx = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  const base = idx >= 0 ? trimmed.slice(idx + 1) : trimmed
  return base || trimmed
}

/** 逐项过滤：一条脏记录（null/字符串/无 path）不能连坐整表被清空。
 *  同 path 只留第一条 — 历史数据里若出现重复，UI 会渲染两行且删除只删得掉一条。 */
function sanitizeRows(raw: unknown): PathBookmark[] {
  if (!Array.isArray(raw)) return []
  const seen = new Set<string>()
  const rows: PathBookmark[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const rec = item as Record<string, unknown>
    const path = typeof rec.path === 'string' ? rec.path.trim().slice(0, 4096) : ''
    if (!path || seen.has(path)) continue
    seen.add(path)
    const label = typeof rec.label === 'string' && rec.label.trim()
      ? rec.label.trim().slice(0, 60)
      : labelFromPath(path)
    rows.push({
      id: typeof rec.id === 'string' && rec.id ? rec.id : genId(),
      label,
      path,
      // NaN/Infinity 会让排序结果不确定，退回位置序号
      order: Number.isFinite(rec.order) ? rec.order as number : rows.length,
    })
    if (rows.length >= MAX_BOOKMARKS_PER_SCOPE) break
  }
  return rows
}

export const useSftpBookmarksStore = defineStore('sftpBookmarks', () => {
  const local = ref<PathBookmark[]>([])
  const remote = ref<Record<string, PathBookmark[]>>({})
  const loaded = ref(false)

  function load(): void {
    if (loaded.value) return
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed)) {
          // 旧版/手改数据可能是裸数组，按本地收藏读，避免整份丢弃
          local.value = sanitizeRows(parsed)
        } else if (parsed && typeof parsed === 'object') {
          const rec = parsed as Record<string, unknown>
          local.value = sanitizeRows(rec.local)
          const remoteRaw = rec.remote
          if (remoteRaw && typeof remoteRaw === 'object' && !Array.isArray(remoteRaw)) {
            const next: Record<string, PathBookmark[]> = {}
            for (const [host, rows] of Object.entries(remoteRaw as Record<string, unknown>)) {
              if (!host) continue
              next[host] = sanitizeRows(rows)
            }
            remote.value = next
          }
        }
      }
    } catch (err) {
      console.error('[sftpBookmarks] load failed:', err)
      local.value = []
      remote.value = {}
    }
    loaded.value = true
  }

  function save(): void {
    try {
      const payload: BookmarkFile = { local: local.value, remote: remote.value }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
    } catch (err) {
      console.error('[sftpBookmarks] save failed:', err)
    }
  }

  function listFor(side: 'local' | 'remote', hostKey: string): PathBookmark[] {
    const rows = side === 'local' ? local.value : (hostKey ? remote.value[hostKey] ?? [] : [])
    return rows.slice().sort((a, b) => a.order - b.order)
  }

  function isBookmarked(side: 'local' | 'remote', hostKey: string, path: string): boolean {
    if (!path) return false
    return listFor(side, hostKey).some(b => b.path === path)
  }

  /** 整份替换而不是原地改：remote 是按 host 分档的普通对象，
   *  原地 push 不会触发依赖 remote 的计算属性重新求值。 */
  function write(side: 'local' | 'remote', hostKey: string, rows: PathBookmark[]): void {
    if (side === 'local') {
      local.value = rows
      return
    }
    if (!hostKey) return
    remote.value = { ...remote.value, [hostKey]: rows }
  }

  /** @returns 'added' 新增、'removed' 取消收藏、'limit' 已达上限（未写入）、
   *  'noop' 参数不成立（空路径 / 远程缺 hostKey），调用方不应给出成功提示 */
  function toggle(
    side: 'local' | 'remote',
    hostKey: string,
    path: string,
  ): 'added' | 'removed' | 'limit' | 'noop' {
    if (!path || (side === 'remote' && !hostKey)) return 'noop'
    const rows = listFor(side, hostKey)
    const existing = rows.find(b => b.path === path)
    if (existing) {
      write(side, hostKey, rows.filter(b => b.id !== existing.id))
      save()
      return 'removed'
    }
    if (rows.length >= MAX_BOOKMARKS_PER_SCOPE) return 'limit'
    const nextOrder = rows.length > 0 ? Math.max(...rows.map(r => r.order)) + 1 : 0
    write(side, hostKey, [
      ...rows,
      { id: genId(), label: labelFromPath(path), path, order: nextOrder },
    ])
    save()
    return 'added'
  }

  function remove(side: 'local' | 'remote', hostKey: string, id: string): boolean {
    const rows = listFor(side, hostKey)
    if (!rows.some(b => b.id === id)) return false
    write(side, hostKey, rows.filter(b => b.id !== id))
    save()
    return true
  }

  return {
    local,
    remote,
    loaded,
    load,
    listFor,
    isBookmarked,
    toggle,
    remove,
  }
})
