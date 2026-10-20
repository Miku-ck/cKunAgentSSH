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
