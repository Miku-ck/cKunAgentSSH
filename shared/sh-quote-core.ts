/**
 * POSIX sh 单引号转义 —— 全仓唯一实现。
 *
 * 为什么在 shared/：SSH exec 通道没有 argv 形态，远程命令只能是字符串，路径/参数
 * 拼进命令行必须转义。此前 ssh-manager.ts、git-repos-core.ts、electron/main.ts
 * 各持一份同逻辑实现，随改随漂 —— 收敛到这里的单一出口（谁还要第四份，先来这里）。
 *
 * 适用场景（务必对号入座，用错 = 注入）：
 * - 目标 shell 是 POSIX sh 家族（bash / zsh / dash 等）；
 * - 产物是「单引号字面量」：单引号内部没有任何字符有特殊含义，嵌入的 `'` 本身
 *   按 `'\''`（关引号 + 转义引号 + 开引号）接续，与 git 的 sq_quote 同法；
 * - 不适用于 PowerShell（单引号内把 `'` 翻倍成 `''`，见 electron/shortcut-win.ts
 *   的 shellQuotePs）与 Windows cmd —— 那是另一套转义规则，别拿这两个函数去凑。
 */

/**
 * 包裹成 POSIX sh 单引号字面量：`shSingleQuote("a'b")` → `'a'\''b'`。
 * 远程命令拼接里的路径 / 分支名 / 提交信息一律经它包裹
 * （git-repos-core.ts 头注「命令安全」段描述的双保险中的转义层）。
 */
export function shSingleQuote(s: string): string {
  return `'${String(s).replace(/'/g, `'\\''`)}'`
}

/**
 * 只做内容体转义、**不加**外层引号：把整段脚本嵌进调用点自己拼的外层单引号时用
 * （如 `` `bash -lic '${shQuoteBody(script)}'` ``）。外层引号必须由调用方补上 ——
 * 只转义不包裹，漏了外层引号等于没转义。
 */
export function shQuoteBody(s: string): string {
  return String(s).replace(/'/g, `'\\''`)
}
