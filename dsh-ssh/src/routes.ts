/**
 * The /api/ckun-ssh route family (GUI data path) plus the terminal WebSocket
 * upgrade. The GUI is the only consumer; agent traffic goes through the tools.
 */
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Duplex } from 'node:stream'
import { WebSocketServer } from 'ws'
import type { WebRoute, WebUpgradeRoute } from '@deepseek-ai/dsh-host-webserver'
import type { SshEngine } from './engine.ts'
import type { HostStore } from './store.ts'
import type { HostEntry } from './protocol.ts'

function sendJson(res: ServerResponse, code: number, body: unknown): void {
  const text = JSON.stringify(body)
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  res.end(text)
}

function sendError(res: ServerResponse, code: number, message: string): void {
  sendJson(res, code, { error: message })
}

/**
 * Two ceilings, because the two paths are not the same thing: a JSON config body is
 * never big (an oversized paste should fail fast), while an upload streams a file.
 * Sharing one 64 MiB limit made both of those statements false.
 */
const MAX_JSON_BODY_BYTES = 1024 * 1024
const MAX_BODY_BYTES = 64 * 1024 * 1024

function readBody(req: IncomingMessage, limit = MAX_BODY_BYTES): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    req.on('data', (c: Buffer) => {
      size += c.length
      if (size > limit) {
        // 带 statusCode：各 handler 的 catch 原先一律回 500，
        // 于是「请求体太大」这个客户端错误看起来像服务端坏了。
        // 这里绝不能顺手 req.destroy()：socket 立刻断开，413 就永远发不出去，
        // 客户端只看到一个被对端关掉的连接（本文件的新用例正是这么撞的）。
        // 停止累计就够了，未读的请求体由响应结束后的连接清理统一处理。
        reject(Object.assign(new Error(`body too large (limit ${limit} bytes)`), { statusCode: 413 }))
        return
      }
      chunks.push(c)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

/**
 * Reject `.`/`..` as whole path segments, without the substring test that used to
 * stand in for it: `remote.includes('..')` also rejected legitimate names such as
 * `/data/backup..2024/file` (误拦), while it never looked at the `name` parameter at
 * all (see safeFileName). Reads are deliberately not filtered — browsing upward via
 * /ls is a normal operation for an operator with SSH rights on that host.
 */
function hasTraversalSegment(path: string): boolean {
  return path.split(/[/\\]/).some((seg) => seg === '.' || seg === '..')
}

/** HTTP status an error asks for; only body-size errors carry one today. */
function statusOfError(err: unknown): number {
  return (err as { statusCode?: number }).statusCode ?? 500
}

/**
 * Reduce a caller-supplied filename to a single safe path segment.
 * Used by upload, where the value lands in both a local temp path and the
 * remote target path — a traversal here escapes tmpdir locally and writes
 * outside the requested directory remotely.
 */
function safeFileName(raw: string): string {
  const base = raw.split(/[/\\]/).filter(Boolean).pop() ?? 'upload'
  return base === '.' || base === '..' || base === '' ? 'upload' : base
}

async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const text = await readBody(req, MAX_JSON_BODY_BYTES)
  if (!text) return {}
  const parsed: unknown = JSON.parse(text)
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('expected a JSON object body')
  return parsed as Record<string, unknown>
}

function queryOf(req: IncomingMessage): URLSearchParams {
  return new URL(req.url ?? '/', 'http://localhost').searchParams
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' && v !== '' ? v : fallback
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

/** The route family plus the terminal upgrade. */
export function makeRoutes(engine: SshEngine, store: HostStore): { routes: WebRoute[]; upgrade: WebUpgradeRoute; disposeRoutes: () => void } {
  const routes: WebRoute[] = []

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/hosts',
    handler: async (req, res) => {
      try {
        if (req.method === 'GET') {
          return sendJson(res, 200, { hosts: engine.listHosts(queryOf(req).get('q') ?? undefined) })
        }
        if (req.method === 'POST') {
          const body = await readJsonBody(req)
          const alias = str(body.alias)
          const host = str(body.host)
          if (!alias || !host) return sendError(res, 400, 'alias and host are required')
          const tagsRaw: unknown = body.tags
          const tags = Array.isArray(tagsRaw) ? tagsRaw.filter((t): t is string => typeof t === 'string') : []
          const entry: Omit<HostEntry, 'createdAt' | 'updatedAt'> = {
            alias,
            host,
            port: num(body.port, 22),
            user: str(body.user, 'root'),
            auth: body.auth === 'key' || body.auth === 'agent' ? body.auth : 'password',
            keyPath: body.keyPath === undefined ? undefined : str(body.keyPath) || undefined,
            password: body.password === undefined ? undefined : str(body.password) || undefined,
            passphrase: body.passphrase === undefined ? undefined : str(body.passphrase) || undefined,
            environment: body.environment === undefined ? undefined : str(body.environment) || undefined,
            tags,
            description: body.description === undefined ? undefined : str(body.description) || undefined,
          }
          await store.upsert(entry)
          return sendJson(res, 200, { ok: true, host: engine.listHosts(alias).find((h) => h.alias === alias) })
        }
        if (req.method === 'DELETE') {
          const alias = queryOf(req).get('alias') ?? ''
          if (!alias) return sendError(res, 400, 'alias query param required')
          return sendJson(res, 200, { ok: await store.remove(alias) })
        }
        sendError(res, 405, 'method not allowed')
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/hosts/import',
    handler: async (req, res) => {
      try {
        if (req.method !== 'POST') return sendError(res, 405, 'method not allowed')
        const result = await store.importSshConfig()
        return sendJson(res, 200, result)
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/test',
    handler: async (req, res) => {
      try {
        if (req.method !== 'POST') return sendError(res, 405, 'method not allowed')
        const body = await readJsonBody(req)
        const alias = str(body.alias)
        if (!alias) return sendError(res, 400, 'alias is required')
        const result = await engine.testHost(alias, body.pin === true)
        return sendJson(res, 200, result)
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/exec',
    handler: async (req, res) => {
      try {
        if (req.method !== 'POST') return sendError(res, 405, 'method not allowed')
        const body = await readJsonBody(req)
        const alias = str(body.alias)
        const command = str(body.command)
        if (!alias || !command) return sendError(res, 400, 'alias and command are required')
        // GUI-originated exec: the human in the panel is the operator — no gate.
        const result = await engine.exec(alias, command, num(body.timeoutMs, 60000))
        return sendJson(res, 200, result)
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/ls',
    handler: async (req, res) => {
      try {
        if (req.method !== 'GET') return sendError(res, 405, 'method not allowed')
        const q = queryOf(req)
        const alias = q.get('alias') ?? ''
        const path = q.get('path') ?? '/'
        if (!alias) return sendError(res, 400, 'alias query param required')
        const entries = await engine.ls(alias, path)
        return sendJson(res, 200, { path, entries })
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/download',
    handler: async (req, res) => {
      try {
        if (req.method !== 'GET') return sendError(res, 405, 'method not allowed')
        const q = queryOf(req)
        const alias = q.get('alias') ?? ''
        const remote = q.get('path') ?? ''
        if (!alias || !remote) return sendError(res, 400, 'alias and path query params required')
        if (hasTraversalSegment(remote)) return sendError(res, 400, 'path must not contain "." or ".." segments')
        const local = join(tmpdir(), `ckun-dsh-ssh-${Date.now()}-${randomUUID().slice(0, 8)}`, remote.split('/').filter(Boolean).pop() ?? 'file')
        await engine.download(alias, remote, local)
        res.writeHead(200, {
          'content-type': 'application/octet-stream',
          'content-disposition': `attachment; filename="${encodeURIComponent(remote.split('/').filter(Boolean).pop() ?? 'file')}"`,
        })
        // stream the temp file back, then let the client's next tick clean up
        const { createReadStream } = await import('node:fs')
        const { unlink } = await import('node:fs/promises')
        const rs = createReadStream(local)
        rs.on('error', (e) => sendError(res, 500, e.message))
        rs.pipe(res)
        res.on('close', () => {
          void unlink(local).catch(() => {})
        })
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/upload',
    handler: async (req, res) => {
      try {
        if (req.method !== 'POST') return sendError(res, 405, 'method not allowed')
        const q = queryOf(req)
        const alias = q.get('alias') ?? ''
        const remote = q.get('path') ?? ''
        // `name` 同时进本地临时路径和远端目标路径，两处都按文件名用，所以在这里过一次：
        // 未过滤时 name=..%2F..%2Fwindows%2Fwin.ini 会被解码成 '../../…'，join 之后
        // 写盘落在 tmpdir 之外；同一串又拼进 `${remote}/${name}`，绕过上面那条
        // remote.includes('..') 检查，在 SSH 主机上写出 ../../etc/sudoers。
        // GUI 侧本来只传 basename（client/api.ts），此处收紧不影响正常调用。
        const name = safeFileName(q.get('name') ?? 'upload')
        if (!alias || !remote) return sendError(res, 400, 'alias and path query params required')
        if (hasTraversalSegment(remote)) return sendError(res, 400, 'path must not contain "." or ".." segments')
        const local = join(tmpdir(), `ckun-dsh-ssh-up-${Date.now()}-${randomUUID().slice(0, 8)}`, name)
        const { mkdirSync, writeFileSync } = await import('node:fs')
        const { unlink } = await import('node:fs/promises')
        const body = await readBody(req)
        mkdirSync(dirname(local), { recursive: true })
        writeFileSync(local, body)
        const bytes = await engine.upload(alias, local, `${remote.replace(/\/$/, '')}/${name}`)
        void unlink(local).catch(() => {})
        return sendJson(res, 200, { ok: true, bytes })
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/tunnel',
    handler: async (req, res) => {
      try {
        if (req.method !== 'POST') return sendError(res, 405, 'method not allowed')
        const body = await readJsonBody(req)
        const action = str(body.action)
        if (action === 'list') {
          return sendJson(res, 200, { tunnels: engine.stateSnapshot().tunnels })
        }
        if (action === 'start') {
          const alias = str(body.alias)
          const localPort = num(body.localPort, 0)
          const remotePort = num(body.remotePort, 0)
          if (!alias || !remotePort) return sendError(res, 400, 'alias and remotePort are required')
          const tunnel = await engine.startTunnel(alias, localPort || 0, str(body.remoteHost, '127.0.0.1'), remotePort)
          return sendJson(res, 200, { ok: true, tunnel })
        }
        if (action === 'stop' || action === 'stop-all') {
          if (action === 'stop') {
            const id = str(body.tunnelId)
            const stopped = engine.stopTunnel(id)
            return sendJson(res, 200, { ok: stopped !== undefined, tunnel: stopped })
          }
          engine.stopAllTunnels()
          return sendJson(res, 200, { ok: true })
        }
        sendError(res, 400, 'action must be start | stop | stop-all | list')
      } catch (err) {
        sendError(res, statusOfError(err), (err as Error).message)
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/api/ckun-ssh/state',
    handler: async (_req, res) => {
      sendJson(res, 200, engine.stateSnapshot())
    },
  })

  /**
   * Per-route-set WSS, not a module-level singleton. A settings edit remounts the
   * routes without remounting the module (`index.ts` sync() on `loader/volatile-update`),
   * so a module-scope server (a) is never closed by any dispose path — its live sockets
   * keep piping a remote shell through an engine that was already disposed, and
   * (b) gets re-pointed at a new engine while the old sockets stay on the stale one.
   */
  const terminalWss = new WebSocketServer({ noServer: true })

  const upgrade: WebUpgradeRoute = {
    path: '/api/ckun-ssh/terminal',
    handler: (req, socket, head) => {
      const alias = queryOf(req).get('alias') ?? ''
      if (!alias) {
        socket.destroy()
        return
      }
      terminalWss.handleUpgrade(req, socket as Duplex, head, (ws) => {
        void serveTerminal(ws, engine, alias)
      })
    },
  }

  /** Tear down the terminal sockets with the route set that owns them. */
  const disposeRoutes = (): void => {
    for (const ws of terminalWss.clients) {
      try {
        ws.terminate()
      } catch {
        /* already gone */
      }
    }
    terminalWss.close()
  }

  return { routes, upgrade, disposeRoutes }
}

import type { WebSocket } from 'ws'

/** 终端输出的背压水位（高/低）：越过高位压住远端通道，回落到低位再放。 */
const TERMINAL_BUFFER_HIGH = 4 * 1024 * 1024
const TERMINAL_BUFFER_LOW = 1 * 1024 * 1024

async function serveTerminal(ws: WebSocket, engine: SshEngine, alias: string): Promise<void> {
  const send = (frame: unknown) => {
    if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(frame))
  }
  let stream: import('ssh2').ClientChannel | undefined
  try {
    // Fail closed: the GUI terminal also requires a pinned host key.
    const client = await engine.connect(alias, false)
    await new Promise<void>((resolve) => {
      client.shell({ term: 'xterm-256color', cols: 80, rows: 24 }, {}, (err, ch) => {
        if (err) {
          send({ type: 'exit', code: null, error: err.message })
          ws.close()
          resolve()
          return
        }
        stream = ch
        send({ type: 'ready', sessionId: randomUUID().slice(0, 8), alias })
        // 背压要还给 SSH 侧：一个不再读取的标签页（隐藏、卡顿、被挂起）会让 ws 的
        // 发送缓冲按远端输出速率无上限增长，而远端可以是 `yes` 这种每秒几百 MB 的源。
        // 远程字节把宿主内存吃光不是终端面板该有的失败方式。
        ch.on('data', (d: Buffer) => {
          send({ type: 'output', data: d.toString('utf8') })
          if (ws.bufferedAmount > TERMINAL_BUFFER_HIGH) ch.pause()
        })
        ws.on('drain', () => {
          if (ws.bufferedAmount < TERMINAL_BUFFER_LOW) ch.resume()
        })
        ch.on('close', (code: number | null) => {
          send({ type: 'exit', code })
          ws.close()
          resolve()
        })
        resolve()
      })
    })
  } catch (err) {
    send({ type: 'exit', code: null, error: (err as Error).message })
    ws.close()
    return
  }
  ws.on('message', (data) => {
    let frame: { type?: string; data?: string; cols?: number; rows?: number }
    try {
      frame = JSON.parse(data.toString('utf8'))
    } catch {
      return
    }
    if (!stream) return
    if (frame.type === 'input' && typeof frame.data === 'string') stream.write(frame.data)
    else if (frame.type === 'resize' && typeof frame.cols === 'number' && typeof frame.rows === 'number') {
      // ssh2 client channels expose setWindow(rows, cols, height, width)
      try {
        const chan = stream as unknown as { setWindow?: (rows: number, cols: number, height: number, width: number) => void }
        chan.setWindow?.(Math.max(1, frame.rows), Math.max(2, frame.cols), 0, 0)
      } catch {
        /* ignore */
      }
    } else if (frame.type === 'close') {
      try {
        stream.end()
      } catch {
        /* ignore */
      }
      ws.close()
    }
  })
  ws.on('close', () => {
    try {
      stream?.end()
    } catch {
      /* ignore */
    }
  })
}
