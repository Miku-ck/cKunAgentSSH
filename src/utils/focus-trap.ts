/**
 * 模态焦点陷阱的纯计算部分。
 *
 * 设置弹窗与它内部再 Teleport 的编辑框都挂到 body 上，Tab 顺序必须只在本层
 * 模态里循环；实际 DOM 查找留在组件里，这里只算「下一站是第几项」。
 *
 * FOCUSABLE_SELECTOR 同时是 useModalFocusTrap 的查找口径：两份字符串各写一份时，
 * 一边收紧（比如补上 :not([disabled])）另一边不会跟着，键盘行为就按弹窗分叉了。
 */
export const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * current 不在集合内（-1 或越界）时：正向落到第一项，反向落到最后一项。
 * 没有可聚焦项时返回 -1，调用方应把焦点交还给模态容器。
 */
export function nextFocusIndex(count: number, current: number, dir: 1 | -1): number {
  if (count <= 0) return -1
  if (current < 0 || current >= count) return dir === 1 ? 0 : count - 1
  return (current + dir + count) % count
}
