// Engine integration test against an in-process ssh2 Server.
// Verifies: fail-closed unpinned hosts, TOFU pinning, exec, shell + resize,
// local port-forward tunnels, host-key mismatch (MITM) rejection, pool state.
import { generateKeyPairSync } from 'node:crypto'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import net from 'node:net'

// Isolate the plugin data (host store + known hosts) in a temp DSH home.
const fakeHome = mkdtempSync(join(tmpdir(), 'ckun-ssh-test-'))
process.env.DSH_HOME = fakeHome

const ssh2mod = await import('ssh2')
const { Server } = ssh2mod.default ?? ssh2mod
const { HostStore } = await import('../src/store.ts')
const { SshEngine } = await import('../src/engine.ts')

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

// ssh2's key parser accepts PKCS#1 ("BEGIN RSA PRIVATE KEY") and OpenSSH
// formats, but not the PKCS#8 that node emits by default — so request pkcs1.
const makeKey = () => generateKeyPairSync('rsa', {
  modulusLength: 2048,
  publicKeyEncoding: { type: 'spki', format: 'pem' },
  privateKeyEncoding: { type: 'pkcs1', format: 'pem' },
})
const keyA = makeKey()
const keyB = makeKey()

/** Spin up one in-process SSH server with the given host key (optional fixed port). */
async function startServer(privPem, state, listenOn = 0) {
  const server = new Server({ hostKeys: [{ key: privPem }] }, (client) => {
    // expected: refused handshakes (TOFU capture) surface as server-side errors
    client.on('error', () => {})
    client.on('authentication', (ctx) => ctx.accept())
    // NOTE: ssh2 Server emits 'tcpip' (not 'tcp connection') for direct-tcpip,
    // with the callback signature (accept, reject, info)
    client.on('tcpip', (accept, reject, info) => {
      const stream = accept()
      // pipe through a tiny local echo server (stands in for the "remote" service)
      const sock = net.connect(state.echoPort, '127.0.0.1', () => {
        stream.pipe(sock).pipe(stream)
      })
      sock.on('error', () => stream.close())
      stream.on('error', () => sock.destroy())
    })
    client.on('session', (accept) => {
      const session = accept()
      session.on('pty', (a) => a())
      session.on('window-change', (accept2, reject2, size) => {
        state.lastWindow = [size.rows, size.cols]
        // window-change is fire-and-forget (client sends want_reply=0), so
        // accept2 is often undefined — never call it unguarded.
        if (typeof accept2 === 'function') accept2()
      })
      session.on('exec', (accept2, reject2, info) => {
        const stream = accept2()
        if (info.command === 'echo:hello') {
          stream.write('hello\n')
          stream.exit(0)
          stream.close()
        } else if (info.command === 'boom') {
          stream.stderr.write('nope\n')
          stream.exit(3)
          stream.close()
        } else {
          stream.exit(127)
          stream.close()
        }
      })
      session.on('shell', (accept2) => {
        const stream = accept2()
        stream.on('data', (d) => stream.write(`echo:${d}`))
        stream.on('close', () => stream.close())
      })
    })
  })
  // bind errors must reject the promise; post-bind errors are handled per-connection
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(listenOn, '127.0.0.1', () => {
      server.removeListener('error', reject)
      resolve()
    })
  })
  return server
}

// tiny echo service the server-side tunnel pipes connect to
const echo = net.createServer((s) => s.pipe(s))
await new Promise((r) => echo.listen(0, '127.0.0.1', r))
const state = { echoPort: echo.address().port, lastWindow: null }

