/**
 * 任务台消息流「要不要跟随到底」的贴底判定（纯函数，供 HomeWorkspace 与测试共用）。
 *
 * 流式期间 content/reasoning 每秒触发数十次滚动请求：无条件跟到底的话，用户
 * 上翻查看历史时滚动条会被持续抢走。跟随只在视口本就贴底（距底不足一个阈值）
 * 时进行。判定必须是纯数字运算 —— 前端单测统一跑在 node 环境（无 DOM），
 * 组件把 scrollTop / clientHeight / scrollHeight 读进来喂给它。
 */

/** 距底多少像素以内仍算「贴底」：放宽到几十像素的量级，流式一帧的小幅增长
 *  （通常只有几像素）不至于把跟随误判成放手；真往上翻过一段距离才停跟随。 */
export const STICKY_FOLLOW_GAP_PX = 80

/** 距底距离 = scrollHeight - clientHeight - scrollTop，不大于阈值即贴底。
 *  内容不足一屏时差值为负，同样算贴底（没有什么滚动条可抢）。 */
export function shouldFollowScroll(
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
  gapPx: number = STICKY_FOLLOW_GAP_PX,
): boolean {
  return scrollHeight - clientHeight - scrollTop <= gapPx
}
