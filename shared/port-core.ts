/**
 * port-core.ts — TCP 端口范围判据与提示文案的单一实现源（主进程与渲染端共享）
 *
 * 背景：同一个「1–65535 的整数」判据原先写着六份 —— gateway-store.ts 与 tunnel-store.ts
 * 各有一份逐字相同的 `isPort`，tunnel-manager.ts 两处、TunnelModal.vue 与 AccessModal.vue
 * 各把范围比较内联重写一遍。改上界只需改一处，漏改的代价是渲染层放行、主进程拒收，
 * 用户看到「填得对却存不下」；而文案已经先漂成了四个变体（带字段前缀的三种 + 裸的一句）。
 *
 * 范围口径：0 不是合法端口（保留），65535 是 u16 上界。读侧（tunnel-manager 监听前）
 * 与写侧（store 落盘前）都走这里，越界值一律拒绝，不夹到边界。
 */

const PORT_MIN = 1
const PORT_MAX = 65535

/** 是否为合法 TCP 端口号（非整数 / 越界 / 非 number 都算不合法） */
export function isPort(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= PORT_MIN && v <= PORT_MAX
}

/**
 * 端口越界的用户可读文案。
 * @param field 字段前缀，如 '本地监听' / '目标' / '远端绑定'；省略则给出通用句
 */
export function portRangeError(field = ''): string {
  return `${field}端口需为 ${PORT_MIN}–${PORT_MAX} 的整数`
}
