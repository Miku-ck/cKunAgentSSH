// API integration test: real HTTP server dispatching the /api/ckun-ssh route
// family (plus the terminal WebSocket upgrade) against an in-process ssh2
// Server. Verifies the full GUI data path: hosts CRUD, TOFU pin, exec, state,
// tunnels, and the interactive terminal (ready/input/resize/exit frames).
// SFTP endpoints (ls/download/upload) need a real SFTP subsystem, covered in
// on-host E2E after restart.
import { generateKeyPairSync } from 'node:crypto'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import net from 'node:net'

const fakeHome = mkdtempSync(join(tmpdir(), 'ckun-ssh-routes-'))
process.env.DSH_HOME = fakeHome

const ssh2mod = await import('ssh2')
const { Server } = ssh2mod.default ?? ssh2mod
const WebSocket = (await import('ws')).default
const { HostStore } = await import('../src/store.ts')
const { SshEngine } = await import('../src/engine.ts')
const { createCredentialVault } = await import('../src/vault.ts')
const { makeRoutes } = await import('../src/routes.ts')

let pass = 0
let fail = 0
const check = (name, cond, extra = '') => {
  if (cond) {
    pass++
    console.log(`PASS  ${name}`)
  } else {
    fail++
    console.log(`FAIL  ${name} ${extra}`)
  }
}

const makeKey = () => generateKeyPairSync('rsa', {
  modulusLength: 2048,
  publicKeyEncoding: { type: 'spki', format: 'pem' },
  privateKeyEncoding: { type: 'pkcs1', format: 'pem' },
})
const keyA = makeKey()

const state = { lastWindow: null }

// in-process SSH server: authentication always accepts, exec + shell echo
const ssh = new Server({ hostKeys: [{ key: keyA.privateKey }] }, (client) => {
  client.on('error', () => {})
  client.on('authentication', (ctx) => ctx.accept())
  client.on('tcpip', (accept, reject, info) => {
    const stream = accept()
    const sock = net.connect(state.echoPort, '127.0.0.1', () => stream.pipe(sock).pipe(stream))
    sock.on('error', () => stream.close())
    stream.on('error', () => sock.destroy())
  })
  client.on('session', (accept) => {
    const session = accept()
    session.on('pty', (a) => a())
    session.on('window-change', (accept2, reject2, size) => {
      state.lastWindow = [size.rows, size.cols]
      if (typeof accept2 === 'function') accept2()
    })
    session.on('exec', (accept2, reject2, info) => {
      const stream = accept2()
      if (info.command === 'echo:hello') {
        stream.write('hello\n')
        stream.exit(0)
      } else {
        stream.exit(127)
      }
      stream.close()
    })
    session.on('shell', (accept2) => {
      const stream = accept2()
      stream.on('data', (d) => stream.write(`echo:${d}`))
      stream.on('close', () => stream.close())
    })
  })
})
await new Promise((resolve, reject) => {
  ssh.once('error', reject)
  ssh.listen(0, '127.0.0.1', () => {
    ssh.removeListener('error', reject)
    resolve()
  })
})
const sshPort = ssh.address().port

const echo = net.createServer((s) => s.pipe(s))
await new Promise((r) => echo.listen(0, '127.0.0.1', r))
state.echoPort = echo.address().port

// vault-backed store (secrets never touch the JSON file)
const records = new Map()
const creds = {
  readRecord: async (k) => records.get(k),
  modifyRecord: async (k, mutate) => {
    const next = await mutate(records.get(k))
    if (next === undefined) records.delete(k)
    else records.set(k, next)
    return records.get(k)
  },
  deleteRecord: async (k) => {
    records.delete(k)
  },
}
const store = new HostStore(createCredentialVault(creds))
const engine = new SshEngine(store, { maxOutputBytes: () => 65536, keepaliveMs: () => 10000 })

