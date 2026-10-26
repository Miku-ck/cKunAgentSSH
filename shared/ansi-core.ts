/**
 * ANSI 转义序列剥离 —— 全仓唯一强实现（CSI + OSC + 其他两字节 ESC 序列）。
 *
 * 为什么收敛：曾有四份并存 —— git-repos-core 只剥 SGR 一类 CSI（`\x1b[?25h` 这类
 * 带 ? 的私有模式剥不掉），useCompletion / ssh-handshake / useAgentDeliver 各持一份
 * CSI+OSC 组合。剥不净的残码轻则让报错文案带乱码（git stderr 的 remote: 颜色码），
 * 重则打断哨兵/标记匹配（握手探针、工具输出交付）。
 *
 * 正则依据（ECMA-48 / xterm 同口径）：
 * - CSI：`ESC [` + 参数位（0x30–0x3F，含 SGR 的 `;` 与私有模式的 `?` `>`）+
 *   中间位（0x20–0x2F）+ 终止位（0x40–0x7E）—— 颜色、光标移动、`\x1b[?25h`
 *   一类带参私有模式全覆盖；
 * - OSC：`ESC ]` + payload + 终止符（BEL 0x07 或 ST `ESC \`）。payload 一旦出现
 *   ESC 即视为序列未终止、不再吞并 —— 宁可留残码，也不把后续正文（可能正是
 *   要匹配的哨兵/标记）一并吃掉；
 * - 其他两字节 ESC 序列（ESC M / ESC D / ESC H 等 Fe 类）：同理剥掉。`ESC 7`、
 *   `ESC =` 这类 0x30–0x3F 段不在剥除范围 —— 回显场景未观测到它们打断匹配，
 *   为不过度吞并先不扩。
 */
export function stripAnsi(s: string): string {
  return s
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '') // CSI（ECMA-48 参数/中间/终止位）
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '') // OSC（BEL 或 ST 终止）
    .replace(/\x1b[@-Z\\-_]/g, '') // 其他两字节 ESC（Fe 类，如 ESC M）
}
