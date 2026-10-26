/**
 * tar 形态（解包目录直发）没有安装器：桌面/开始菜单快捷方式与 AUMID 注册
 * 由本模块在打包版启动时按需自建/自愈。
 *
 * 为什么必须存在（spec §4）：
 * - NSIS 的 createDesktopShortcut / AUMID 注入在 dir 产物下不存在了；
 * - Toast 通知归属依赖"开始菜单里有一个 AppUserModelID 属性正确的 .lnk"——
 *   光靠运行时 app.setAppUserModelId 只解决进程侧身份，UWP 归属检查仍会
 *   在 Start Menu 找不到匹配快捷方式时丢弃通知；
 * - WScript.Shell 能建 .lnk 但写不了 AUMID 扩展属性，须走 IPropertyStore COM 接口。
 *
 * 行为口径：非 win / 非打包态跳过。健康判定在 TS 侧完成、零 PS 开销：开始菜单
 * .lnk 在位，且 userData 指纹文件记录的"上次建链 exe"与当前 exe 一致。指纹失配
 * （典型成因：tar 解包目录被用户移动/改名 —— 这是直发形态的常态操作，只判
 * 「存在」会让 .lnk 永久指向旧路径且永不自愈）或 .lnk 缺失时唤起 PS 校验：逐处
 * 读 .lnk 的 TargetPath 与当前 exe 比对，漂移即重建。开始菜单项缺失/漂移都补建；
 * 桌面项在标记文件缺失时缺失/漂移都补建，标记已在位时只修漂移不凭空新建
 * （尊重用户删除）。失败只 warn——快捷方式建不出来绝不能影响应用本体运行。
 */
import { execFile } from 'child_process'
import { existsSync } from 'fs'
import { readFile, writeFile } from 'fs/promises'
import { join } from 'path'
import { app } from 'electron'
import { APP_USER_MODEL_ID } from './desktop-prefs'
import { resolvePowerShell } from './utils/powershell-path'
import { perfMark } from './utils/perf-mark'

/** 快捷方式显示名与 tar 包内可执行名（electron-builder.yml executableName）同源 */
export const SHORTCUT_NAME = 'cKunAgentSSH'
/** 桌面项"仅首启建一次"的标记文件（放 userData，不进发布物） */
export const DESKTOP_MARKER_FILE = '.desktop-shortcut-created'
/** 上次成功建链时的 exe 路径指纹（userData 内，不进发布物）。与桌面标记分开：
 *  marker 表达"桌面项建过、用户删了别再建"，指纹表达"所有 .lnk 为哪个 exe 而建"。 */
export const SHORTCUT_EXE_CACHE_FILE = '.shortcuts-built-for'

export interface ShortcutPlan {
  /** 是否需要唤起 PS 校验/重建；false 即健康快路径（本模块整体零开销） */
  needsPs: boolean
  /** 桌面段「.lnk 缺失」是否算待建（无 marker → 是；有 marker → repair-only） */
  desktopCreateIfMissing: boolean
}

/** PS 单引号字符串转义：内部 ' 翻倍。所有插入脚本的字符串必须过它 */
export function shellQuotePs(s: string): string {
  return `'${s.replace(/'/g, "''")}'`
}

/** 开始菜单 .lnk 的确定性路径（appData 可拼，TS 侧做 existsSync 幂等判断用） */
export function startMenuLnkPath(appData: string): string {
  return join(appData, 'Microsoft', 'Windows', 'Start Menu', 'Programs', `${SHORTCUT_NAME}.lnk`)
}

/**
 * 计划：指纹匹配且开始菜单 .lnk 在位 → 健康快路径（桌面项由同一次成功建链保证，
 * 见 ensureShellShortcuts 的写入时序）；否则唤起 PS 做逐项漂移校验。
 * 桌面真实路径 TS 不猜（OneDrive 重定向兼容），由 PS 内解析。
 */
export function planShortcuts(opts: {
  startMenuExists: boolean
  desktopMarkerExists: boolean
  exeCacheMatches: boolean
}): ShortcutPlan {
  return {
    needsPs: !(opts.exeCacheMatches && opts.startMenuExists),
    desktopCreateIfMissing: !opts.desktopMarkerExists,
  }
}