// HTTP dispatch over the plugin's WebRoute list + WS upgrade
const { routes, upgrade } = makeRoutes(engine, store)
const byPath = new Map(routes.map((r) => [r.path, r.handler]))
const server = http.createServer((req, res) => {
  const handler = byPath.get(new URL(req.url, 'http://x').pathname)
  if (!handler) {
    res.writeHead(404, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ error: 'no route' }))
    return
  }
  handler(req, res)
})
server.on('upgrade', (req, socket, head) => {
  const u = new URL(req.url, 'http://x')
  if (u.pathname === upgrade.path) upgrade.handler(req, socket, head)
  else socket.destroy()
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const base = `http://127.0.0.1:${server.address().port}`

const post = (path, body) => fetch(base + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }).then(async (r) => ({ status: r.status, body: await r.json() }))
const get = (path) => fetch(base + path).then(async (r) => ({ status: r.status, body: await r.json() }))

try {
  // hosts CRUD
  const created = await post('/api/ckun-ssh/hosts', { alias: 'web', host: '127.0.0.1', port: sshPort, user: 'u', auth: 'password', password: 'pw', environment: 'dev', tags: ['test'] })
  check('POST /hosts creates', created.status === 200 && created.body.ok === true, JSON.stringify(created.body))
  check('secret went to the credential record', records.has('ckun-ssh/web'), [...records.keys()].join(','))

  const listed = await get('/api/ckun-ssh/hosts')
  const webEntry = listed.body.hosts.find((h) => h.alias === 'web')
  check('GET /hosts lists without leaking secrets', listed.status === 200 && webEntry && webEntry.pinned === false && !JSON.stringify(webEntry).includes('pw'), JSON.stringify(webEntry))

  // TOFU: unpinned test -> fingerprint; pin -> ok
  const t1 = await post('/api/ckun-ssh/test', { alias: 'web' })
  check('POST /test unpinned offers fingerprint', t1.body.ok === false && t1.body.needsPin === true && /^SHA256:/.test(t1.body.fingerprint ?? ''), JSON.stringify(t1.body))
  const t2 = await post('/api/ckun-ssh/test', { alias: 'web', pin: true })
  check('POST /test pin connects + profile', t2.status === 200 && t2.body.ok === true && /^SHA256:/.test(t2.body.fingerprint ?? ''), JSON.stringify(t2.body))

  const listed2 = await get('/api/ckun-ssh/hosts')
  check('host now pinned in list', listed2.body.hosts.find((h) => h.alias === 'web')?.pinned === true)

  // exec through the GUI path
  const r1 = await post('/api/ckun-ssh/exec', { alias: 'web', command: 'echo:hello' })
  check('POST /exec succeeds', r1.status === 200 && r1.body.success === true && r1.body.stdout.includes('hello'), JSON.stringify(r1.body))

  const st = await get('/api/ckun-ssh/state')
  check('GET /state shows open connection', st.body.connections?.web === 'open', JSON.stringify(st.body))

  // tunnels through the API
  const ts = await post('/api/ckun-ssh/tunnel', { action: 'start', alias: 'web', remoteHost: '127.0.0.1', remotePort: state.echoPort })
  check('tunnel start via API', ts.body.ok === true && ts.body.tunnel?.localPort > 0, JSON.stringify(ts.body))
  const tdata = await new Promise((resolve, reject) => {
    const s = net.connect(ts.body.tunnel.localPort, '127.0.0.1', () => s.write('ping'))
    let got = ''
    s.on('data', (d) => {
      got += d
      if (got.includes('ping')) {
        s.end()
        resolve(got)
      }
    })
    s.on('error', reject)
    setTimeout(() => reject(new Error('tunnel echo timeout')), 4000)
  })
  check('tunnel echoes through SSH', tdata.includes('ping'))
  const tstop = await post('/api/ckun-ssh/tunnel', { action: 'stop', tunnelId: ts.body.tunnel.id })
  check('tunnel stop via API', tstop.body.ok === true, JSON.stringify(tstop.body))

  // terminal WebSocket: ready -> input -> output -> resize -> close
  const ws = new WebSocket(`ws://127.0.0.1:${server.address().port}/api/ckun-ssh/terminal?alias=web`)
  const frames = []
  let frameListener
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`terminal ready timeout: ${JSON.stringify(frames)}`)), 8000)
    frameListener = (d) => {
      const f = JSON.parse(d.toString())
      frames.push(f)
      // ready means the shell is up; exit means it failed — stop waiting either way
      if (f.type === 'ready' || f.type === 'exit') {
        ws.off('message', frameListener)
        clearTimeout(timer)
        resolve()
      }
    }
    ws.on('message', frameListener)
    ws.on('error', reject)
  })
  const readyFrame = frames.find((f) => f.type === 'ready')
  check('terminal ready frame', readyFrame !== undefined && readyFrame.alias === 'web', JSON.stringify(frames))
  ws.send(JSON.stringify({ type: 'input', data: 'hi' }))
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`terminal output timeout: ${JSON.stringify(frames)}`)), 8000)
    const onMsg = (d) => {
      const f = JSON.parse(d.toString())
      frames.push(f)
      if (f.type === 'output' && f.data.includes('echo:hi')) {
        ws.off('message', onMsg)
        clearTimeout(timer)
        resolve()
      }
    }
    ws.on('message', onMsg)
  })
  check('terminal input echoed', true)
  ws.send(JSON.stringify({ type: 'resize', cols: 120, rows: 40 }))
  await new Promise((r) => setTimeout(r, 300))
  check('terminal resize reached server (40,120)', Array.isArray(state.lastWindow) && state.lastWindow[0] === 40 && state.lastWindow[1] === 120, JSON.stringify(state.lastWindow))
  ws.send(JSON.stringify({ type: 'close' }))
  ws.close()

  // delete host (removes base + secret record)
  const del = await fetch(`${base}/api/ckun-ssh/hosts?alias=web`, { method: 'DELETE' }).then(async (r) => r.json())
  check('DELETE /hosts removes', del.ok === true, JSON.stringify(del))
  const listed3 = await get('/api/ckun-ssh/hosts')
  check('host gone from list', listed3.body.hosts.every((h) => h.alias !== 'web'))
  check('secret record deleted with host', records.has('ckun-ssh/web') === false)

  engine.dispose()
} finally {
  server.close()
  echo.close()
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
