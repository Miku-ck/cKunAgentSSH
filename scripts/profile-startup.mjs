#!/usr/bin/env node
'use strict'
/**
 * 启动日志剖析：把 [perf] 打点还原成关键路径分段表，并可与一份热态基线对表。
 *
 * 用法：
 *   node scripts/profile-startup.mjs <logfile> [--baseline <baselineLog>]
 *   type main.log | node scripts/profile-startup.mjs --stdin
 *
 * 典型：
 *   node scripts/profile-startup.mjs docs/startup-baseline-cold.log --baseline dev-server.log
 *
 * 只解析 [perf] 行与 Python 的 startup 汇总行，不改动任何文件。
 */
import fs from 'node:fs'

const STAGES = [
  { name: '主进程入口加载',            from: null,                          to: 'main module loaded' },
  { name: 'Electron 初始化 whenReady', from: 'main module loaded',          to: 'whenReady resolved' },
  { name: 'IPC / Manager 装配',        from: 'whenReady resolved',          to: 'ipc setup done' },
  { name: 'BrowserWindow 创建',        from: 'ipc setup done',              to: 'createWindow returned' },
  { name: '解释器解析 → Python spawn', from: 'createWindow returned',       to: 'backend: spawned pid=' },
  { name: 'dev server 探活',           from: 'backend: spawned pid=',       to: 'dev server probe done' },
  { name: '探活完成 → 首次导航',       from: 'dev server probe done',       to: 'renderer did-start-navigation' },
  { name: '导航 → DOM 就绪（首屏）',   from: 'renderer did-start-navigation', to: 'renderer dom-ready' },
  { name: 'Python 后端就绪',           from: 'backend: spawned pid=',       to: 'backend: health ready' },
  { name: '端到端：首窗可见',          from: null,                          to: 'window ready-to-show' },
  { name: '端到端：加载完成',          from: null,                          to: 'renderer did-finish-load' },
]

const PY_RE = /startup imports=(\d+)ms assemble=(\d+)ms boot=(\d+)ms lifespan=(\d+)ms total=(\d+)ms \| init_db=(\d+)ms round1=(\d+)ms background=(\d+)ms/

function parse(text) {
  const marks = []
  const re = /\[perf\]\s+(.+?)\s+@(\d+)ms/g
  let m
  while ((m = re.exec(text)) !== null) marks.push({ name: m[1], ms: Number(m[2]) })
  const py = text.match(PY_RE)
  const python = py ? {
    imports: +py[1], assemble: +py[2], boot: +py[3], lifespan: +py[4],
    total: +py[5], init_db: +py[6], round1: +py[7], background: +py[8],
  } : null
  return { marks, python }
}

function at(marks, prefix) {
  if (prefix === null) return 0
  const hit = marks.find((x) => x.name.startsWith(prefix))
  return hit ? hit.ms : null
}

function stageMs(data, stage) {
  const a = at(data.marks, stage.from)
  const b = at(data.marks, stage.to)
  return a === null || b === null ? null : b - a
}

function pad(s, n) { return String(s).padEnd(n, ' ') }
function rpad(s, n) { return String(s).padStart(n, ' ') }

function fmt(ms) { return ms === null ? '   n/a' : rpad(ms + 'ms', 8) }

function main() {
  const argv = process.argv.slice(2)
  const baseIdx = argv.indexOf('--baseline')
  const baselinePath = baseIdx >= 0 ? argv[baseIdx + 1] : null
  const useStdin = argv.includes('--stdin')
  const logPath = argv.find((a) => a !== '--stdin' && a !== '--baseline' && a !== baselinePath)

  let text
  if (useStdin) {
    text = fs.readFileSync(0, 'utf8')
  } else if (logPath) {
    text = fs.readFileSync(logPath, 'utf8')
  } else {
    console.error('用法: node scripts/profile-startup.mjs <logfile> [--baseline <baselineLog>] [--stdin]')
    process.exit(1)
  }
  const cur = parse(text)
  const base = baselinePath ? parse(fs.readFileSync(baselinePath, 'utf8')) : null

  console.log(`当前日志 : ${useStdin ? '<stdin>' : logPath}  (${cur.marks.length} 个打点)`)
  console.log(`基线日志 : ${baselinePath || '无（只输出绝对值）'}${base ? `  (${base.marks.length} 个打点)` : ''}`)
  console.log('')
  console.log(pad('阶段', 30) + rpad('本次', 10) + rpad('热态基线', 10) + rpad('倍数', 8) + '  判定')
  console.log('-'.repeat(74))
  for (const s of STAGES) {
    const c = stageMs(cur, s)
    const b = base ? stageMs(base, s) : null
    let verdict = ''
    if (c !== null && c >= 3000) verdict = 'SLOW 关键瓶颈'
    else if (b !== null && b > 0 && c !== null && c / b >= 3) verdict = 'SLOW 冷态放大'
    else if (c !== null) verdict = 'ok'
    const ratio = (b !== null && b > 0 && c !== null) ? (c / b).toFixed(1) + 'x' : '-'
    console.log(pad(s.name, 30) + pad(fmt(c), 10) + pad(b === null ? '   n/a' : fmt(b), 10) + pad(ratio, 8) + '  ' + verdict)
  }

  if (cur.python) {
    const p = cur.python
    const bp = base && base.python
    console.log('')
    console.log('Python 后端自报（相对模块开始执行）')
    console.log(pad('  分段', 30) + rpad('本次', 10) + (bp ? rpad('热态基线', 10) : ''))
    for (const k of ['imports', 'assemble', 'boot', 'lifespan', 'total', 'init_db', 'round1', 'background']) {
      console.log(pad('  ' + k, 30) + pad(rpad(p[k] + 'ms', 8), 10) + (bp ? pad(rpad(bp[k] + 'ms', 8), 10) : ''))
    }
  }

  // 关键路径判定：渲染链与后端链是并行发起的，谁最后结束谁决定总时长
  const navEnd = at(cur.marks, 'renderer dom-ready')
  const beEnd = at(cur.marks, 'backend: health ready')
  const finish = at(cur.marks, 'renderer did-finish-load')
  console.log('')
  console.log('并行链判定')
  console.log(`  渲染链终点 dom-ready      : ${navEnd === null ? 'n/a' : navEnd + 'ms'}`)
  console.log(`  后端链终点 health ready   : ${beEnd === null ? 'n/a' : beEnd + 'ms'}`)
  console.log(`  端到端 did-finish-load    : ${finish === null ? 'n/a' : finish + 'ms'}`)
  if (navEnd !== null && beEnd !== null) {
    const winner = navEnd >= beEnd ? '渲染链' : '后端链'
    console.log(`  关键路径 = ${winner}（两条链并行，慢的那条决定总启动）`)
  }
  const show = at(cur.marks, 'window ready-to-show')
  if (show !== null && finish !== null && finish > show) {
    console.log(`  [WARN] 首窗可见后仍有 ${finish - show}ms 的加载尾段（含 6s 兜底亮窗造成的空窗体感）`)
  }

  // 最慢的三段串行增量
  const slow = STAGES
    .filter((s) => s.from !== null)
    .map((s) => ({ name: s.name, ms: stageMs(cur, s) }))
    .filter((x) => x.ms !== null && x.ms > 0)
    .sort((a, b) => b.ms - a.ms)
    .slice(0, 3)
  console.log('')
  console.log('最慢三段（按串行增量，含并行重叠，仅用于定位）')
  for (const x of slow) console.log(`  ${rpad(x.ms + 'ms', 8)}  ${x.name}`)
}

main()
