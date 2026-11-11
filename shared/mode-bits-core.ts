/**
 * 权限位 ↔ rwx 字符串的互逆转换（S5-27）。
 *
 * 为什么放 shared：这两个函数原本分处两个进程 —— 主进程的 modeToString 把
 * SFTP attrs.mode 渲染成 'drwxr-xr-x' 交给渲染层，渲染层的 modeTextToBits 再把
 * 字符串反推回数字喂给 chmod 弹窗。两者必须严格互逆，否则用户在弹窗里点
 * 「应用」时高位权限（SUID/SGID/sticky）会被静默清零，界面看不出任何异常。
 *
 * 互逆性不是「看起来对」就行的性质：一旦两处实现分叉（渲染认 s/S/t/T、反推
 * 只认 x/-），高位就会在每次编辑权限时被悄悄抹掉。抽到 shared 让两边共用
 * 同一份实现，配合 shared/mode-bits.test.ts 的往返测试把这条不变量钉死。
 *
 * 渲染形态与 ls 一致：有 x 才把 s/t 落进 x 位（保留执行位），无 x 用大写 S/T。
 */

/** 权限位掩码：只取 12 位（含 SUID/SGID/sticky），去掉文件类型位。 */
export const PERM_BITS_MASK = 0o7777

/** SUID / SGID / sticky 三位的掩码 */
export const SUID_BIT = 0o4000
export const SGID_BIT = 0o2000
export const STICKY_BIT = 0o1000

/**
 * 数值 mode（含文件类型位）→ 'drwxr-xr-x' 形式。
 * 只渲染权限部分；类型位由调用方按需传入（SFTP 条目已另存 isDirectory）。
 */
export function modeToRwxString(mode: number): string {
  const typeMap: Record<number, string> = {
    0o140000: 's', 0o120000: 'l', 0o100000: '-',
    0o060000: 'b', 0o020000: 'c', 0o040000: 'd', 0o010000: 'p',
  }
  let typeChar = '?'
  for (const k of Object.keys(typeMap)) {
    if ((mode & 0o170000) === Number(k)) {
      typeChar = typeMap[Number(k)]
      break
    }
  }
  const permChars = ['r', 'w', 'x']
  let s = typeChar
  for (let i = 0; i < 9; i++) {
    s += (mode & (1 << (8 - i))) ? permChars[i % 3] : '-'
  }
  // 高位三个特殊位覆写 x 位（下标 3/6/9 即 rwx 三段各自的 x 位）
  if ((mode & SUID_BIT) !== 0) s = setCharAt(s, 3, s[3] === 'x' ? 's' : 'S')
  if ((mode & SGID_BIT) !== 0) s = setCharAt(s, 6, s[6] === 'x' ? 's' : 'S')
  if ((mode & STICKY_BIT) !== 0) s = setCharAt(s, 9, s[9] === 'x' ? 't' : 'T')
  return s
}

/**
 * rwx 字符串（如 'drwxr-xr-x' / '-rwsr-xr-x'）→ 权限位（含高位）。
 *
 * 只认 r/w/x/s/S/t/T 七个合法字符；其余（`-`、`?`、乱码）一律按「无权限」处理。
 * 原实现用 `body[i] !== '-'` 一刀切 —— 类型位未知时 modeToString 会渲染出
 * `??????????`，反推回来 9 个 `?` 全被当成有权限，得出 0o777 这种凭空的权限。
 */
export function rwxStringToMode(modeText: string): number {
  if (!modeText || modeText.length < 10) return 0
  const body = modeText.slice(1, 10)
  let bits = 0
  const masks = [0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001]
  // 小写 s/t 是「有 x + 特殊位」，大写 S/T 是「没有 x + 特殊位」。只有小写
  // 才算执行位，否则 chmod 一个 SUID 脚本会凭空把 x 也打开。
  const valid = new Set(['r', 'w', 'x', 's', 't'])
  for (let i = 0; i < 9; i++) {
    if (valid.has(body[i])) bits |= masks[i]
  }
  // 第 3/6/9 位上的 s/S → SUID/SGID，t/T → sticky（大小写都认，只是不进 masks）
  if (body[2] === 's' || body[2] === 'S') bits |= SUID_BIT
  if (body[5] === 's' || body[5] === 'S') bits |= SGID_BIT
  if (body[8] === 't' || body[8] === 'T') bits |= STICKY_BIT
  return bits
}

/** 把字符串第 idx 位换成 ch。 */
function setCharAt(s: string, idx: number, ch: string): string {
  return s.slice(0, idx) + ch + s.slice(idx + 1)
}
