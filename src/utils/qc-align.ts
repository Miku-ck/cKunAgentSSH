/**
 * 快捷命令条（QuickCommandBar）顶线与 AI 面板输入框底线的对齐算式
 * —— 纯函数，便于单测锁住方向（历史事故：±1 写反，差 2px，见下）。
 *
 * 两栏同为 .ssh-view 的 stretch 子项，底边同 y = baseBottom：
 *
 *   .ssh-main（左栏）                   .side-chat-panel（右栏）
 *   ┌────────────────────┐             ┌──────────────────────────┐
 *   │   …终端…           │             │   …消息列表…             │
 *   ├────────────────────┤             ├──────────────────────────┤
 *   │  .quick-command-bar│             │  textarea 输入区         │
 *   │  高 = 本函数返回值 │             ├──────────────────────────┤ ← .input-container
 *   │  （自带 1px        │             │  .input-toolbar          │   的 border-bottom
 *   │   border-top）     │             │  4px + 24px + 4px        │   （1px 分隔线）
 *   └────────────────────┘             └──────────────────────────┘
 *                  baseBottom（窗口内容底边，两栏共用同一个 y）
 *
 * 目标：QC 的 border-top 与右栏那条 border-bottom **落在同一像素行**，
 * 使左右两栏的分隔线在视觉上连成一条。
 *
 * 为什么不能写成「工具栏高度 - 1」：`--qc-h` 是 QC 元素的**总高**
 * （全局 box-sizing: border-box，含自身那 1px 的上边框）。取 32 - 1 = 31 时，
 * QC 的 border-top 落在距底 30~31px，而右栏那条线在 32~33px，
 * 两条线错开约 2px —— 看着像没对齐，实际是算式把 1px 减错了方向。
 */

/** 分隔线宽度（.input-container 的 border-bottom），也是 QC 自身 border-top 的宽度 */
export const SEPARATOR_PX = 1

/**
 * 计算 QC 元素应有的高度，使其 border-top 与右栏分隔线重合。
 *
 * 返回 h（> 0）后满足不变量：`baseBottom - h === Math.round(containerBottom) - 1`
 * —— 即 QC 的上边沿（border-top 上沿）与分隔线上沿同 y。
 *
 * @param baseBottom      底边基线 y（.side-chat-panel 与 .ssh-main 共用的底边）
 * @param containerBottom `.input-container` 的 getBoundingClientRect().bottom。
 *                        border 计入盒模型，所以这是分隔线的**下沿**，不是上沿。
 * @returns              QC 的 height（px）。**0 表示无效**（未布局或几何不合理），
 *                       调用方必须跳过写入 —— 这不是"高度为 0"的意思。
 */
export function qcHeightForAlign(baseBottom: number, containerBottom: number): number {
  // 先挡无效输入：元素还没铺开时 rect 是 0，若照常计算会得到 ≈ 整个面板高度
  // 的返回值（baseBottom - (0 - 1)），调用方的 `h <= 0` 检查根本拦不住正数，
  // 于是 QC 被撑满整个左栏。分隔线落在基线之下同样是几何上不可能的值。
  if (containerBottom <= 0 || containerBottom > baseBottom) return 0

  // 分隔线自身高度要先从下沿减掉，得到上沿；再与基线作差。
  // 两边各自取整后作差（而不是先作差再取整），避免两次舍入把 1px 的线吃掉。
  const separatorTop = Math.round(containerBottom) - SEPARATOR_PX
  const h = Math.round(baseBottom) - separatorTop
  return h > 0 ? h : 0
}
