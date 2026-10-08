import { homedir } from 'node:os'
import { join } from 'node:path'

/**
 * DSH user-data root: $DSH_HOME when set, else ~/.dsh. The plugin keeps its
 * host registry and known-hosts store here, next to DSH's own profile data.
 */
export function dshHome(): string {
  return process.env.DSH_HOME?.trim() || join(homedir(), '.dsh')
}

/** Absolute path of one plugin data file under the DSH home. */
export function dataFile(name: string): string {
  return join(dshHome(), name)
}

/** Expand a leading ~ (or ~user) in a path, platform-style. */
export function expandHome(p: string): string {
  if (p === '~') return homedir()
  if (p.startsWith('~/') || p.startsWith('~\\')) return join(homedir(), p.slice(2))
  return p
}
