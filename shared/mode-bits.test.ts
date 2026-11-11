/**
 * 权限位渲染 ↔ 反推的往返一致性（S5-27）。
 *
 * 这两个函数分处两个进程、两条通道：
 *   主进程 ssh-manager.ts       → modeToRwxString  → SftpEntry.mode 字符串
 *   渲染层 SftpView.vue          → rwxStringToMode  → chmod 弹窗的 currentBits
 *
 * 它们必须严格互逆 —— 否则用户在弹窗里点「应用」时，高位权限（SUID/SGID/sticky）
 * 会被静默清零，界面看不出任何异常。原实现：渲染只读低 9 位，反推也只认 9 位，
 * `chmod 4755` 的文件显示成 `-rwxr-xr-x`。
 *
 * 现在两处共用 shared/mode-bits-core.ts 同一份实现，这里的往返测试是那条
 * 不变量的守卫：一旦有人改动其中一侧的解析口径（比如只认小写 s 不认大写 S），
 * 往返立刻失败。
 */
import { describe, expect, it } from 'vitest'
import { modeToRwxString, rwxStringToMode } from './mode-bits-core'

/** 只取权限位（含高位），去掉文件类型位 —— 列表里类型位是渲染成首字符的。 */
const permBits = (mode: number) => mode & 0o7777
const withFileBit = (m: number) => 0o100000 | m

describe('modeToRwxString 渲染 SUID/SGID/sticky', () => {
  it('普通文件不带高位', () => {
    expect(modeToRwxString(withFileBit(0o644))).toBe('-rw-r--r--')
    expect(modeToRwxString(withFileBit(0o755))).toBe('-rwxr-xr-x')
  })

  it('SUID/SGID/sticky 落进 x 位（有 x 用小写，与 ls 一致）', () => {
    expect(modeToRwxString(withFileBit(0o4755))).toBe('-rwsr-xr-x')   // SUID
    expect(modeToRwxString(withFileBit(0o2755))).toBe('-rwxr-sr-x')   // SGID
    expect(modeToRwxString(withFileBit(0o1777))).toBe('-rwxrwxrwt')   // sticky（/tmp）
    expect(modeToRwxString(withFileBit(0o7777))).toBe('-rwsrwsrwt')   // 三个一起
  })

  it('无 x 位时用大写 S/T（ls 的形态）', () => {
    expect(modeToRwxString(withFileBit(0o4644))).toBe('-rwSr--r--')
    expect(modeToRwxString(withFileBit(0o2644))).toBe('-rw-r-Sr--')
    expect(modeToRwxString(withFileBit(0o1666))).toBe('-rw-rw-rwT')
  })

  it('目录的类型位不被高位影响', () => {
    expect(modeToRwxString(0o041777)).toBe('drwxrwxrwt')
    expect(modeToRwxString(0o040755)).toBe('drwxr-xr-x')
  })

  it('符号链接与套接字等类型位各自渲染', () => {
    expect(modeToRwxString(0o120777)).toBe('lrwxrwxrwx')
    expect(modeToRwxString(0o140755)).toBe('srwxr-xr-x')
  })
})

describe('rwxStringToMode 往返', () => {
  it('常见权限值往返一致', () => {
    for (const m of [0o644, 0o755, 0o600, 0o777, 0o000, 0o444, 0o111]) {
      expect(permBits(rwxStringToMode(modeToRwxString(withFileBit(m))))).toBe(m)
    }
  })

  it('高位权限往返一致（原实现会在这里静默清零）', () => {
    for (const m of [0o4755, 0o2755, 0o1777, 0o7777, 0o4644, 0o2644, 0o1666, 0o4000, 0o2000, 0o1000]) {
      expect(permBits(rwxStringToMode(modeToRwxString(withFileBit(m))))).toBe(m)
    }
  })

  it('解析 s/S/t/T 时同时保留 x 位（s 不等于「只有 s 没有 x」）', () => {
    expect(rwxStringToMode('-rwsr-xr-x')).toBe(0o4755)
    expect(rwxStringToMode('-rwSr--r--')).toBe(0o4644)
    expect(rwxStringToMode('-rwxrwxrwt')).toBe(0o1777)
    expect(rwxStringToMode('-rw-rw-rwT')).toBe(0o1666)
  })

  it('目录串往返保留 sticky', () => {
    expect(rwxStringToMode('drwxrwxrwt')).toBe(0o1777)
  })

  it('脏输入不抛异常、按位解析', () => {
    expect(rwxStringToMode('')).toBe(0)
    expect(rwxStringToMode('-rwx')).toBe(0)          // 短于 10 位
    expect(rwxStringToMode('garbage!!')).toBe(0)     // 无权限字符
    expect(rwxStringToMode('??????????')).toBe(0)    // 类型位未知
  })
})
