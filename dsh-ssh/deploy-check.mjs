// Verify the deployed plugin loads from the desktop profile context.
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const plugin = pathToFileURL('C:/Users/ckun/.dsh/profiles/desktop/node_modules/@ckun/dsh-ssh/lib/index.js')
const mod = await import(plugin).catch((e) => {
  console.error('DEPLOY LOAD FAIL:', e)
  process.exit(1)
})
console.log('DEPLOY LOAD OK')
console.log('name:', mod.name)
console.log('inject:', JSON.stringify(mod.inject))
console.log('apply:', typeof mod.apply, '| Config present:', !!mod.Config)

const client = readFileSync('C:/Users/ckun/.dsh/profiles/desktop/node_modules/@ckun/dsh-ssh/lib/client.js', 'utf8')
console.log('client.js head:', client.slice(0, 60).replace(/\s+/g, ' '))
console.log('client.js tail:', client.slice(-50).replace(/\s+/g, ' '))
console.log('client.js bytes:', client.length)
if (!client.startsWith('window.__ModuleLoader__.load(')) {
  console.error('client.js wrapper missing!')
  process.exit(1)
}
console.log('ALL DEPLOY CHECKS PASSED')
