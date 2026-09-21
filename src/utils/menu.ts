/**
 * 浮层菜单的开向校正：光标贴近视口边缘时翻到另一侧，避免菜单开到屏幕外。
 * 各调用方按"菜单项数 × 单项高度"估一个尺寸传进来即可。
 */
export function menuTop(clientY: number, estimatedHeight: number): number {
  return clientY + estimatedHeight > window.innerHeight ? clientY - estimatedHeight : clientY
}

export function menuLeft(clientX: number, estimatedWidth: number): number {
  return clientX + estimatedWidth > window.innerWidth ? clientX - estimatedWidth : clientX
}
