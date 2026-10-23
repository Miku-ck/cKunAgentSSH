/**
 * tar 形态（解包目录直发）没有安装器：桌面/开始菜单快捷方式与 AUMID 注册
 * 由本模块在打包版每次启动时自建。
 *
 * 为什么必须存在（spec §4）：
 * - NSIS 的 createDesktopShortcut / AUMID 注入在 dir 产物下不存在了；
 * - Toast 通知归属依赖"开始菜单里有一个 AppUserModelID 属性正确的 .lnk"——
 *   光靠运行时 app.setAppUserModelId 只解决进程侧身份，UWP 归属检查仍会
 *   在 Start Menu 找不到匹配快捷方式时丢弃通知；
 * - WScript.Shell 能建 .lnk 但写不了 AUMID 扩展属性，须走 IPropertyStore COM 接口。
 *
 * 行为口径：非 win / 非打包态跳过；开始菜单项每次启动幂等补建（可能被用户
 * 删掉），桌面项仅首启一次（userData 标记文件控制，用户主动删除后不强求重建）；
 * 失败只 warn——快捷方式建不出来绝不能影响应用本体运行。
 */
import { execFile } from 'child_process'
import { existsSync } from 'fs'
import { writeFile } from 'fs/promises'
import { join } from 'path'
import { app } from 'electron'
import { APP_USER_MODEL_ID } from './desktop-prefs'
import { resolvePowerShell } from './utils/powershell-path'
import { perfMark } from './utils/perf-mark'

/** 快捷方式显示名与 tar 包内可执行名（electron-builder.yml executableName）同源 */
export const SHORTCUT_NAME = 'cKunAgentSSH'
/** 桌面项"仅首启建一次"的标记文件（放 userData，不进发布物） */
export const DESKTOP_MARKER_FILE = '.desktop-shortcut-created'

export interface ShortcutPlan {
  createStartMenu: boolean
  createDesktop: boolean
}

/** PS 单引号字符串转义：内部 ' 翻倍。所有插入脚本的字符串必须过它 */
export function shellQuotePs(s: string): string {
  return `'${s.replace(/'/g, "''")}'`
}

/** 开始菜单 .lnk 的确定性路径（appData 可拼，TS 侧做 existsSync 幂等判断用） */
export function startMenuLnkPath(appData: string): string {
  return join(appData, 'Microsoft', 'Windows', 'Start Menu', 'Programs', `${SHORTCUT_NAME}.lnk`)
}

/** 计划：已存在 → 不建。桌面实际路径 TS 不猜（OneDrive 重定向兼容），由 PS 内解析 */
export function planShortcuts(opts: { startMenuExists: boolean; desktopMarkerExists: boolean }): ShortcutPlan {
  return {
    createStartMenu: !opts.startMenuExists,
    createDesktop: !opts.desktopMarkerExists,
  }
}

/**
 * 写 AUMID 的 C# 辅助类（Add-Type 内嵌）。要点：
 * - WScript.Shell 不支持扩展属性，须 SHGetPropertyStoreFromParsingName 打开
 *   .lnk 的属性存储（flags=3 → GPS_READWRITE）；
 * - PKEY_AppUserModel_ID = fmtid 9F4C2855-… + pid 5；
 * - x64 下 PropVariant 前 8 字节是 vt/填充，值指针在偏移 8（VT_LPWSTR=31）。
 */
const AUMID_CSHARP = `
using System;
using System.Runtime.InteropServices;
public static class CKunLnkAumid {
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHGetPropertyStoreFromParsingName(
    [MarshalAs(UnmanagedType.LPWStr)] string path, IntPtr pbc, int flags,
    ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
  [ComImport, Guid("886d8eea-8cf2-4446-8d02-cdba1dbdcf91"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPropertyStore {
    void GetCount(out uint c); void GetAt(uint i, out PropertyKey k);
    void GetValue(ref PropertyKey k, out PropVariant v); void GetCaps(out uint c);
    void SetValue(ref PropertyKey k, ref PropVariant v); void Commit();
  }
  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PropertyKey { public Guid fmtid; public uint pid; }
  [StructLayout(LayoutKind.Explicit)]
  struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
  }
  public static void Set(string lnk, string aumid) {
    Guid iid = new Guid("886d8eea-8cf2-4446-8d02-cdba1dbdcf91");
    IPropertyStore store;
    SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 3, ref iid, out store);
    PropertyKey pk = new PropertyKey {
      fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    PropVariant pv = new PropVariant { vt = 31, p = Marshal.StringToCoTaskMemUni(aumid) };
    store.SetValue(ref pk, ref pv);
    store.Commit();
    Marshal.FreeCoTaskMem(pv.p);
  }
}
`.trim()