// In-memory credentials stand-in: the store keeps secrets in a vault record
// (like ctx.credentials would), never in the JSON file.
const records = new Map()
const mockCredentials = {
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
const { createCredentialVault } = await import('../src/vault.ts')

const store = new HostStore(createCredentialVault(mockCredentials))
await store.upsert({
  alias: 'test',
  host: '127.0.0.1',
  port: 0, // patched after the server is up
  user: 'u',
  auth: 'password',
  password: 'x',
  environment: 'dev',
  tags: [],
})

const server1 = await startServer(keyA.privateKey, state)
const port = server1.address().port
await store.upsert({ alias: 'test', host: '127.0.0.1', port, user: 'u', auth: 'password', password: 'x', environment: 'dev', tags: [] })

// the auth VALUE is the string "password", so match the JSON KEYS, not the substring
const jsonStore = JSON.stringify(store.get('test'))
check('secret lives in the credential record, not the JSON store', records.has('ckun-ssh/test') && !/"password":|"passphrase":/.test(jsonStore), jsonStore)

const engine = new SshEngine(store, { maxOutputBytes: () => 65536, keepaliveMs: () => 10000 })

try {
  // 1. fail closed: unpinned host + agent-style connect
  let refused = null
  try {
    await engine.connect('test', false)
  } catch (e) {
    refused = e.message
  }
  check('unpinned host refused (fail closed)', /not pinned/i.test(refused ?? ''), String(refused))

  // 2. TOFU pin via GUI-style test
  const t1 = await engine.testHost('test', false)
  check('test without pin reports needsPin + fingerprint', t1.ok === false && t1.needsPin === true && /^SHA256:/.test(t1.fingerprint ?? ''), JSON.stringify(t1))

  const t2 = await engine.testHost('test', true)
  check('test with pin connects + profile', t2.ok === true && t2.profile !== undefined && /^SHA256:/.test(t2.fingerprint ?? ''), JSON.stringify(t2))

  const kh = readFileSync(join(fakeHome, 'ckun-dsh-ssh-knownhosts'), 'utf8')
  check('known-hosts file written with pin', kh.includes('[127.0.0.1]:') && kh.split(' ').length >= 2, kh.slice(0, 60))

  // 3. pooled connect now allowed without TOFU
  const client = await engine.connect('test', false)
  check('second connect reuses pool (same client)', (await engine.connect('test', false)) === client)

  // 4. exec
  const r1 = await engine.exec('test', 'echo:hello')
  check('exec success + stdout + exit 0', r1.success === true && r1.exitCode === 0 && r1.stdout.includes('hello'), JSON.stringify(r1))
  const r2 = await engine.exec('test', 'boom')
  check('exec failure surfaces stderr + exit 3', r2.success === false && r2.exitCode === 3 && r2.stderr.includes('nope'), JSON.stringify(r2))

  // 5. abort handling
  const ac = new AbortController()
  const abortP = engine.exec('test', 'echo:hello', 5000, ac.signal)
  ac.abort()
  const r3 = await abortP
  check('aborted exec reports cancelled', r3.success === false && /cancel/i.test(r3.error ?? ''), JSON.stringify(r3))

  // 6. shell + resize (fresh pool fetch — never reuse a stale reference)
  const shellClient = await engine.connect('test', false)
  await new Promise((resolve, reject) => {
    shellClient.shell({ term: 'xterm-256color', cols: 100, rows: 30 }, {}, (err, ch) => {
      if (err) return reject(err)
      ch.on('data', (d) => {
        if (String(d).includes('echo:hi')) {
          // server echoed; now resize and finish
          const chan = ch
          chan.setWindow(40, 120, 0, 0)
          setTimeout(() => {
            ch.end()
            resolve()
          }, 150)
        }
      })
      ch.on('error', reject)
      ch.write('hi')
    })
  })
  check('shell echo round-trip', true)
  check('window-change received by server (40,120)', Array.isArray(state.lastWindow) && state.lastWindow[0] === 40 && state.lastWindow[1] === 120, JSON.stringify(state.lastWindow))

  // 7. tunnel: local port -> forwardOut -> server -> echo service
  const tunnel = await engine.startTunnel('test', 0, '127.0.0.1', state.echoPort)
  check('tunnel bound a local port', typeof tunnel.localPort === 'number' && tunnel.localPort > 0, JSON.stringify(tunnel))
  const tdata = await new Promise((resolve, reject) => {
    const s = net.connect(tunnel.localPort, '127.0.0.1', () => s.write('ping'))
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
  check('tunnel end-to-end echo through SSH', tdata.includes('ping'), JSON.stringify(tdata))
  engine.stopTunnel(tunnel.id)
  const afterStop = engine.stateSnapshot().tunnels.find((t) => t.id === tunnel.id)
  check('tunnel removed after stop', afterStop === undefined)

  // 8. pool state
  const snap = engine.stateSnapshot()
  check('pool shows test open', snap.connections.test === 'open', JSON.stringify(snap.connections))
  const hosts = engine.listHosts()
  check('listHosts shows pinned + connected', hosts[0].pinned === true && hosts[0].connected === true && hosts[0].keyPath === undefined)

  // 9. MITM: same host:port, different host key -> must be refused.
  // Drop the pooled connection so the next connect() re-handshakes (and
  // re-verifies the host key) against the new server.
  engine.disconnect('test')
  // close() without a callback stops the listener immediately; re-bind the
  // same port and retry while stale sockets linger.
  server1.close()
  let server2
  for (let attempt = 0; ; attempt++) {
    try {
      server2 = await startServer(keyB.privateKey, state, port)
      break
    } catch (e) {
      if (attempt >= 25 || !/EADDRINUSE/.test(String(e?.code ?? e))) throw e
      await new Promise((r) => setTimeout(r, 100))
    }
  }
  let mitm = null
  try {
    await engine.connect('test', false)
  } catch (e) {
    mitm = e.message
  }
  check('host key mismatch refused (MITM guard)', /verification failed|host key rejected/i.test(mitm ?? ''), String(mitm))
  const t3 = await engine.testHost('test', false)
  check('testHost surfaces mismatch + offers re-pin fingerprint', t3.ok === false && t3.needsPin === true && /^SHA256:/.test(t3.fingerprint ?? ''), JSON.stringify(t3))

  engine.dispose()
} finally {
  echo.close()
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
