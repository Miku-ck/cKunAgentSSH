/**
 * 诊断包的纯逻辑（无 IO、无 electron 依赖）。
 *
 * 诊断包会离开本机（用户发给我们、贴进 issue），所以这里的第一职责是**脱敏**：
 * 日志里出现过 token=…、Authorization: Bearer …、代理 URL 里的口令、sk-/ghp_ 令牌，
 * 打包时必须按同一套规则抹掉。第二职责是控制体积：main.log 三份共 15MB，全塞进包
 * 没有意义，日志只保留尾部，崩溃转储只收最近的几份。
 *
 * 真正的文件读写与 tar 在 electron/diagnostics.ts，那边靠注入点测。
 */

/** 一个包最多带多少个崩溃转储、每份日志最多留多少字节 */
export const DIAG_MAX_CRASH_DUMPS = 5
export const DIAG_MAX_LOG_BYTES = 4 * 1024 * 1024

/**
 * 凭据抹除规则。
 *
 * 注意 `\b(key)\b\s*[:=]` 这一族必须要求"值至少 4 个非空字符"，否则
 * `password: ` 这类空值也会被塞进 ***，反而让日志更难读；而 `Bearer` 与
 * 各家前缀令牌单独成规则，因为它们常出现在句子中间（`status=401 auth=Bearer abc`）。
 */
const REDACT_RULES: Array<[RegExp, string]> = [
  // 顺序重要：Bearer / 前缀令牌先抹。否则 `Authorization: Bearer eyJ…` 会先被下面的
  // 键值规则吃掉 "Bearer" 这一段，令牌本体就停留在 *** 后面。
  [/(\bBearer)\s+[A-Za-z0-9._~+/-]{8,}=*/g, '$1 ***'],
  [/\bsk-[A-Za-z0-9_-]{8,}/g, 'sk-***'],
  [/\bgh[pousr]_[A-Za-z0-9]{16,}/g, 'ghp_***'],
  // URL 里的 userinfo（http://user:pw@proxy:3128）
  [/(\b[a-z][a-z0-9+.-]*:\/\/)[^\s/@]+@/gi, '$1***@'],
  // key=value / key: value / "key": "value"（env、JSON、日志行三种写法）。
  // 键名允许带前缀词：真实日志里是 `ELECTRON_BRIDGE_TOKEN=`、`CKUN_API_KEY=`，
  // 只认裸 `token` 会漏掉它们（下划线是单词字符，`\btoken` 在 `_TOKEN` 里不成立）。
  // 值前排除 `Bearer`：那一段已由上面处理，否则会变成 `Authorization: *** ***`。
  [
    /\b([A-Za-z0-9_]*(?:token|secret|password|passwd|pwd|api[_-]?key|apikey|authorization|cookie)[A-Za-z0-9_]*)\b(\s*[:=]\s*)(["']?)(?!Bearer\b)([^\s"',;]{4,})\3/gi,
    '$1$2$3***$3',
  ],
]

/** 抹掉日志/环境值里的凭据。幂等：对已抹过的文本再跑一遍结果不变。 */
export function redactSecrets(text: string): string {
  let out = text
  for (const [re, rep] of REDACT_RULES) out = out.replace(re, rep)
  return out
}

/**
 * 只保留尾部 maxBytes 字节（日志排障看最近发生了什么，头部是启动时的样板）。
 * 按字节切可能把多字节字符劈开，这里顺手丢掉开头那个替换符，避免包里出现 U+FFFD。
 */
export function tailText(text: string, maxBytes: number): string {
  const buf = Buffer.from(text, 'utf8')
  if (buf.length <= maxBytes) return text
  const tail = buf.subarray(buf.length - maxBytes).toString('utf8').replace(/^\uFFFD+/, '')
  return tail
}

/** `ckunagentssh-diag-<version>-<YYYYMMDD-HHmmss>.tar.gz`（文件名里不能有冒号） */
export function diagnosticsFileName(version: string, now: Date): string {
  const p = (n: number, w = 2) => String(n).padStart(w, '0')
  const stamp = `${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`
  return `ckunagentssh-diag-${version || '0.0.0'}-${stamp}.tar.gz`
}

/** 诊断包里允许出现的环境变量：只读、且排除一切可能带凭据的键 */
const ENV_ALLOW = /^(CKUN_|ELECTRON_|HTTPS?_PROXY$|HTTP_PROXY$|NO_PROXY$|ALL_PROXY$|LANG$|TZ$|TEMP$|TMP$)/i
/** 哪怕在允许名单里也一律不出包（值就是凭据本身） */
const ENV_DENY = /TOKEN|SECRET|PASSWORD|PASSWD|KEY|AUTH|CREDENTIAL/i

/**
 * 环境变量白名单快照。
 *
 * 不整份 dump：`process.env` 里有 bridge token、代理口令、各家 API key，诊断包是
 * 要发出去的东西。键名过白名单后再用 redactSecrets 兜一层值（代理 URL 里可能带口令）。
 */
export function envSnapshot(env: Record<string, string | undefined>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(env)) {
    if (typeof v !== 'string' || !ENV_ALLOW.test(k)) continue
    if (ENV_DENY.test(k)) { out[k] = '***'; continue }
    out[k] = redactSecrets(v)
  }
  return out
}

export interface LogFileStat {
  name: string
  bytes: number
}

/**
 * 导出结果。放在这里而不是 electron/diagnostics.ts：渲染层的 env.d.ts 也要引用它，
 * 而那边不该为一个类型去 import 主进程模块（会连带 node 专有类型）。
 */
export interface DiagnosticsResult {
  ok: boolean
  /** 产物绝对路径；失败时为 null */
  path: string | null
  bytes: number
  logs: LogFileStat[]
  /** 包内崩溃转储份数 */
  crashDumps: number
  error: string | null
}

export interface SystemInfoInput {
  appVersion: string
  electronVersion: string
  chromeVersion: string
  nodeVersion: string
  platform: string
  arch: string
  locale: string
  pid: number
  startedAt: string
  appDir: string
  userDataDir: string
  logs: LogFileStat[]
  /** 内嵌 CPython 运行时的版本；拿不到就是 null（不猜） */
  runtimeVersion: string | null
  /** 实际使用的解释器路径（内嵌 runtime 或系统 python）：排障第一件事是确认走的是哪一个 */
  runtimePath: string | null
  /** 自更新的通道与阶段（已配置的通道地址会过 redactSecrets） */
  updateFeedUrl: string
  updatePhase: string
  crashDumps: number
  env: Record<string, string | undefined>
}

/**
 * system.json 的对象形态。字段名刻意扁平：支持人员要能一眼扫到"哪个版本、什么平台、
 * 几个崩溃转储"，而不是在一堆嵌套里找。
 */
export function buildSystemInfo(input: SystemInfoInput): Record<string, unknown> {
  return {
    app: {
      version: input.appVersion,
      electron: input.electronVersion,
      chrome: input.chromeVersion,
      node: input.nodeVersion,
      platform: input.platform,
      arch: input.arch,
      locale: input.locale,
      pid: input.pid,
      startedAt: input.startedAt,
    },
    paths: {
      appDir: input.appDir,
      userDataDir: input.userDataDir,
    },
    runtime: {
      python: input.runtimeVersion,
      path: input.runtimePath,
    },
    update: {
      feedUrl: redactSecrets(input.updateFeedUrl),
      phase: input.updatePhase,
    },
    logs: input.logs,
    crashDumps: input.crashDumps,
    env: envSnapshot(input.env),
  }
}
