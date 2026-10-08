// Unit tests for the credential vault: key sanitization, record round-trips,
// delete semantics, and the graceful fallback when the service is missing.
import { createCredentialVault, secretIdForAlias } from '../src/vault.ts'

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

/** In-memory stand-in for the credentials seam (ctx.credentials). */
function mockProvider() {
  const records = new Map()
  return {
    records,
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
}

// alias -> credential-key id sanitization
check('alias passthrough', secretIdForAlias('gpu-01') === 'gpu-01', secretIdForAlias('gpu-01'))
check('alias lowercased', secretIdForAlias('My GPU 01') === 'my-gpu-01', secretIdForAlias('My GPU 01'))
check('alias leading digit', secretIdForAlias('123') === 'h-123', secretIdForAlias('123'))
check('alias junk collapsed', secretIdForAlias('a..b!!c') === 'a-b-c', secretIdForAlias('a..b!!c'))
check('alias empty', secretIdForAlias('!!!') === 'host', secretIdForAlias('!!!'))

// missing service -> unavailable vault, all no-ops
const off = createCredentialVault(undefined)
check('unavailable without service', off.available === false)
check('unavailable read is empty', JSON.stringify(await off.read('x')) === '{}')
await off.save('x', { password: 'p' })
await off.remove('x')

// unavailable when the object is not the seam shape
const off2 = createCredentialVault({ readRecord: async () => undefined })
check('unavailable for partial object', off2.available === false)

// record round-trip
const provider = mockProvider()
const vault = createCredentialVault(provider)
check('available with service', vault.available === true)

await vault.save('My GPU 01', { password: 'pw1' })
const key = 'ckun-ssh/my-gpu-01'
check('record stored under sanitized key', provider.records.has(key), [...provider.records.keys()].join(','))
const rec = provider.records.get(key)
check('record is a grant with payload v1', rec && rec.kind === 'grant' && rec.payload.v === 1, JSON.stringify(rec))
check('record carries password only', rec.payload.password === 'pw1' && rec.payload.passphrase === undefined, JSON.stringify(rec.payload))
check('read returns the secret', JSON.stringify(await vault.read('My GPU 01')) === JSON.stringify({ password: 'pw1' }))

// passphrase + password together
await vault.save('My GPU 01', { password: 'pw2', passphrase: 'pp' })
check('both secrets stored', JSON.stringify(await vault.read('My GPU 01')) === JSON.stringify({ password: 'pw2', passphrase: 'pp' }))

// empty save deletes the record
await vault.save('My GPU 01', {})
check('empty save deletes record', provider.records.has(key) === false)
check('read after delete is empty', JSON.stringify(await vault.read('My GPU 01')) === '{}')

// remove()
await vault.save('node-a', { password: 'x' })
await vault.remove('node-a')
check('remove deletes record', provider.records.has('ckun-ssh/node-a') === false)

// foreign record shapes are ignored (never leak other plugins' payloads)
provider.records.set('ckun-ssh/other', { kind: 'api-key', key: 'sk-1' })
check('api-key record reads as empty', JSON.stringify(await vault.read('other')) === '{}')
provider.records.set('ckun-ssh/other2', { kind: 'grant', payload: { v: 2 } })
check('unknown payload version reads as empty', JSON.stringify(await vault.read('other2')) === '{}')

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
