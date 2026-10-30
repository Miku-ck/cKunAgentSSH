/**
 * exec-outcome-core.ts — 远程命令退出码三态判据的单一实现源（主进程与渲染端共享）
 *
 * 背景（S5-19）：`exitCode === null` 是「不知道」，不是失败。ssh2 只在收到
 * exit-status 时填这个数，`close` 事件不带它 —— 精简 sshd 不回 exit-status、
 * 或命令被 OOM kill 之后，读数都是 null。原实现按 `!== 0` 判失败并 unlink 归档，
 * 于是「其实已经写完」的那次把用户要的产物删掉了，还附一句「远程打包失败」。
 * 长任务（镜像 save/load）同样吃这条：写完文件却没回 exit-status 是真实存在的，
 * 按失败处理会让人删掉一份好备份重导一遍。
 *
 * 这条判据原先写着两份逐字相同的实现 —— `electron/ssh-manager.ts` 与
 * `src/stores/docker-core.ts` 各一份，两边注释都自述「与对侧同一口径」，而那正是
 * 漂移的前兆：改一侧忘另一侧时，渲染层把「未知」报成失败、主进程却保留产物，
 * 用户看到的结论取决于走了哪条腿。收敛路径与本仓既有家族一致
 * （ssh-error-core / transfer-error-core / port-core / github-accel-core）。
 */

export type ExecOutcome = 'ok' | 'failed' | 'unknown'

/** 0 = 成功；null = 没回退出码（未知，不许当成失败）；其余非零 = 失败 */
export function classifyExit(exitCode: number | null): ExecOutcome {
  if (exitCode === 0) return 'ok'
  if (exitCode === null) return 'unknown'
  return 'failed'
}

// ── 单次 exec 的 1MB 输出截断 ──
//
// 主进程累加到上限就停手并在流尾追加一句标记；渲染层据此告诉用户「这份列表可能缺行」，
// 而不是把「只看到前 N 条」当成主机/集群的真实状态。
//
// 识别按固定前缀匹配而非全等：这份文案两侧各持一份字面量，主进程侧曾把
// 「1MB 上限」改成「1M 字符上限」，全等 includes 从此永久失配 —— 截断提示条不再出现
// （原 Docker 页的用例是自造输入喂 isOutputTruncated，测不出这条跨层链路断裂，
// 靠独立复核发现）。收在这里之后两侧共用一处定义，漂移的根子也一并拔掉。
const EXEC_TRUNCATED_NOTE_PREFIX = '[输出已截断，超过 '

/** 主进程追加截断标记时用的那句文案（与渲染层的前缀匹配共用同一份字面量） */
export const EXEC_TRUNCATED_NOTE = `${EXEC_TRUNCATED_NOTE_PREFIX}1M 字符上限]`

export function isOutputTruncated(stdout: string, stderr = ''): boolean {
  return stdout.includes(EXEC_TRUNCATED_NOTE_PREFIX) || stderr.includes(EXEC_TRUNCATED_NOTE_PREFIX)
}
