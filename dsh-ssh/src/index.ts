/**
 * @ckun/dsh-ssh — host half.
 *
 * Mounts the SSH engine (persistent ssh2 pool with host-key pinning), the
 * /api/ckun-ssh route family plus the terminal WebSocket upgrade, the agent
 * tools (ssh_list / ssh_exec / ssh_connect), the ssh_exec approval gate
 * (tools/pre-execute listener), and an optional system-prompt announcement.
 *
 * Security model:
 *  - host keys are pinned only by explicit human action (GUI test-connect);
 *  - agent connects to unpinned hosts are refused (fail closed);
 *  - ssh_exec: read-only runs; mutating/dangerous ask for approval; prod
 *    hosts deny mutating (config-gated) and always deny dangerous.
 */
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
// Type-only: pull the service Context merges (webServer / systemPrompt / tools + the tools/pre-execute event).
import type {} from '@deepseek-ai/dsh-host-webserver'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type {} from '@deepseek-ai/dsh-tools'
import { SshEngine } from './engine.ts'
import { makeRoutes } from './routes.ts'
import { HostStore } from './store.ts'
import { execPolicy } from './security.ts'
import { sshConnectTool, sshExecTool, sshListTool } from './tools.ts'

/** Stable cordis plugin name. */
export const name = 'ckun-ssh'

/** Services required before the SSH surfaces can mount. */
export const inject = ['webServer', 'tools', 'systemPrompt']

declare module '@deepseek-ai/cordis' {
  interface Events {
    /**
     * Volatile config values were committed into the running instance without a
     * remount (cordis-plugin-loader); dispatched to the owning fiber only.
     * @param paths - the changed config paths, as key arrays.
     * @mode emit
     */
    'loader/volatile-update'(paths: readonly (readonly string[])[]): void
  }
}

/** Plugin config as a profile patch declares it, before schema resolution. */
export interface Config {
  /** Master switch for the plugin (routes, tools, prompt section). */
  enabled?: boolean
  /** When true, a system-prompt section announces the SSH tools to every agent. */
  announceToAgent?: boolean
  /** Per-output cap for ssh_exec stdout/stderr, in bytes. */
  maxOutputBytes?: number
  /** ssh2 keepalive interval, in milliseconds. */
  keepaliveMs?: number
  /** Allow mutating (still approval-gated) commands on prod hosts. */
  allowProdMutations?: boolean
}

/** The config the Host hands to apply — the runtime face of Config (fields may be live refs). */
export interface ResolvedConfig {
  enabled?: ConfigField<boolean>
  announceToAgent?: ConfigField<boolean>
  maxOutputBytes?: ConfigField<number>
  keepaliveMs?: ConfigField<number>
  allowProdMutations?: ConfigField<boolean>
}

/** A stable reference a volatile config field resolves to. */
interface ConfigRef<T> {
  get(): T
}

/** One resolved config field: a live reference, or a plain value. */
type ConfigField<T> = ConfigRef<T> | T

/** Read one possibly-live config field with a fallback. */
function val<T>(field: ConfigField<T> | undefined, fallback: T): T {
  if (field === undefined || field === null) return fallback
  if (typeof field === 'object' && typeof (field as ConfigRef<T>).get === 'function') return (field as ConfigRef<T>).get()
  return field as T
}

/** Plugin config schema (volatile fields live-edit through the settings page). */
export const Config = z.object({
  enabled: z.boolean().default(true).volatile(),
  announceToAgent: z.boolean().default(true).volatile(),
  maxOutputBytes: z.number().default(65536).volatile(),
  keepaliveMs: z.number().default(30000).volatile(),
  allowProdMutations: z.boolean().default(false).volatile(),
})

/** The agent-facing announcement (kept bilingual; the model reads it verbatim). */
const SSH_PROMPT = `Remote SSH operations are available via the @ckun/dsh-ssh plugin:
- ssh_list: list configured hosts (alias / target / auth / environment / host-key pin / connection state).
- ssh_connect: open or reuse the pooled connection and fetch the environment profile (distro / init / package manager). Call it before ssh_exec when the command depends on the target system.
- ssh_exec: run a shell command on a remote host (NOT this machine). Read-only commands run directly; mutating and dangerous commands trigger a user approval card; mutating operations on prod hosts are denied.
Hosts whose host key is not pinned (state UNPINNED) cannot be connected — ask the user to pin it in the SSH panel first. For the web terminal, SFTP transfer and tunnels, the user operates the SSH panel in the sidebar; suggest it when they want an interactive session or file transfer.`

/**
 * Mount the plugin surfaces.
 * @param ctx - the plugin context.
 * @param config - resolved (possibly live) config.
 */
export function apply(ctx: Context, config: ResolvedConfig): void {
  const store = new HostStore()
  const engine = new SshEngine(store, {
    maxOutputBytes: () => val(config.maxOutputBytes, 65536),
    keepaliveMs: () => val(config.keepaliveMs, 30000),
  })

  let surfaces: Array<() => void> = []

  /** (Re)derive every surface from the current config values. */
  const sync = (): void => {
    for (const dispose of surfaces.splice(0)) dispose()
    if (!val(config.enabled, true)) return
    const { routes, upgrade } = makeRoutes(engine, store)
    for (const route of routes) surfaces.push(ctx.webServer.register(route))
    surfaces.push(ctx.webServer.registerUpgrade(upgrade))
    for (const tool of [sshListTool(engine), sshConnectTool(engine), sshExecTool(engine)]) {
      surfaces.push(ctx.tools.register(tool))
    }
    // The ssh_exec approval gate: classify, then allow / deny / ask.
    // GUI-originated execs (the panel) bypass this listener by design.
    surfaces.push(
      ctx.on(
        'tools/pre-execute',
        async (exec, next) => {
          if (exec.name !== 'ssh_exec') return next()
          const args = (exec.arguments ?? {}) as { alias?: unknown; command?: unknown }
          if (typeof args.command !== 'string' || args.command.trim() === '') return next()
          const host = typeof args.alias === 'string' ? store.get(args.alias) : undefined
          const policy = execPolicy({
            command: args.command,
            environment: host?.environment,
            hostAlias: host?.alias ?? String(args.alias ?? '?'),
            allowProdMutations: val(config.allowProdMutations, false),
          })
          if (policy.decision === 'allow') return next()
          if (policy.decision === 'deny') return { kind: 'deny', reason: policy.reason }
          return {
            kind: 'ask',
            reason: policy.reason,
            displayReason: { zh: policy.zh, en: policy.reason },
          }
        },
        { prepend: true },
      ),
    )
    if (val(config.announceToAgent, true)) {
      surfaces.push(ctx.systemPrompt.section({ name: 'ckun-ssh', order: 5100, text: SSH_PROMPT }))
    }
  }

  // Settings edits commit into this instance's config refs without a remount.
  ctx.on('loader/volatile-update', () => sync())

  sync()

  ctx.effect(
    () => () => {
      for (const dispose of surfaces.splice(0)) dispose()
      engine.dispose()
    },
    'ckun-ssh: shutdown',
  )
}
