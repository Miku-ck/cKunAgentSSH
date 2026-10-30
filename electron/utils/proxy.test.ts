/**
 * 代理探测纯逻辑（electron/utils/proxy.ts）：
 * 解析 / CONNECT 应答 / WinINET 多协议值 / 脱敏。
 * 隧道本身要真连网，这里只钉输入输出与缓存键。
 */
import { describe, expect, it } from 'vitest'
import {
  maskProxy,
  parseConnectResponse,
  parseProxySpec,
  pickProxyServerValue,
  tunnelAgentFor,
} from './proxy'

describe('代理字符串解析', () => {
  it('裸 host:port 默认按 http 代理处理', () => {
    expect(parseProxySpec('127.0.0.1:7890')).toMatchObject({ protocol: 'http', host: '127.0.0.1', port: 7890 })
  })

  it('http / https 都走 CONNECT 隧道（https 代理复用同一实现）', () => {
    expect(parseProxySpec('http://proxy.example:8080')).toMatchObject({ protocol: 'http', port: 8080 })
    expect(parseProxySpec('https://proxy.example:3128')).toMatchObject({ protocol: 'http', port: 3128 })
  })

  it('socks 单独标记（不建隧道，仅上报给 UI）', () => {
    expect(parseProxySpec('socks5://127.0.0.1:1080')).toMatchObject({ protocol: 'socks', port: 1080 })
    expect(parseProxySpec('socks4://127.0.0.1:1080')).toMatchObject({ protocol: 'socks' })
  })

  it('缺端口 / 空值 / 未知协议一律算无效（宁可不走代理，也不猜端口）', () => {
    expect(parseProxySpec('')).toBeNull()
    expect(parseProxySpec(null)).toBeNull()
    expect(parseProxySpec('   ')).toBeNull()
    expect(parseProxySpec('socks5://127.0.0.1')).toBeNull()
    expect(parseProxySpec('ftp://proxy.example:21')).toBeNull()
  })

  it('认证信息按 URL 编码解码后保留（CONNECT 时要拼 Base64）', () => {
    expect(parseProxySpec('http://user:pa%40ss@proxy.example:8080')?.auth).toBe('user:pa@ss')
    expect(parseProxySpec('http://proxy.example:8080')?.auth).toBeUndefined()
  })
})

describe('CONNECT 应答解析', () => {
  it('2xx 视为隧道建立，其余（407/403/5xx）都是失败', () => {
    expect(parseConnectResponse('HTTP/1.1 200 Connection established')).toEqual({ ok: true, status: 200 })
    expect(parseConnectResponse('HTTP/1.0 200 OK')).toEqual({ ok: true, status: 200 })
    expect(parseConnectResponse('HTTP/1.1 407 Proxy Authentication Required')).toEqual({ ok: false, status: 407 })
    expect(parseConnectResponse('HTTP/1.1 502 Bad Gateway')).toEqual({ ok: false, status: 502 })
  })

  it('不是 HTTP 应答就返回 null（别把 TLS 首包当状态行）', () => {
    expect(parseConnectResponse('')).toBeNull()
    expect(parseConnectResponse('\x16\x03\x01\x00')).toBeNull()
    expect(parseConnectResponse('HTTP/2 200')).toBeNull()
  })
})

describe('Windows ProxyServer 多协议值', () => {
  it('形如 http=h:p;https=h:p 时取 https 优先', () => {
    expect(pickProxyServerValue('http=127.0.0.1:8888;https=127.0.0.1:9999')).toBe('127.0.0.1:9999')
    expect(pickProxyServerValue('ftp=x:1;http=127.0.0.1:8888')).toBe('127.0.0.1:8888')
  })

  it('没有 http/https 段就返回 null（不拿 ftp 代理去连 443）', () => {
    expect(pickProxyServerValue('ftp=127.0.0.1:21')).toBeNull()
    expect(pickProxyServerValue('')).toBeNull()
    expect(pickProxyServerValue(null)).toBeNull()
  })

  it('单值形态原样返回', () => {
    expect(pickProxyServerValue('127.0.0.1:7890')).toBe('127.0.0.1:7890')
  })
})

describe('隧道 Agent 缓存与脱敏', () => {
  it('同一代理复用同一个 Agent（避免每次探测新建隧道类）', () => {
    const a = parseProxySpec('127.0.0.1:7890')!
    expect(tunnelAgentFor(a)).toBe(tunnelAgentFor(a))
    expect(tunnelAgentFor(parseProxySpec('127.0.0.1:7891')!)).not.toBe(tunnelAgentFor(a))
  })

  it('脱敏只保留 host:port，不把认证信息打到界面', () => {
    expect(maskProxy(parseProxySpec('http://user:secret@proxy.example:8080')!)).toBe('proxy.example:8080（含认证）')
    expect(maskProxy(parseProxySpec('proxy.example:8080')!)).toBe('proxy.example:8080')
  })
})
