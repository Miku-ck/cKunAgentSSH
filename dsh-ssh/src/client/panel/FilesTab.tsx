/**
 * SFTP browser tab: directory listing, single-file download, single-file upload.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { SshApi } from '../api.ts'
import type { RemoteDirEntry } from '../../protocol.ts'

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function FolderIcon(): React.JSX.Element {
  return (
    <svg className="ckun-ssh-file-icon dir" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round">
      <path d="M1.75 4.5A1.5 1.5 0 0 1 3.25 3h3L7.75 4.75h5A1.5 1.5 0 0 1 14.25 6.25v5A1.5 1.5 0 0 1 12.75 12.75H3.25A1.5 1.5 0 0 1 1.75 11.25v-6.75Z" />
    </svg>
  )
}

function FileIcon(): React.JSX.Element {
  return (
    <svg className="ckun-ssh-file-icon file" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 2.25h5.5l3 3v8.5H4z" />
      <path d="M9.5 2.25v3h3" />
    </svg>
  )
}

function parentOf(path: string): string {
  const trimmed = path.replace(/\/+$/, '')
  const idx = trimmed.lastIndexOf('/')
  if (idx <= 0) return '/'
  return trimmed.slice(0, idx)
}

export function FilesTab({ alias, api }: { alias: string; api: SshApi }): React.JSX.Element {
  const [path, setPath] = useState('/')
  const [entries, setEntries] = useState<RemoteDirEntry[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(
    async (p: string) => {
      setError('')
      setEntries(null)
      try {
        const result = await api.ls(alias, p === '' ? '/' : p)
        setPath(result.path)
        setEntries(result.entries)
      } catch (err) {
        setError((err as Error).message)
      }
    },
    [alias, api],
  )

  useEffect(() => {
    void load('/')
  }, [load])

  const onPick = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (file === undefined) return
    setBusy(true)
    setError('')
    api
      .upload(alias, path, file.name, file)
      .then(() => void load(path))
      .catch((err) => setError(`上传失败: ${(err as Error).message}`))
      .finally(() => setBusy(false))
  }

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div className="ckun-ssh-files-bar">
        <button className="ckun-ssh-btn" disabled={path === '/'} onClick={() => void load(parentOf(path))}>
          上级
        </button>
        <input
          className="ckun-ssh-input ckun-ssh-mono"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void load(path)
          }}
        />
        <button className="ckun-ssh-btn primary" onClick={() => void load(path)}>
          打开
        </button>
        <button className="ckun-ssh-btn" disabled={busy} onClick={() => fileRef.current?.click()}>
          上传文件
        </button>
        <input ref={fileRef} type="file" style={{ display: 'none' }} onChange={onPick} />
        <span className="ckun-ssh-note">上传到当前目录{busy ? '（进行中…）' : ''}</span>
      </div>
      <div className="ckun-ssh-files-table">
        {error !== '' && <div className="ckun-ssh-error">{error}</div>}
        {entries === null ? (
          <div className="ckun-ssh-note">加载中…</div>
        ) : (
          <table className="ckun-ssh-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th>大小</th>
                <th>修改时间</th>
                <th style={{ width: 70 }} />
              </tr>
            </thead>
            <tbody>
              {entries.length === 0 && (
                <tr>
                  <td colSpan={5} className="ckun-ssh-empty-row">
                    <svg width="22" height="22" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round">
                      <path d="M1.75 4.5A1.5 1.5 0 0 1 3.25 3h3L7.75 4.75h5A1.5 1.5 0 0 1 14.25 6.25v5A1.5 1.5 0 0 1 12.75 12.75H3.25A1.5 1.5 0 0 1 1.75 11.25v-6.75Z" />
                    </svg>
                    该目录为空
                  </td>
                </tr>
              )}
              {entries.map((en) => (
                <tr key={en.path}>
                  <td
                    className="ckun-ssh-mono"
                    style={{ cursor: en.type === 'dir' ? 'pointer' : 'default' }}
                    onDoubleClick={() => {
                      if (en.type === 'dir') void load(en.path)
                    }}
                  >
                    {en.type === 'dir' ? <FolderIcon /> : <FileIcon />}
                    {en.name}
                  </td>
                  <td>{en.type === 'dir' ? '目录' : en.type === 'link' ? '链接' : '文件'}</td>
                  <td>{en.type === 'file' ? fmtSize(en.size) : '—'}</td>
                  <td>{new Date(en.mtime).toLocaleString()}</td>
                  <td>
                    {en.type === 'file' && (
                      <a className="ckun-ssh-btn" href={api.downloadUrl(alias, en.path)} download>
                        下载
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
