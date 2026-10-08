// Build @ckun/dsh-ssh:
//  - host half  -> lib/index.js   (single-file ESM; bare imports resolved by the dsh host at runtime)
//  - client half-> lib/client.js  (CJS bundle wrapped in window.__ModuleLoader__.load)
import * as esbuild from 'esbuild'
import { readFileSync, writeFileSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('.', import.meta.url))
const p = (...parts) => root + parts.join('/')

rmSync(p('lib'), { recursive: true, force: true })

// Host half: bundle our own TS, keep every bare specifier external (ssh2, ws,
// @deepseek-ai/* SDK modules are provided by the dsh host process).
await esbuild.build({
  entryPoints: [p('src', 'index.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  target: 'node22',
  packages: 'external',
  outfile: p('lib', 'index.js'),
  sourcemap: true,
  logLevel: 'warning',
})

// Client half: browser bundle; React + every @deepseek-ai client module stay
// external (resolved by the dsh web runtime's module loader). xterm.js and its
// CSS are inlined (CSS as a text string via the loader override below).
await esbuild.build({
  entryPoints: [p('src', 'client', 'index.ts')],
  bundle: true,
  format: 'cjs',
  platform: 'browser',
  target: 'es2020',
  jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', '@deepseek-ai/*'],
  loader: { '.css': 'text' },
  outfile: p('lib', 'client.bundle.cjs'),
  sourcemap: true,
  logLevel: 'warning',
})

// Wrap the CJS bundle in the module-loader factory the dsh web runtime expects.
const bundle = readFileSync(p('lib', 'client.bundle.cjs'), 'utf8')
const wrapped =
  'window.__ModuleLoader__.load({\n' +
  '\tid: "@ckun/dsh-ssh",\n' +
  '\tfactory: (require) => {\n' +
  '\t\tvar module = { exports: {} };\n' +
  '\t\tvar exports = module.exports;\n' +
  bundle +
  '\t\treturn module.exports;\n' +
  '\t}\n' +
  '});\n'
writeFileSync(p('lib', 'client.js'), wrapped)
console.log('build ok: lib/index.js + lib/client.js')
