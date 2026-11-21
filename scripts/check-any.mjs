#!/usr/bin/env node
// check-any.mjs — 统计 TypeScript / Vue 源码中的显式 any 类型逃逸。
//
// 只降不升：数量超过基线上限即失败，防止新代码把类型系统掏空。
// 基线 = 当前存量（2 处，均在 electron/utils/ipc-handler.ts：IPC 分发器
// 的 args/返回开放是唯一合理逃逸）。排除测试与 .d.ts。
//
// 用法: node scripts/check-any.mjs
// 环境变量 ANY_LIMIT 可覆盖上限。

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const ROOT = process.cwd();
const LIMIT = Number(process.env.ANY_LIMIT ?? 2);
const EXTS = new Set(['.ts', '.vue']);
const SKIP_DIR = new Set(['node_modules', 'out', 'dist', 'runtime', '.git', '.superpowers']);

// 显式 any 的常见书写形态（类型位置）
const PATTERNS = [
  /:\s*any\b/g,          // : any
  /\bas\s+any\b/g,       // as any
  /<any>/g,              // <any>
  /\bany\[\]/g,          // any[]
  /Array<any>/g,         // Array<any>
  /Record<[^,>]*,\s*any>/g, // Record<K, any>
  /Record<any,/g,        // Record<any, V>
  /Promise<any>/g,       // Promise<any>
  /\(any\)/g,            // (any)
  /\bany\b\s*=>/g,       // any => （回调参数）
];

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIR.has(name)) continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (EXTS.has(name.slice(name.lastIndexOf('.')))) out.push(p);
  }
  return out;
}

let total = 0;
const perFile = [];
for (const file of walk(ROOT)) {
  if (/\.test\.|\.spec\.|\.d\.ts$/.test(file)) continue;
  const text = readFileSync(file, 'utf8');
  let n = 0;
  for (const re of PATTERNS) {
    const m = text.match(re);
    if (m) n += m.length;
  }
  if (n) {
    total += n;
    perFile.push([relative(ROOT, file).replace(/\\/g, '/'), n]);
  }
}

perFile.sort((a, b) => b[1] - a[1]);
console.log(`显式 any 计数: ${total} / 上限 ${LIMIT}`);
for (const [f, n] of perFile.slice(0, 15)) console.log(`  ${String(n).padStart(3)}  ${f}`);

if (total > LIMIT) {
  console.error(`❌ any 数量 ${total} 超过上限 ${LIMIT}`);
  process.exit(1);
}
console.log('✅ any 门禁通过');