/**
 * 生成完整 PS 脚本：按 plan 拼段；每段 = WScript.Shell 建 .lnk（图标即 exe 内嵌
 * 图标，tar 解包后 resources 布局不变）+ CKunLnkAumid::Set 写通知归属。
 * 桌面/开始菜单真实路径在 PS 内用 [Environment]::GetFolderPath 解析（OneDrive
 * 重定向兼容）；$ErrorActionPreference='Stop' 任一步失败整体非零退出，由 TS 捕获。
 */
export function buildShortcutScript(plan: ShortcutPlan, execPath: string, aumid: string): string {
  const lines: string[] = ["$ErrorActionPreference = 'Stop'"]
  const targets: string[] = []
  if (plan.createStartMenu) {
    // GetFolderPath('StartMenu') 返回 开始菜单\Programs 的上一级，Programs 需拼上
    targets.push(`Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'`)
  }
  if (plan.createDesktop) {
    targets.push(`[Environment]::GetFolderPath('Desktop')`)
  }
  if (targets.length === 0) return ''
  lines.push(`Add-Type -TypeDefinition ${shellQuotePs(AUMID_CSHARP)}`)
  for (const dirExpr of targets) {
    lines.push(
      `$dir = ${dirExpr}`,
      `New-Item -ItemType Directory -Force -Path $dir | Out-Null`,
      `$lnk = Join-Path $dir ${shellQuotePs(`${SHORTCUT_NAME}.lnk`)}`,
      `$ws = New-Object -ComObject WScript.Shell`,
      `$sc = $ws.CreateShortcut($lnk)`,
      `$sc.TargetPath = ${shellQuotePs(execPath)}`,
      `$sc.WorkingDirectory = Split-Path ${shellQuotePs(execPath)}`,
      `$sc.Description = ${shellQuotePs(SHORTCUT_NAME)}`,
      `$sc.IconLocation = ${shellQuotePs(`${execPath},0`)}`,
      `$sc.Save()`,
      `[CKunLnkAumid]::Set($lnk, ${shellQuotePs(aumid)})`,
    )
  }
  return lines.join('\n')
}

/**
 * 每次启动调用（main.ts fire-and-forget 接线）。全程不抛：跳过条件早退、
 * 幂等零开销、PS 失败仅 warn。成功且本次建过桌面项 → 写标记文件。
 */
export async function ensureShellShortcuts(): Promise<void> {
  if (process.platform !== 'win32' || !app.isPackaged) return
  const markerPath = join(app.getPath('userData'), DESKTOP_MARKER_FILE)
  const plan = planShortcuts({
    startMenuExists: existsSync(startMenuLnkPath(app.getPath('appData'))),
    desktopMarkerExists: existsSync(markerPath),
  })
  if (!plan.createStartMenu && !plan.createDesktop) return
  const script = buildShortcutScript(plan, app.getPath('exe'), APP_USER_MODEL_ID)
  try {
    await new Promise<void>((resolve, reject) => {
      execFile(
        resolvePowerShell(),
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
        { timeout: 15000, windowsHide: true },
        (err) => (err ? reject(err) : resolve()),
      )
    })
  } catch (e) {
    // ConstrainedLanguage/AppLocker 环境下 Add-Type 可能被策略拒绝——只 warn，
    // 通知归属退回运行时 setAppUserModelId（多数场景仍可用），本体不受影响
    console.warn('[shortcut] 快捷方式创建失败（不影响应用运行）:', e)
    return
  }
  perfMark('shortcuts: ensured')
  if (plan.createDesktop) {
    // 标记异步写、失败仅 warn：下次启动多建一次桌面项而已，无害
    void writeFile(markerPath, new Date().toISOString(), 'utf-8')
      .catch((e) => console.warn('[shortcut] 桌面标记写入失败:', e))
  }
}
