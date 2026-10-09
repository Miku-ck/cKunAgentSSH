// Regression: apply() must not read service properties it did not declare in
// `inject`. Cordis contexts are proxies — reading a non-injected service
// property (ctx.credentials) throws "cannot get property ... without inject"
// even when the service is provided, which used to kill the whole plugin mount
// on every boot (GUI: "SSH 插件未启用或主机未响应"). The plugin must go through
// ctx.get() for the optional credentials service instead.
import { EventEmitter } from 'node:events'
import { existsSync, mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

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

const fakeHome = mkdtempSync(join(tmpdir(), 'ckun-ssh-apply-'))

// --- a cordis-shaped context: proxy semantics for service properties --------
const INJECTED = ['webServer', 'tools', 'systemPrompt']

function makeProxyCtx(provided) {
  const state = { routes: [], upgrades: [], tools: [], sections: [], listeners: [] }
  const target = {
    on: (evt, _fn, _opts) => {
      state.listeners.push(evt)
      return () => {}
    },
    effect: (_fn, label) => state.effects ??= label,
    get: (name) => (name in provided ? provided[name] : undefined),
  }
  return {
    state,
    ctx: new Proxy(target, {
      get(t, prop) {
        if (typeof prop === 'symbol') return
        if (prop in t) return t[prop]
        if (INJECTED.includes(prop)) {
          if (!(prop in provided)) throw new Error(`cannot get required service "${prop}" in inactive context`)
          return provided[prop]
        }
        throw new Error(`cannot get property "${prop}" without inject`)
      },
    }),
  }
}

const mockServices = (withCredentials) => {
  const services = {
    webServer: {
      register: (route) => () => {},
      registerUpgrade: () => () => {},
    },
    tools: { register: () => () => {} },
    systemPrompt: { section: () => () => {} },
  }
  if (withCredentials) {
    const records = new Map()
    services.credentials = {
      readRecord: async (key) => records.get(key),
      modifyRecord: async (key, mutate) => {
        const next = await mutate(records.get(key))
        if (next === undefined) records.delete(key)
        else records.set(key, next)
        return records.get(key)
      },
      deleteRecord: async (key) => records.delete(key),
      records,
    }
  }
  return services
}

// capture the real registrations by replacing the service methods
function instrument(services) {
  const out = { routes: [], upgrades: 0, tools: [], sections: [] }
  services.webServer.register = (route) => {
    out.routes.push(route)
    return () => {}
  }
  services.webServer.registerUpgrade = () => {
    out.upgrades++
    return () => {}
  }
  services.tools.register = (tool) => {
    out.tools.push(tool)
    return () => {}
  }
  services.systemPrompt.section = (s) => {
    out.sections.push(s)
    return () => {}
  }
  return out
}

// fake HTTP pieces for driving one route handler
function jsonReq(payload) {
  const req = new EventEmitter()
  req.method = 'POST'
  req.url = '/api/ckun-ssh/hosts'
  queueMicrotask(() => {
    req.emit('data', Buffer.from(JSON.stringify(payload)))
    req.emit('end')
  })
  return req
}
const fakeRes = () => {
  const res = { statusCode: 0, headers: {}, body: '' }
  res.writeHead = (code, headers) => {
    res.statusCode = code
    Object.assign(res.headers, headers ?? {})
  }
  res.end = (text) => {
    res.body = text ?? ''
  }
  return res
}

const plugin = await import('../src/index.ts')

// sanity: the proxy really throws on a non-injected service property (the old
// code path). If this ever changes, this test must be rethought.
{
  const { ctx } = makeProxyCtx(mockServices(true))
  let threw = false
  try {
    void ctx.credentials
  } catch {
    threw = true
  }
  check('proxy ctx throws on ctx.credentials read', threw)
}

// --- case A: credentials service provided → vault must route through it -----
process.env.DSH_HOME = join(fakeHome, 'with-creds')
{
  const services = mockServices(true)
  const reg = instrument(services)
  const { ctx } = makeProxyCtx(services)

  let applyError
  try {
    plugin.apply(ctx, {})
  } catch (err) {
    applyError = err
  }
  check('apply() survives with credentials service present', applyError === undefined, applyError?.message)
  check(
    'all /api/ckun-ssh routes registered',
    reg.routes.length === 9 && reg.routes.every((r) => r.path.startsWith('/api/ckun-ssh/')),
    JSON.stringify(reg.routes.map((r) => r.path)),
  )
  check('terminal upgrade registered', reg.upgrades === 1)
  check('ssh tools registered', JSON.stringify(reg.tools.map((t) => t.name).sort()) === JSON.stringify(['ssh_connect', 'ssh_exec', 'ssh_list']))
  check('system prompt section registered', reg.sections.length === 1 && reg.sections[0].name === 'ckun-ssh')

  // drive POST /api/ckun-ssh/hosts end to end: the secret must land in the
  // credential record, and the JSON registry must stay secret-free
  const hosts = reg.routes.find((r) => r.path === '/api/ckun-ssh/hosts')
  const res = fakeRes()
  await hosts.handler(jsonReq({ alias: 'prod-a', host: '10.0.0.5', password: 's3cret' }), res)
  const body = JSON.parse(res.body)
  check('POST /hosts ok', res.statusCode === 200 && body.ok === true, res.body)
  const grants = [...services.credentials.records.entries()].filter(([k]) => k.startsWith('ckun-ssh/'))
  check('secret stored in credential record', grants.length === 1 && grants[0][1].payload?.password === 's3cret', JSON.stringify(grants))
  const file = join(process.env.DSH_HOME, 'ckun-dsh-ssh.json')
  check('JSON registry carries no secret', existsSync(file) && !readFileSync(file, 'utf8').includes('s3cret'))
}

// --- case B: no credentials service → graceful JSON fallback ----------------
process.env.DSH_HOME = join(fakeHome, 'no-creds')
{
  const services = mockServices(false)
  const reg = instrument(services)
  const { ctx } = makeProxyCtx(services)

  let applyError
  try {
    plugin.apply(ctx, {})
  } catch (err) {
    applyError = err
  }
  check('apply() survives without credentials service', applyError === undefined, applyError?.message)

  const hosts = reg.routes.find((r) => r.path === '/api/ckun-ssh/hosts')
  const res = fakeRes()
  await hosts.handler(jsonReq({ alias: 'dev-b', host: '10.0.0.6', password: 'pw-fallback' }), res)
  const file = join(process.env.DSH_HOME, 'ckun-dsh-ssh.json')
  check('fallback stores secret in 0600 JSON', existsSync(file) && readFileSync(file, 'utf8').includes('pw-fallback'))
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
