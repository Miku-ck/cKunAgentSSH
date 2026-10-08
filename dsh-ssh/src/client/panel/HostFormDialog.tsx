/**
 * Host create/edit dialog with the TOFU pin flow:
 *   测试连接 → (key not pinned yet) → show SHA256 fingerprint → 信任并固定 → 连接成功.
 * The fingerprint is the only way a host key becomes trusted (agent connects
 * are fail-closed until this human step is done).
 */
import { useState } from 'react'
import type { SshApi } from '../api.ts'
import type { HostSummary } from '../../protocol.ts'

interface HostFormDialogProps {
  api: SshApi
  existing?: HostSummary
  onClose(): void
  onSaved(): void
}

interface FormState {
  alias: string
  host: string
  port: number
  user: string
  auth: 'password' | 'key' | 'agent'
  keyPath: string
  password: string
  passphrase: string
  environment: string
  tags: string
  description: string
}

/** The host form modal. */
export function HostFormDialog({ api, existing, onClose, onSaved }: HostFormDialogProps): React.JSX.Element {
  const [form, setForm] = useState<FormState>({
    alias: existing?.alias ?? '',
    host: existing?.host ?? '',
    port: existing?.port ?? 22,
    user: existing?.user ?? 'root',
    auth: existing?.auth ?? 'key',
    keyPath: existing?.keyPath ?? '',
    password: '',
    passphrase: '',
    environment: existing?.environment ?? '',
    tags: (existing?.tags ?? []).join(', '),
    description: existing?.description ?? '',
  })
  const [testing, setTesting] = useState(false)
  const [pinOffer, setPinOffer] = useState<{ fingerprint: string } | null>(null)
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)

  const set = (key: keyof FormState) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [key]: event.target.value }))

  const persist = () =>
    api.saveHost({
      alias: form.alias.trim(),
      host: form.host.trim(),
      port: Number(form.port) || 22,
      user: form.user.trim() || 'root',
      auth: form.auth,
      keyPath: form.keyPath.trim() || undefined,
      password: form.password === '' ? undefined : form.password,
      passphrase: form.passphrase === '' ? undefined : form.passphrase,
      environment: form.environment.trim() || undefined,
      tags: form.tags.split(',').map((s) => s.trim()).filter(Boolean),
      description: form.description.trim() || undefined,
    })

  /** Test (persisting first so the store knows the host), then offer the pin step. */
  const doTest = async (pin: boolean) => {
    if (form.alias.trim() === '' || form.host.trim() === '') {
      setNote({ ok: false, text: '请先填写别名和地址' })
      return
    }
    setTesting(true)
    setNote(null)
    setPinOffer(null)
    try {
      await persist()
      const result = await api.testHost(form.alias.trim(), pin)
      if (result.ok) {
        const profile = result.profile
        setNote({
          ok: true,
          text:
            `连接成功` +
            (profile?.distro ? ` · ${profile.distro}` : '') +
            (profile?.pkgManager ? ` · 包管理 ${profile.pkgManager}` : '') +
            (result.fingerprint ? ` · ${result.fingerprint}` : ''),
        })
      } else if (result.needsPin && result.fingerprint !== undefined) {
        setPinOffer({ fingerprint: result.fingerprint })
      } else {
        setNote({ ok: false, text: result.error ?? '连接失败' })
      }
    } catch (err) {
      setNote({ ok: false, text: (err as Error).message })
    } finally {
      setTesting(false)
    }
  }

  const save = async () => {
    try {
      await persist()
      onSaved()
    } catch (err) {
      setNote({ ok: false, text: (err as Error).message })
    }
  }

  const remove = async () => {
    if (existing === undefined) return
    if (!window.confirm(`删除主机 ${existing.alias}？（连接池会断开，主机密钥固定记录保留）`)) return
    try {
      await api.deleteHost(existing.alias)
      onSaved()
    } catch (err) {
      setNote({ ok: false, text: (err as Error).message })
    }
  }

  return (
    <div className="ckun-ssh-modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="ckun-ssh-modal">
        <h3>{existing !== undefined ? `编辑主机 · ${existing.alias}` : '新增 SSH 主机'}</h3>
        <div className="ckun-ssh-form-row">
          <label>别名 *</label>
          <input className="ckun-ssh-input" value={form.alias} onChange={set('alias')} placeholder="prod-web-01" disabled={existing !== undefined} />
        </div>
        <div className="ckun-ssh-form-row">
          <label>地址 *</label>
          <input className="ckun-ssh-input" value={form.host} onChange={set('host')} placeholder="192.168.1.10 或 host.example.com" />
        </div>
        <div className="ckun-ssh-form-row">
          <label>端口</label>
          <input className="ckun-ssh-input" style={{ maxWidth: 100 }} value={String(form.port)} onChange={set('port')} />
          <label>用户</label>
          <input className="ckun-ssh-input" value={form.user} onChange={set('user')} />
        </div>
        <div className="ckun-ssh-form-row">
          <label>认证方式</label>
          <select className="ckun-ssh-select" value={form.auth} onChange={set('auth')}>
            <option value="key">密钥 (key)</option>
            <option value="password">密码 (password)</option>
            <option value="agent">系统 ssh-agent</option>
          </select>
        </div>
        {form.auth === 'key' && (
          <>
            <div className="ckun-ssh-form-row">
              <label>私钥路径</label>
              <input className="ckun-ssh-input ckun-ssh-mono" value={form.keyPath} onChange={set('keyPath')} placeholder="~/.ssh/id_ed25519" />
            </div>
            <div className="ckun-ssh-form-row">
              <label>私钥口令</label>
              <input className="ckun-ssh-input" type="password" value={form.passphrase} onChange={set('passphrase')} placeholder={existing !== undefined ? '留空 = 保持不变' : ''} />
            </div>
          </>
        )}
        {form.auth === 'password' && (
          <div className="ckun-ssh-form-row">
            <label>密码</label>
            <input className="ckun-ssh-input" type="password" value={form.password} onChange={set('password')} placeholder={existing !== undefined ? '留空 = 保持不变' : ''} />
          </div>
        )}
        <div className="ckun-ssh-form-row">
          <label>环境</label>
          <select className="ckun-ssh-select" value={form.environment} onChange={set('environment')}>
            <option value="">未指定</option>
            <option value="dev">dev</option>
            <option value="staging">staging</option>
            <option value="prod">prod（生产：agent 禁变更）</option>
          </select>
          <label>标签</label>
          <input className="ckun-ssh-input" value={form.tags} onChange={set('tags')} placeholder="gpu, k8s（逗号分隔）" />
        </div>
        <div className="ckun-ssh-form-row">
          <label>描述</label>
          <input className="ckun-ssh-input" value={form.description} onChange={set('description')} />
        </div>
        {pinOffer !== null && (
          <div>
            <div className="ckun-ssh-note">
              首次连接该主机（主机密钥未固定）。核对下方 SHA256 指纹（可在目标机器上执行
              <span className="ckun-ssh-mono"> ssh-keygen -lf &lt;host key&gt;</span> 或在同网段机器上用
              <span className="ckun-ssh-mono"> ssh -o BatchMode=yes</span> 交叉验证）后信任：
            </div>
            <div className="ckun-ssh-fingerprint">{pinOffer.fingerprint}</div>
            <div className="ckun-ssh-form-row">
              <button className="ckun-ssh-btn primary" disabled={testing} onClick={() => void doTest(true)}>
                ✓ 信任并固定指纹
              </button>
              <button className="ckun-ssh-btn" disabled={testing} onClick={onClose}>
                取消（保持未固定）
              </button>
            </div>
          </div>
        )}
        {note !== null && <div className={note.ok ? 'ckun-ssh-ok' : 'ckun-ssh-error'}>{note.text}</div>}
        <div className="ckun-ssh-modal-foot">
          {existing !== undefined && (
            <button className="ckun-ssh-btn danger" style={{ marginRight: 'auto' }} onClick={() => void remove()}>
              删除
            </button>
          )}
          <button className="ckun-ssh-btn" disabled={testing} onClick={() => void doTest(false)}>
            {testing ? '测试中…' : '测试连接'}
          </button>
          <button className="ckun-ssh-btn" onClick={onClose}>
            取消
          </button>
          <button className="ckun-ssh-btn primary" onClick={() => void save()}>
            保存
          </button>
        </div>
      </div>
    </div>
  )
}
