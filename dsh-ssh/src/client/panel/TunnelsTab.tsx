/**
 * Local port-forward tunnel manager: 127.0.0.1:localPort -> remoteHost:remotePort
 * through the selected host (ssh -L semantics).
 */
import { useCallback, useEffect, useState } from 'react'
import type { SshApi } from '../api.ts'
import type { TunnelInfo } from '../../protocol.ts'

/** The tunnels subtab. */
export function TunnelsTab({ alias, api }: { alias: string; api: SshApi }): React.JSX.Element {
  const [tunnels, setTunnels] = useState<TunnelInfo[] | null>(null)
  const [localPort, setLocalPort] = useState('')
  const [remoteHost, setRemoteHost] = useState('127.0.0.1')
  const [remotePort, setRemotePort] = useState('')
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const result = await api.tunnel('list')
      setTunnels((result.tunnels ?? []).filter((t) => t.alias === alias))
    } catch (err) {
      setNote({ ok: false, text: (err as Error).message })
    }
  }, [alias, api])

  useEffect(() => {
    void refresh()
    const timer = setInterval(() => void refresh(), 5000)
    return () => clearInterval(timer)
  }, [refresh])

  const start = () => {
    const lp = Number(localPort)
    const rp = Number(remotePort)
    if (!Number.isInteger(rp) || rp <= 0) {
      setNote({ ok: false, text: '远程端口必须是正整数' })
      return
    }
    setBusy(true)
    setNote(null)
    api
      .tunnel('start', { alias, localPort: Number.isInteger(lp) && lp > 0 ? lp : 0, remoteHost, remotePort: rp })
      .then((r) => {
        setNote({ ok: true, text: `已建立本机 :${r.tunnel?.localPort} → ${r.tunnel?.remoteHost}:${r.tunnel?.remotePort}` })
        setLocalPort('')
        return refresh()
      })
      .catch((err) => setNote({ ok: false, text: (err as Error).message }))
      .finally(() => setBusy(false))
  }

  const stop = (id: string) => {
    setBusy(true)
    api
      .tunnel('stop', { tunnelId: id })
      .then(() => refresh())
      .catch((err) => setNote({ ok: false, text: (err as Error).message }))
      .finally(() => setBusy(false))
  }

  return (
    <div style={{ padding: '12px 14px', overflowY: 'auto', height: '100%' }}>
      <div style={{ fontSize: 12, color: 'var(--dsw-alias-label-secondary, #9aa1b0)', marginBottom: 10 }}>
        通过 {alias} 建立本地端口转发（等价 ssh -L 本机端口:目标:端口）。本机在 127.0.0.1 上监听。
      </div>
      <div className="ckun-ssh-form-row">
        <label>本机端口</label>
        <input className="ckun-ssh-input" style={{ maxWidth: 120 }} value={localPort} onChange={(e) => setLocalPort(e.target.value)} placeholder="0 = 随机" />
        <label>远程主机</label>
        <input className="ckun-ssh-input" value={remoteHost} onChange={(e) => setRemoteHost(e.target.value)} />
        <label>远程端口</label>
        <input className="ckun-ssh-input" style={{ maxWidth: 120 }} value={remotePort} onChange={(e) => setRemotePort(e.target.value)} placeholder="3306" />
        <button className="ckun-ssh-btn primary" disabled={busy} onClick={start}>
          建立隧道
        </button>
      </div>
      {note !== null && <div className={note.ok ? 'ckun-ssh-ok' : 'ckun-ssh-error'}>{note.text}</div>}
      {tunnels !== null && (
        <table className="ckun-ssh-table" style={{ marginTop: 12 }}>
          <thead>
            <tr>
              <th>本机</th>
              <th>→ 目标（经 {alias}）</th>
              <th>状态</th>
              <th style={{ width: 60 }} />
            </tr>
          </thead>
          <tbody>
            {tunnels.length === 0 && (
              <tr>
                <td colSpan={4} className="ckun-ssh-note">
                  当前主机没有隧道
                </td>
              </tr>
            )}
            {tunnels.map((t) => (
              <tr key={t.id}>
                <td className="ckun-ssh-mono">127.0.0.1:{t.localPort}</td>
                <td className="ckun-ssh-mono">
                  {t.remoteHost}:{t.remotePort}
                </td>
                <td>{t.state === 'open' ? '已建立' : t.state === 'closing' ? '关闭中' : '已关闭'}</td>
                <td>
                  {t.state !== 'closed' && (
                    <button className="ckun-ssh-btn danger" disabled={busy} onClick={() => stop(t.id)}>
                      关闭
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
