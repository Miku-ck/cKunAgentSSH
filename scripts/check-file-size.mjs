#!/usr/bin/env node
// check-file-size.mjs — 生产源码文件行数门禁（P2 巨型文件拆分配套）。
//
// 规则（只降不升）：
//   1. 新增生产文件不得超过 LIMIT 行（默认 800）。
//   2. 存量超限文件禁止增行 —— 基线 scripts/file-size-baseline.json 记录拆分前
//      各超限文件的行数，行数只能下降；一旦回升即失败。
// 基线用 `--init` 生成/刷新（每次完成一轮拆分后刷新，把已降下来的文件移出基线）。
//
// 豁免：测试文件（*.test.* / *.spec.* / backend/tests/）、.d.ts、文档与生成物。
// 约定见 AGENTS.md「单文件单域」。
//
// 用法:
//   node scripts/check-file-size.mjs          检查
//   node scripts/check-file-size.mjs --init   生成本次基线
// 环境变量 FILE_SIZE_LIMIT 可覆盖上限。

import { readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs';
import { join, relative } from 'node:path';

const ROOT = process.cwd();
const LIMIT = Number(process.env.FILE_SIZE_LIMIT ?? 800);
const BASELINE = join(ROOT, 'scripts', 'file-size-baseline.json');
const EXTS = new Set(['.py', '.ts', '.vue', '.js', '.mjs', '.cjs']);
const SKIP_DIR = new Set([
  'node_modules', 'out', 'dist', 'runtime', '.git', '.superpowers',
  '.venv', '__pycache__', 'docs', '.mimosa', '.workbuddy', '.zcode',
  '.atomcode', '.codebuddy',
]);

function countLines(text) {
  return text.split(/\r?\n/).length - (text.endsWith('\n') ? 1 : 0);
}

function walk(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIR.has(entry.name)) continue;
    const p = join(dir, entry.name);
    if (entry.isDirectory()) walk(p, out);
    else if (!entry.isFile()) continue;
    else {
      const ext = entry.name.slice(entry.name.lastIndexOf('.'));
      if (!EXTS.has(ext)) continue;
      if (/\.test\.|\.spec\.|\.d\.ts$/.test(entry.name)) continue;
      const rel = relative(ROOT, p).replace(/\\/g, '/');
      if (rel.startsWith('backend/tests/')) continue;
      out.push([rel, countLines(readFileSync(p, 'utf8'))]);
    }
  }
  return out;
}

function loadBaseline() {
  if (!existsSync(BASELINE)) return {};
  try {
    const data = JSON.parse(readFileSync(BASELINE, 'utf8'));
    return data && typeof data.files === 'object' ? data.files : {};
  } catch {
    return {};
  }
}

const all = walk(ROOT);
const over = all.filter(([, lines]) => lines > LIMIT).sort((a, b) => b[1] - a[1]);

if (process.argv.includes('--init')) {
  const files = {};
  for (const [f, lines] of over) files[f] = lines;
  writeFileSync(BASELINE, `${JSON.stringify({ limit: LIMIT, files }, null, 2)}\n`, 'utf8');
  console.log(`已生成基线: ${over.length} 个超限文件（上限 ${LIMIT} 行）→ scripts/file-size-baseline.json`);
  process.exit(0);
}

const base = loadBaseline();
const grew = [];
const added = [];
for (const [f, lines] of over) {
  if (f in base) {
    if (lines > base[f]) grew.push([f, base[f], lines]);
  } else {
    added.push([f, lines]);
  }
}

console.log(`生产文件总数: ${all.length} / 超限(>${LIMIT}): ${over.length} / 基线记录: ${Object.keys(base).length}`);
for (const [f, lines] of over.slice(0, 15)) console.log(`  ${String(lines).padStart(5)}  ${f}`);

let failed = false;
if (added.length) {
  failed = true;
  console.error(`❌ 新增超限文件（不在基线内，上限 ${LIMIT} 行）:`);
  for (const [f, lines] of added) console.error(`     ${lines}  ${f}`);
}
if (grew.length) {
  failed = true;
  console.error(`❌ 存量超限文件增行（拆分只应让行数下降）:`);
  for (const [f, b, lines] of grew) console.error(`     ${f}: ${b} → ${lines}`);
}
// 基线中已降到上限以下的文件：提示可刷新基线，不阻塞
const shrunk = Object.keys(base).filter((f) => {
  const cur = all.find(([rel]) => rel === f);
  return cur && cur[1] <= LIMIT;
});
if (shrunk.length) {
  console.log(`ℹ️  以下基线文件已降到 ${LIMIT} 行内，可运行 \`node scripts/check-file-size.mjs --init\` 刷新基线:`);
  for (const f of shrunk) console.log(`     ${f}`);
}

if (failed) process.exit(1);
console.log('✅ 文件行数门禁通过');
