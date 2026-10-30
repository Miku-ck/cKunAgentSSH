/**
 * 诊断包纯逻辑守卫（shared/diagnostics-core.ts）。
 *
 * 这一层最要紧的不是"能打包"，而是**不能把凭据打出去**：诊断包会离开本机。
 * 所以每条脱敏规则都按"真实日志里长什么样"写用例，而不是按正则字面量写。
 */
import { describe, expect, it } from 'vitest'
import {
  DIAG_MAX_LOG_BYTES,
  buildSystemInfo,
  diagnosticsFileName,
  envSnapshot,
  redactSecrets,
  tailText,
} from './diagnostics-core'

describe('redactSecrets', () => {
  it('抹掉 env 风格的键值（含带前缀的键名）', () => {
    expect(redactSecrets('ELECTRON_BRIDGE_TOKEN=abcdef123456')).toBe('ELECTRON_BRIDGE_TOKEN=***')
    expect(redactSecrets('CKUN_API_KEY: sk-abcdefgh12345678')).not.toContain('abcdefgh12345678')
    expect(redactSecrets('password=hunter2')).toBe('password=***')
  })

  it('抹掉 Bearer / 厂商前缀令牌', () => {
    expect(redactSecrets('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9')).not.toContain('eyJhbGciOiJIUzI1NiJ9')
    expect(redactSecrets('using sk-proj-abcdefghijklmn')).toBe('using sk-***')
    expect(redactSecrets('push with ghp_abcdefghijklmnopqrstuvwxyz')).toBe('push with ghp_***')
  })

  it('抹掉代理 URL 里的口令，但保留主机端口', () => {
    expect(redactSecrets('proxy=http://user:pa55word@proxy.internal:3128'))
      .toBe('proxy=http://***@proxy.internal:3128')
  })

  it('短值/空值不动（否则日志里全是 ***，反而看不出问题）', () => {
    expect(redactSecrets('password: ')).toBe('password: ')
    expect(redactSecrets('token=ab')).toBe('token=ab')
  })

  it('幂等：抹过的文本再抹一遍不变', () => {
    const once = redactSecrets('ELECTRON_BRIDGE_TOKEN=abcdef123456')
    expect(redactSecrets(once)).toBe(once)
  })

  it('不碰普通中文日志', () => {
    const line = '[main] 后端启动完成 port=8080'
    expect(redactSecrets(line)).toBe(line)
  })
})

describe('tailText', () => {
  it('未超限原样返回', () => {
    expect(tailText('abc', 10)).toBe('abc')
  })

  it('超限只留尾部，且不产生替换符（不会把多字节字符劈开）', () => {
    const text = `${'一'.repeat(100)}尾部`
    const out = tailText(text, 30)
    expect(Buffer.byteLength(out, 'utf8')).toBeLessThanOrEqual(30)
    expect(out.endsWith('尾部')).toBe(true)
    expect(out.startsWith('\uFFFD')).toBe(false)
  })
})

describe('diagnosticsFileName', () => {
  it('版本 + 本地时间戳，文件名里不含冒号', () => {
    const name = diagnosticsFileName('0.1.0', new Date(2026, 9, 30, 12, 5, 9))
    expect(name).toBe('ckunagentssh-diag-0.1.0-20261030-120509.tar.gz')
    expect(name).not.toContain(':')
  })

  it('版本缺失时用 0.0.0 兜底', () => {
    expect(diagnosticsFileName('', new Date(2026, 0, 2, 3, 4, 5))).toBe('ckunagentssh-diag-0.0.0-20260102-030405.tar.gz')
  })
})

describe('envSnapshot', () => {
  it('只收白名单键，凭据类键一律出 "***"', () => {
    const snap = envSnapshot({
      CKUN_UPDATE_FEED: 'https://feed.example/latest.json',
      ELECTRON_BRIDGE_TOKEN: 'abcdef123456',
      CKUN_HTTP_PROXY: 'http://user:pw1234@proxy:3128',
      PATH: 'C:\\Windows',
      ANTHROPIC_API_KEY: 'sk-abcdefgh12345678',
    })
    expect(snap.CKUN_UPDATE_FEED).toBe('https://feed.example/latest.json')
    expect(snap.ELECTRON_BRIDGE_TOKEN).toBe('***')
    // 代理地址本身留着（排障要看指向哪），但口令抹掉
    expect(snap.CKUN_HTTP_PROXY).toBe('http://***@proxy:3128')
    // PATH / API key 这类根本不出包
    expect(snap.PATH).toBeUndefined()
    expect(snap.ANTHROPIC_API_KEY).toBeUndefined()
  })
})

describe('buildSystemInfo', () => {
  it('把版本、路径、日志清单、更新阶段与崩溃数摊平，并脱敏通道地址', () => {
    const info = buildSystemInfo({
      appVersion: '0.1.0',
      electronVersion: '35.7.5',
      chromeVersion: '134.0.0.0',
      nodeVersion: '22.0.0',
      platform: 'win32',
      arch: 'x64',
      locale: 'zh-CN',
      pid: 4242,
      startedAt: '2026-10-30T00:00:00.000Z',
      appDir: 'D:\\apps\\cKunAgentSSH',
      userDataDir: 'C:\\Users\\u\\AppData\\Roaming\\cKunAgentSSH',
      logs: [{ name: 'main.log', bytes: 1024 }],
      runtimeVersion: '3.11.15',
      runtimePath: 'C:\\app\\resources\\python-runtime\\python.exe',
      updateFeedUrl: 'https://u:secretpw@feed.example/latest.json',
      updatePhase: 'idle',
      crashDumps: 2,
      env: { ELECTRON_BRIDGE_TOKEN: 'abcdef123456' },
    })
    expect(info).toMatchObject({
      app: { version: '0.1.0', electron: '35.7.5', platform: 'win32', pid: 4242 },
      runtime: { python: '3.11.15', path: 'C:\\app\\resources\\python-runtime\\python.exe' },
      update: { phase: 'idle' },
      crashDumps: 2,
    })
    // 通道地址里的口令不能出包
    expect(JSON.stringify(info)).not.toContain('secretpw')
    expect(JSON.stringify(info)).not.toContain('abcdef123456')
  })

  it('日志上限常量是"尾部截断"的阈值本身（4MB）', () => {
    expect(DIAG_MAX_LOG_BYTES).toBe(4 * 1024 * 1024)
  })
})
