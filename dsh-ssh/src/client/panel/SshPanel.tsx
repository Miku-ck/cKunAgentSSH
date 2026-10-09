/**
 * The SSH 运维 main panel: host tree (grouped by environment) on the left,
 * terminal / files / tunnels subtabs on the right, status bar along the bottom.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { SshApi } from '../api.ts'
import { createSshApi } from '../api.ts'
import type { HostSummary, StateSnapshot } from '../../protocol.ts'
import { FilesTab } from './FilesTab.tsx'
import { HostFormDialog } from './HostFormDialog.tsx'
import { TerminalTab } from './TerminalTab.tsx'
import { TunnelsTab } from './TunnelsTab.tsx'

const PANEL_ID = 'ckun-ssh-panel'

interface SlotsApi {
  inject(key: string, callback: () => () => void): () => void
  register(options: Record<string, unknown>, component: unknown): () => void
}

export function SshPanelIcon(): React.JSX.Element {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.75" y="2.25" width="12.5" height="11.5" rx="1.75" />
      <path d="M4.25 5.25l2.75 2.75-2.75 2.75" />
      <path d="M8.5 10.75h3.25" />
    </svg>
  )
}

export function IconImport(): React.JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2.5v7" />
      <path d="M5 7l3 3 3-3" />
      <path d="M3 10.75V13a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 13 13v-2.25" />
    </svg>
  )
}

export function IconKebab(): React.JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
      <circle cx="3.25" cy="8" r="1.15" />
      <circle cx="8" cy="8" r="1.15" />
      <circle cx="12.75" cy="8" r="1.15" />
    </svg>
  )
}

export function IconInfo(): React.JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <circle cx="8" cy="8" r="6.25" />
      <path d="M8 7.25v4" />
      <circle cx="8" cy="4.75" r=".4" fill="currentColor" />
    </svg>
  )
}

/** Register the panel (sidebar row + main content). Returns a disposer. */
export function registerSshPanel(slots: SlotsApi): () => void {
  const disposers: Array<() => void> = []
  disposers.push(
    slots.inject('sidebar.panellist', () =>
      slots.register(
        { name: 'sidebar.panellist', id: PANEL_ID, order: 45, label: () => 'SSH 运维' },
        SshPanelIcon,
      ),
    ),
  )
  disposers.push(
    slots.inject('main', () =>
      slots.register({ name: 'main', key: PANEL_ID, inject: () => ({ api: createSshApi() }) }, SshPanelPage),
    ),
  )
  return () => {
    for (const dispose of disposers.splice(0)) dispose()
  }
}

type Tab = 'terminal' | 'files' | 'tunnels'

