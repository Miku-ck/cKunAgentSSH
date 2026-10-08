/**
 * @ckun/dsh-ssh — client half (web GUI).
 * Registers the "SSH 运维" panel: sidebar row + main content (host tree,
 * PTY terminal, SFTP browser, tunnel manager, host form dialog).
 */
import type { Context as ClientContext } from '@deepseek-ai/cordis'
import { CSS_TEXT } from './css.ts'
import xtermCss from '@xterm/xterm/css/xterm.css'
import { registerSshPanel } from './panel/SshPanel.tsx'

let cssInjected = false

/** Inject panel + xterm styles once (idempotent across remounts). */
function injectCss(): void {
  if (cssInjected || typeof document === 'undefined') return
  cssInjected = true
  for (const css of [CSS_TEXT, xtermCss]) {
    const style = document.createElement('style')
    style.id = `ckun-ssh-css-${document.styleSheets.length}`
    style.textContent = css
    document.head.appendChild(style)
  }
}

/** The client services this half consumes. */
export const inject = ['slots']

/** Slot surface (typed locally to avoid a hard dependency on the slots package types). */
interface SlotsApi {
  inject(key: string, callback: () => () => void): () => void
  register(options: Record<string, unknown>, component: unknown): () => void
}

/**
 * Mount the SSH panel into the GUI.
 * @param ctx - the client plugin context.
 */
export function apply(ctx: ClientContext): void {
  try {
    injectCss()
  } catch (error) {
    console.warn('[ckun-ssh] css inject failed:', error)
  }
  try {
    const slots = (ctx as unknown as { slots?: SlotsApi }).slots
    if (slots === undefined) throw new Error('slots service unavailable on this client context')
    const dispose = registerSshPanel(slots)
    ctx.effect(() => () => {
      dispose()
    }, 'ckun-ssh: ui mounts')
  } catch (error) {
    console.warn('[ckun-ssh] panel registration failed:', error)
  }
}
