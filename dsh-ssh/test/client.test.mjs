// Client-half smoke test: evaluate the REAL built client bundle in a fake
// browser environment, then drive apply(ctx) with a fake slots service and
// verify the full registration contract (sidebar row + main panel + api).
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

const here = dirname(fileURLToPath(import.meta.url))
const clientBundle = join(here, '..', 'lib', 'client.js')

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

// --- fake browser environment (just enough for the bundle's top level) ---
const createdEls = []
globalThis.window = globalThis
globalThis.document = {
  createElement: (tag) => {
    const el = { tagName: tag, style: {}, textContent: '', id: '' }
    createdEls.push(el)
    return el
  },
  head: { appendChild: (el) => el },
  styleSheets: { length: 0 },
}
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node-smoke-test' }, configurable: true })

let captured = null
globalThis.window.__ModuleLoader__ = {
  load: (mod) => {
    captured = mod
  },
}

// --- evaluate the real built artifact (isolated scope: no test identifiers leak in) ---
const source = readFileSync(clientBundle, 'utf8')
check('bundle is the ModuleLoader wrapper', source.startsWith('window.__ModuleLoader__.load('), source.slice(0, 60))
const runBundle = new Function('window', 'document', 'navigator', 'require', source)
runBundle(globalThis, globalThis.document, globalThis.navigator, require)
check('bundle registered with ModuleLoader', captured !== null && typeof captured === 'object')
check('bundle id is @ckun/dsh-ssh', captured?.id === '@ckun/dsh-ssh', String(captured?.id))
check('bundle exposes a factory', typeof captured?.factory === 'function')

const mod = captured.factory(require)
check('factory returns the module exports', mod !== null && typeof mod === 'object')
check('module declares inject: ["slots"]', Array.isArray(mod?.inject) && mod.inject.join(',') === 'slots', JSON.stringify(mod?.inject))
check('module exposes apply', typeof mod?.apply === 'function')

// --- drive apply() with a fake slots service ---
const injections = []
const registrations = []
const disposers = []
const slots = {
  inject: (key, cb) => {
    injections.push(key)
    const r = cb() // the panel registers immediately inside the callback
    if (typeof r === 'function') disposers.push(r)
    return () => {}
  },
  register: (options, component) => {
    registrations.push({ options, component })
    return () => {}
  },
}
let effectCleanup = null
const ctx = {
  slots,
  effect: (fn) => {
    effectCleanup = fn
  },
}

mod.apply(ctx)

check('injected sidebar + main slots', injections.join(',') === 'sidebar.panellist,main', injections.join(','))
check('registered two surfaces', registrations.length === 2, String(registrations.length))

const sidebar = registrations[0]
check('sidebar: name + panel id + order', sidebar?.options?.name === 'sidebar.panellist' && sidebar?.options?.id === 'ckun-ssh-panel' && sidebar?.options?.order === 45, JSON.stringify(sidebar?.options))
check('sidebar: label() = SSH 运维', typeof sidebar?.options?.label === 'function' && sidebar.options.label() === 'SSH 运维', String(sidebar?.options?.label?.()))
check('sidebar: component is a function (icon)', typeof sidebar?.component === 'function')

const main = registrations[1]
check('main: name + key', main?.options?.name === 'main' && main?.options?.key === 'ckun-ssh-panel', JSON.stringify(main?.options))
check('main: component is a function (page)', typeof main?.component === 'function')
check('main: inject() provides an api', typeof main?.options?.inject === 'function')
const provided = typeof main?.options?.inject === 'function' ? main.options.inject() : {}
const api = provided?.api
check('api object has the data methods', api !== null && typeof api === 'object' && typeof api.listHosts === 'function' && typeof api.testHost === 'function' && typeof api.exec === 'function', JSON.stringify(Object.keys(api ?? {})))

// css injection ran against the fake document (panel + xterm)
check('css injected into document', createdEls.length >= 2, String(createdEls.length))

// teardown path must not throw
check('effect registered a cleanup', typeof effectCleanup === 'function')
let teardownOk = true
try {
  effectCleanup?.()
  for (const d of disposers) d()
} catch {
  teardownOk = false
}
check('dispose path is clean', teardownOk)

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