export function SshPanelPage(): React.JSX.Element {
  const [api, setApi] = useState<SshApi | null>(null)
  const [hosts, setHosts] = useState<HostSummary[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('terminal')
  const [dialog, setDialog] = useState<{ mode: 'create' } | { mode: 'edit'; host: HostSummary } | null>(null)
  const [snapshot, setSnapshot] = useState<StateSnapshot>({ connections: {}, tunnels: [] })
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    setApi(createSshApi())
  }, [])

  const refresh = useCallback(async () => {
    if (!api) return
    try {
      const [h, s] = await Promise.all([api.listHosts(), api.state()])
      setHosts(h)
      setSnapshot(s)
      setLoadError('')
    } catch (error) {
      setLoadError((error as Error).message)
    }
  }, [api])

  useEffect(() => {
    void refresh()
    const timer = setInterval(() => void refresh(), 5000)
    return () => clearInterval(timer)
  }, [refresh])

  const groups = useMemo(() => {
    const q = query.toLowerCase()
    const filtered = q ? hosts.filter((h) => `${h.alias} ${h.host} ${h.user} ${h.description ?? ''}`.toLowerCase().includes(q)) : hosts
    const byEnv = new Map<string, HostSummary[]>()
    for (const h of filtered) {
      const env = h.environment ?? '未分组'
      const list = byEnv.get(env) ?? []
      list.push(h)
      byEnv.set(env, list)
    }
    const order = ['prod', ...[...byEnv.keys()].filter((k) => k !== 'prod')]
    return order.filter((env) => byEnv.has(env)).map((env) => ({ env, hosts: byEnv.get(env)! }))
  }, [hosts, query])

  const selectedHost = hosts.find((h) => h.alias === selected) ?? null
  const connState = selected ? (snapshot.connections[selected] ?? 'closed') : 'closed'
  const tunnelCount = snapshot.tunnels.filter((t) => t.alias === selected && t.state !== 'closed').length

  const importSshConfig = useCallback(async () => {
    if (!api) return
    try {
      const res = await fetch('/api/ckun-ssh/hosts/import', { method: 'POST', credentials: 'same-origin' })
      const body = (await res.json()) as { imported: number; skipped: number }
      await refresh()
      alert(`已导入 ${body.imported} 台（跳过已存在 ${body.skipped} 台）；密钥主机需先测试连接固定指纹。`)
    } catch (error) {
      setLoadError((error as Error).message)
    }
  }, [api, refresh])

  return (
    <div className="ckun-ssh-root">
      <div className="ckun-ssh-body">
        <div className="ckun-ssh-hosts">
          <div className="ckun-ssh-hosts-head">
            <span>SSH 主机</span>
            <span className="ckun-ssh-spacer" />
            <button className="ckun-ssh-icon-btn" title="从 ~/.ssh/config 导入" onClick={() => void importSshConfig()}>
              <IconImport />
            </button>
            <button className="ckun-ssh-btn primary" onClick={() => setDialog({ mode: 'create' })}>
              + 新主机
            </button>
          </div>
          <input className="ckun-ssh-search" placeholder="搜索别名 / 地址 / 描述…" value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="ckun-ssh-hosts-list">
            {loadError !== '' && <div className="ckun-ssh-error">{loadError}</div>}
            {hosts.length === 0 && loadError === '' && <div className="ckun-ssh-note" style={{ padding: '4px 8px' }}>暂无主机，点“+ 新主机”添加，或导入 ~/.ssh/config。</div>}
            {groups.map((group) => (
              <div key={group.env}>
                <div className="ckun-ssh-group-title">{group.env} · {group.hosts.length}</div>
                {group.hosts.map((h) => (
                  <div key={h.alias} className={`ckun-ssh-host-row${selected === h.alias ? ' active' : ''}`} onClick={() => setSelected(h.alias)}>
                    <span className={`ckun-ssh-dot ${h.environment === 'prod' ? 'prod' : snapshot.connections[h.alias] === 'open' ? 'open' : snapshot.connections[h.alias] === 'connecting' ? 'connecting' : 'closed'}`} />
                    <span style={{ minWidth: 0 }}>
                      <div className="ckun-ssh-name">
                        {h.alias}
                        {h.environment === 'prod' && <span className="ckun-ssh-badge prod">生产</span>}
                      </div>
                      <div className="ckun-ssh-target">
                        {h.user}@{h.host}:{h.port}
                        {!h.pinned && <span className="ckun-ssh-badge warn">未固定</span>}
                      </div>
                    </span>
                    <span className="ckun-ssh-spacer" style={{ flex: 1 }} />
                    <button
                      className="ckun-ssh-icon-btn"
                      title="编辑 / 删除"
                      onClick={(e) => {
                        e.stopPropagation()
                        setDialog({ mode: 'edit', host: h })
                      }}
                    >
                      <IconKebab />
                    </button>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
        <div className="ckun-ssh-main">
          {selectedHost ? (
            <>
              <div className="ckun-ssh-subtabs">
                {(
                  [
                    ['terminal', '终端'],
                    ['files', '文件'],
                    ['tunnels', '隧道'],
                  ] as Array<[Tab, string]>
                ).map(([key, label]) => (
                  <div key={key} className={`ckun-ssh-subtab${tab === key ? ' active' : ''}`} onClick={() => setTab(key)}>
                    {label}
                  </div>
                ))}
                <div className="ckun-ssh-hosttitle">
                  <span className={`ckun-ssh-dot ${connState === 'open' ? 'open' : connState === 'connecting' ? 'connecting' : 'closed'}`} />
                  {selectedHost.alias}
                  {selectedHost.environment === 'prod' && <span className="ckun-ssh-badge prod">生产</span>}
                  {!selectedHost.pinned && <span className="ckun-ssh-badge warn">主机密钥未固定</span>}
                </div>
              </div>
              <div className="ckun-ssh-content">
                {tab === 'terminal' && <TerminalTab key={`${selectedHost.alias}-term`} alias={selectedHost.alias} />}
                {tab === 'files' && api && <FilesTab key={`${selectedHost.alias}-files`} alias={selectedHost.alias} api={api} />}
                {tab === 'tunnels' && api && <TunnelsTab key={`${selectedHost.alias}-tunnels`} alias={selectedHost.alias} api={api} />}
              </div>
            </>
          ) : (
            <div className="ckun-ssh-empty">
              <div style={{ opacity: .6 }}>
                <svg width="30" height="30" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="1.75" y="2.25" width="12.5" height="11.5" rx="1.75" />
                  <path d="M4.25 5.25l2.75 2.75-2.75 2.75" />
                  <path d="M8.5 10.75h3.25" />
                </svg>
              </div>
              <div>从左侧选择一台主机开始</div>
              <div className="ckun-ssh-note">终端 / 文件 / 隧道 — 与 agent 的 ssh_* 工具共享同一连接池</div>
            </div>
          )}
        </div>
      </div>
      <div className="ckun-ssh-statusbar">
        <span>主机 {hosts.length} 台</span>
        <span>隧道 {snapshot.tunnels.filter((t) => t.state !== 'closed').length} 条</span>
        {selectedHost && (
          <>
            <span className="ckun-ssh-mono">
              {selectedHost.user}@{selectedHost.host}:{selectedHost.port}
            </span>
            <span>连接: {connState === 'open' ? '已连接' : connState === 'connecting' ? '连接中…' : '未连接'}</span>
            <span>主机密钥: {selectedHost.pinned ? '已固定' : '未固定'}</span>
            {tunnelCount > 0 && <span>本机隧道 {tunnelCount}</span>}
          </>
        )}
      </div>
      {dialog !== null && api && (
        <HostFormDialog
          api={api}
          existing={dialog.mode === 'edit' ? dialog.host : undefined}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null)
            void refresh()
          }}
        />
      )}
    </div>
  )
}
