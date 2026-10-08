/**
 * Agent tools: the model-facing SSH surface.
 *
 *  - ssh_list    : configured hosts + pin/connection state (read-only)
 *  - ssh_connect : open/reuse the pooled connection + environment profile
 *  - ssh_exec    : non-interactive command execution (approval-gated by the
 *                  tools/pre-execute listener, see index.ts)
 *
 * Every tool talks to the same engine the web GUI uses, so a host configured in
 * the panel is immediately operable by any agent, and vice versa.
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { SshEngine } from './engine.ts'

/** One text content block (the only render shape these tools emit). */
function text(value: string): { type: 'text'; text: string }[] {
  return [{ type: 'text', text: value }]
}

/** Host table render shared by list surfaces. */
function renderHosts(hosts: { alias: string; host: string; port: number; user: string; auth: string; environment?: string; pinned: boolean; connected: boolean; description?: string }[]): string {
  if (hosts.length === 0) return 'no hosts configured（请在 SSH 面板新增主机）'
  const rows = hosts.map((h) => [
    h.alias,
    `${h.user}@${h.host}:${h.port}`,
    h.auth,
    h.environment ?? '-',
    h.pinned ? 'pinned' : 'UNPINNED',
    h.connected ? 'connected' : 'idle',
    h.description ?? '',
  ].join(' | '))
  return ['alias | target | auth | env | hostkey | state | desc', '--- | --- | --- | --- | --- | --- | ---', ...rows].join('\n')
}

/** Render one exec result (mirrors the bash-tool exit-code convention). */
function renderExec(result: { success: boolean; exitCode: number | null; timedOut: boolean; stdout: string; stderr: string; durationMs: number; truncated?: boolean; error?: string }): string {
  const marker = result.timedOut
    ? '[timed out]'
    : result.exitCode !== null
      ? `[exit code: ${result.exitCode}]`
      : result.error
        ? `[error]`
        : '[no exit code]'
  const parts = [marker]
  if (result.stdout) parts.push('stdout:\n' + result.stdout)
  if (result.stderr) parts.push('stderr:\n' + result.stderr)
  if (result.truncated) parts.push('[output truncated]')
  if (result.error) parts.push('error: ' + result.error)
  parts.push(`duration: ${result.durationMs} ms`)
  return parts.join('\n')
}

/** The host-list tool. */
export function sshListTool(engine: SshEngine) {
  return defineTool({
    name: 'ssh_list',
    description:
      'List SSH hosts configured for remote operations (alias, target, auth, environment, host-key pin state, connection state). Use the alias with ssh_connect / ssh_exec. UNPINNED hosts cannot be connected until the user pins their host key in the SSH panel.',
    parameters: {
      query: { type: 'string', description: 'Optional fuzzy filter over alias / host / user / description / tags.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          hosts: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                alias: { type: 'string', required: true },
                host: { type: 'string', required: true },
                port: { type: 'integer', required: true },
                user: { type: 'string', required: true },
                auth: { type: 'string', required: true },
                environment: { type: 'string' },
                pinned: { type: 'boolean', required: true },
                connected: { type: 'boolean', required: true },
                description: { type: 'string' },
              },
            },
          },
        },
      },
      render: (_args, value) => text(renderHosts(value.hosts)),
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      return { hosts: engine.listHosts(args.query) }
    },
  })
}

/** The connect + environment-profile tool. */
export function sshConnectTool(engine: SshEngine) {
  return defineTool({
    name: 'ssh_connect',
    description:
      'Open (or reuse) the pooled SSH connection to a configured host and fetch its environment profile (distro / init / package manager / kernel / uptime). The host key must already be pinned (user did "test connection" in the SSH panel). Call before ssh_exec when you need to tailor commands to the target system.',
    parameters: {
      alias: { type: 'string', required: true, description: 'Host alias from ssh_list.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          alias: { type: 'string', required: true },
          error: { type: 'string' },
          profile: {
            type: 'object',
            additionalProperties: false,
            properties: {
              hostname: { type: 'string', required: true },
              user: { type: 'string', required: true },
              homeDir: { type: 'string' },
              distro: { type: 'string' },
              init: { type: 'string' },
              pkgManager: { type: 'string' },
              kernel: { type: 'string' },
              uptime: { type: 'string' },
            },
          },
        },
      },
      render: (_args, value) =>
        text(
          value.ok && value.profile
            ? `connected to ${value.alias}: ${value.profile.distro ?? 'unknown distro'} / init=${value.profile.init ?? '?'} / pkg=${value.profile.pkgManager ?? '?'} / kernel=${value.profile.kernel ?? '?'} / user=${value.profile.user} / home=${value.profile.homeDir ?? '?'}`
            : `connect ${value.alias} failed: ${value.error ?? 'unknown error'}`,
        ),
    },
    async execute(args) {
      try {
        await engine.connect(args.alias, false)
        const profile = await engine.profile(args.alias)
        return { ok: true, alias: args.alias, profile }
      } catch (err) {
        return { ok: false, alias: args.alias, error: (err as Error).message }
      }
    },
  })
}

/** The remote command-execution tool (approval-gated upstream). */
export function sshExecTool(engine: SshEngine) {
  return defineTool({
    name: 'ssh_exec',
    description:
      'Execute a shell command on a REMOTE SSH host by alias — the command runs on the remote host, never on this machine (use the local bash tool for local commands). Read-only commands run directly; mutating and dangerous commands require user approval; mutating operations on prod hosts are denied. Combine independent read-only queries into one command (e.g. with &&).',
    parameters: {
      alias: { type: 'string', required: true, description: 'Host alias from ssh_list.' },
      command: { type: 'string', required: true, description: 'The shell command to run remotely.' },
      timeoutMs: { type: 'integer', description: 'Timeout in milliseconds (default 60000, max 300000).' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          success: { type: 'boolean', required: true },
          exitCode: { oneOf: [{ type: 'integer' }, { type: 'null' }], required: true },
          timedOut: { type: 'boolean', required: true },
          stdout: { type: 'string', required: true },
          stderr: { type: 'string', required: true },
          durationMs: { type: 'integer', required: true },
          truncated: { type: 'boolean' },
          error: { type: 'string' },
        },
      },
      render: (_args, value) => text(renderExec(value)),
      presentationMeta: (_args, value) => ({
        output: value.stdout + (value.stderr ? (value.stdout ? '\n' : '') + value.stderr : ''),
        exitCode: value.exitCode,
        timedOut: value.timedOut,
        durationMs: value.durationMs,
        error: value.error ?? null,
      }),
    },
    presentCall: (args) => {
      const a = args as { alias?: unknown; command?: unknown }
      return {
        card: 'terminal',
        title: typeof a.command === 'string' ? a.command : 'ssh_exec',
        description: typeof a.alias === 'string' ? `远程主机 remote: ${a.alias}` : undefined,
      } as const
    },
    presentResult: (args, result) => {
      const a = args as { alias?: unknown }
      const meta = (result.meta ?? {}) as { output?: string; exitCode?: number | null; timedOut?: boolean; durationMs?: number; error?: string }
      return {
        card: 'terminal',
        title: typeof a.alias === 'string' ? `remote: ${a.alias}` : undefined,
        output: meta.output ?? undefined,
        exitCode: meta.timedOut || meta.error ? undefined : meta.exitCode ?? undefined,
      } as const
    },
    async execute(args, exec) {
      return engine.exec(args.alias, args.command, args.timeoutMs ?? 60000, exec.signal)
    },
  })
}
