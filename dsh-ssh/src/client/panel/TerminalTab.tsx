/**
 * PTY terminal tab: xterm.js over the plugin's terminal WebSocket.
 * Each mount opens one remote shell on the pooled connection; the pool (and
 * the pinned host key) outlive the tab.
 */
import { useEffect, useRef, useState } from 'react'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal, type ITheme } from '@xterm/xterm'
import { openTerminal } from '../api.ts'
import { IconInfo } from './SshPanel.tsx'

// The xterm theme cannot read the dsw alias tokens directly (xterm wants
// concrete colors), so mirror them as two fixed palettes keyed to the
// body[data-ds-dark-theme] attribute the GUI theme service toggles.
const ANSI_DARK = {
  black: '#484f58', red: '#ff7b72', green: '#3fb950', yellow: '#d29922',
  blue: '#4c82f7', magenta: '#bc8cff', cyan: '#39c5cf', white: '#e6e8ee',
  brightBlack: '#6e7681', brightRed: '#ffa198', brightGreen: '#56d364',
  brightYellow: '#e3b341', brightBlue: '#7ea6ff', brightMagenta: '#d2a8ff',
  brightCyan: '#56d4dd', brightWhite: '#f0f6fc',
}
const ANSI_LIGHT = {
  black: '#24292f', red: '#cf222e', green: '#116329', yellow: '#9a6700',
  blue: '#0550ae', magenta: '#8250df', cyan: '#1b7c83', white: '#57606a',
  brightBlack: '#57606a', brightRed: '#a40e26', brightGreen: '#1a7f37',
  brightYellow: '#6f4200', brightBlue: '#0969da', brightMagenta: '#6639ba',
  brightCyan: '#39916f', brightWhite: '#24292f',
}

function isDarkTheme(): boolean {
  return typeof document !== 'undefined' && document.body?.hasAttribute('data-ds-dark-theme') === true
}

function terminalTheme(): ITheme {
  return isDarkTheme()
    ? { background: 'transparent', foreground: '#e6e8ee', cursor: '#4c82f7', ...ANSI_DARK }
    : { background: 'transparent', foreground: '#24292f', cursor: '#0969da', ...ANSI_LIGHT }
}

export function TerminalTab({ alias }: { alias: string }): React.JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const [exitInfo, setExitInfo] = useState<{ tone: 'note' | 'error'; text: string } | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (host === null) return
    const term = new Terminal({
      fontFamily: 'Cascadia Mono, Consolas, "Courier New", monospace',
      fontSize: 13,
      cursorBlink: true,
      lineHeight: 1.3,
      theme: terminalTheme(),
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host)
    // Re-skin when the GUI switches light/dark (attribute on <body>).
    const themeObserver = typeof MutationObserver === 'undefined' ? undefined : new MutationObserver(() => {
      term.options.theme = terminalTheme()
    })
    themeObserver?.observe(document.body, { attributes: true, attributeFilter: ['data-ds-dark-theme'] })
    let closed = false
    const socket = openTerminal(
      alias,
      (frame) => {
        if (closed) return
        if (frame.type === 'ready') {
          try {
            fit.fit()
            socket.send({ type: 'resize', cols: term.cols, rows: term.rows })
          } catch {
            /* ignore */
          }
        } else if (frame.type === 'output') {
          term.write(frame.data)
        } else if (frame.type === 'exit') {
          const detail = frame.code !== null && frame.code !== undefined ? ` (exit ${frame.code})` : ''
          const text = `[会话结束${detail}${frame.error ? ` · ${frame.error}` : ''}]`
          const tone = frame.error !== undefined || (frame.code !== null && frame.code !== 0) ? 'error' : 'note'
          setExitInfo({ tone, text })
          term.write(`\r\n\x1b[90m${text}\x1b[0m\r\n`)
        }
      },
      () => {
        if (!closed) term.write('\r\n\x1b[90m[连接已断开]\x1b[0m\r\n')
      },
    )
    term.onData((data) => socket.send({ type: 'input', data }))
    const ro = new ResizeObserver(() => {
      try {
        fit.fit()
        socket.send({ type: 'resize', cols: term.cols, rows: term.rows })
      } catch {
        /* ignore */
      }
    })
    ro.observe(host)
    return () => {
      closed = true
      themeObserver?.disconnect()
      ro.disconnect()
      socket.close()
      term.dispose()
    }
  }, [alias])

  return (
    <div className="ckun-ssh-term-wrap">
      {exitInfo !== null && (
        <div className={`ckun-ssh-term-banner${exitInfo.tone === 'error' ? ' error' : ''}`}>
          <IconInfo />
          <span>{exitInfo.text}（重新进入“终端”标签可再开一个 shell）</span>
          <button className="ckun-ssh-term-banner-close" title="关闭提示" onClick={() => setExitInfo(null)}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        </div>
      )}
      <div ref={hostRef} style={{ height: '100%' }} />
    </div>
  )
}