/** 读 exe 指纹；文件缺失/读失败一律按 null 处理（= 视为需要 PS 校验一次） */
export async function readShortcutExeCache(userData: string): Promise<string | null> {
  const p = join(userData, SHORTCUT_EXE_CACHE_FILE)
  if (!existsSync(p)) return null
  try {
    return (await readFile(p, 'utf-8')).trim()
  } catch {
    return null
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
 * 生成完整 PS 脚本。结构：先纯读取判定两处 .lnk 是否缺失/漂移（WScript.Shell 读
 * TargetPath 与当前 exe 比对，PS -ne 对字符串大小写不敏感，与 Windows 路径口径
 * 一致；对不存在的 .lnk 调 CreateShortcut 得到 TargetPath 为空的待建对象，同样
 * 落入"漂移"，故开始菜单段无需单独判存在）→ 确有待建项才 Add-Type（健康路径下
 * 脚本在判定段就空转退出，不付 C# 编译的开销）→ foreach 统一建链 + 写 AUMID。
 * 桌面/开始菜单真实路径在 PS 内用 [Environment]::GetFolderPath 解析（OneDrive
 * 重定向兼容）；$ErrorActionPreference='Stop' 任一步失败整体非零退出，由 TS 捕获。
 */
export function buildShortcutScript(plan: ShortcutPlan, execPath: string, aumid: string): string {
  const qLnkName = shellQuotePs(`${SHORTCUT_NAME}.lnk`)
  const lines: string[] = [
    "$ErrorActionPreference = 'Stop'",
    `$exe = ${shellQuotePs(execPath)}`,
    `$aumid = ${shellQuotePs(aumid)}`,
    `$dtCreateIfMissing = ${plan.desktopCreateIfMissing ? '$true' : '$false'}`,
    '$pending = @()',
    `$smDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'`,
    `$smLnk = Join-Path $smDir ${qLnkName}`,
    `if (-not (Test-Path -LiteralPath $smLnk) -or ((New-Object -ComObject WScript.Shell).CreateShortcut($smLnk).TargetPath -ne $exe)) { $pending += $smDir }`,
    `$dtDir = [Environment]::GetFolderPath('Desktop')`,
    `$dtLnk = Join-Path $dtDir ${qLnkName}`,
    'if (Test-Path -LiteralPath $dtLnk) {',
    '  if (((New-Object -ComObject WScript.Shell).CreateShortcut($dtLnk).TargetPath -ne $exe)) { $pending += $dtDir }',
    '} elseif ($dtCreateIfMissing) { $pending += $dtDir }',
    'if ($pending.Count -gt 0) {',
    `  Add-Type -TypeDefinition ${shellQuotePs(AUMID_CSHARP)}`,
    '  foreach ($dir in $pending) {',
    '    New-Item -ItemType Directory -Force -Path $dir | Out-Null',
    `    $lnk = Join-Path $dir ${qLnkName}`,
    '    $ws = New-Object -ComObject WScript.Shell',
    '    $sc = $ws.CreateShortcut($lnk)',
    '    $sc.TargetPath = $exe',
    '    $sc.WorkingDirectory = Split-Path $exe',
    `    $sc.Description = ${shellQuotePs(SHORTCUT_NAME)}`,
    // 不用 "$exe,0" 双引号内插：路径里出现 $ 字符会被 PS 当变量展开
    '    $sc.IconLocation = ($exe + ",0")',
    '    $sc.Save()',
    '    [CKunLnkAumid]::Set($lnk, $aumid)',
    '  }',
    '}',
  ]
  return lines.join('\n')
}

/**
 * 每次启动调用（main.ts fire-and-forget 接线）。健康快路径零 PS 开销；唤起 PS 且
 * 成功后回写桌面标记与 exe 指纹。全程不抛：跳过条件早退、PS 失败仅 warn。
 */
export async function ensureShellShortcuts(): Promise<void> {
  if (process.platform !== 'win32' || !app.isPackaged) return
  const userData = app.getPath('userData')
  const exe = app.getPath('exe')
  const plan = planShortcuts({
    startMenuExists: existsSync(startMenuLnkPath(app.getPath('appData'))),
    desktopMarkerExists: existsSync(join(userData, DESKTOP_MARKER_FILE)),
    exeCacheMatches: (await readShortcutExeCache(userData)) === exe,
  })
  if (!plan.needsPs) return
  const script = buildShortcutScript(plan, exe, APP_USER_MODEL_ID)
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
  if (plan.desktopCreateIfMissing) {
    // 标记异步写、失败仅 warn：下次启动多建一次桌面项而已，无害
    void writeFile(join(userData, DESKTOP_MARKER_FILE), new Date().toISOString(), 'utf-8')
      .catch((e) => console.warn('[shortcut] 桌面标记写入失败:', e))
  }
  // 指纹同样异步写、失败仅 warn：最坏代价是下次启动多做一次 PS 空转校验
  void writeFile(join(userData, SHORTCUT_EXE_CACHE_FILE), exe, 'utf-8')
    .catch((e) => console.warn('[shortcut] exe 指纹缓存写入失败:', e))
}
