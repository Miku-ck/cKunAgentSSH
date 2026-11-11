/**
 * Python 解释器乐观缓存（A2 提速）。
 *
 * 乐观策略：命中即直接用于 spawn，跳过 ~0.5s 的并行探测；可信度由启动结果
 * 反向背书——健康就绪前任何失败（spawn ENOENT/9009、健康超时、应用退出取消
 * 启动）都说明缓存值已失真（解释器被卸载/移动/依赖被拆），一律作废，下一次
 * start 回到全量探测。健康就绪后才写入，探测成功本身不写（探测通过 ≠ 后端
 * 真能用，就绪才是唯一可信锚点）。
 *
 * 与 desktop-prefs 同口径：缓存永远不该把启动打崩（读失败/损坏一律按无缓存
 * 处理），落盘走原子写（tmp+rename，Windows 杀软占用时 EPERM 重试一次、仍
 * 失败退回直写），写失败只 warn 不影响本次启动。
 */
import { readFileSync, rmSync } from 'fs'
import { mkdir, rename, writeFile } from 'fs/promises'
import { join } from 'path'

export interface PythonLaunch {
  cmd: string
  prefixArgs: string[]
}

export const PYTHON_CACHE_FILE = 'backend-python.json'

/** 结构校验：cmd 非空字符串 + prefixArgs 全字符串。读写两侧共用，脏值绝不进 spawn 命令行 */
function isValidLaunch(v: unknown): v is PythonLaunch {
  if (!v || typeof v !== 'object') return false
  const o = v as Partial<PythonLaunch>
  return typeof o.cmd === 'string' && o.cmd.length > 0
    && Array.isArray(o.prefixArgs)
    && o.prefixArgs.every((s) => typeof s === 'string')
}

/** 读缓存：缺失/损坏/脏结构一律返回 null，不抛（按无缓存处理，交给全量探测兜底） */
export function loadPythonCache(userDataDir: string): PythonLaunch | null {
  // 空目录守卫：join('', f) 退化成相对 cwd 路径，读/写都不允许落在"当前目录"这种
  // 隐式位置上（测试环境 getPath 桩返空串时防污染仓库根）
  if (!userDataDir) return null
  let raw: string
  try {
    raw = readFileSync(join(userDataDir, PYTHON_CACHE_FILE), 'utf-8')
  } catch {
    return null // 文件不存在是常态，不打日志
  }
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!isValidLaunch(parsed)) {
      console.warn('[PythonCache] 缓存结构非法，按无缓存处理:', JSON.stringify(parsed).slice(0, 200))
      return null
    }
    return parsed
  } catch (e) {
    console.warn('[PythonCache] 缓存文件损坏，按无缓存处理:', e)
    return null
  }
}

/**
 * 写缓存：fire-and-forget 异步原子写（不返回 Promise，内部 .catch 兜底）。
 * 非法值直接 warn 返回，绝不落盘——脏缓存比没缓存危险（会被喂给 spawn）。
 */
export function savePythonCache(userDataDir: string, launch: PythonLaunch): void {
  if (!userDataDir) {
    console.warn('[PythonCache] userDataDir 为空，拒绝落盘（防止相对路径污染当前目录）')
    return
  }
  if (!isValidLaunch(launch)) {
    console.warn('[PythonCache] 拒绝落盘非法解释器值:', String(launch))
    return
  }
  const path = join(userDataDir, PYTHON_CACHE_FILE)
  const payload = JSON.stringify(launch, null, 2)
  void (async () => {
    try {
      // 目录通常预先存在（userData/临时目录）；mkdir 失败不致命，继续尝试直写，
      // 真正的磁盘问题会由后续 writeFile 暴露并进 .catch 告警
      await mkdir(userDataDir, { recursive: true })
    } catch { /* 尽力而为 */ }
    const tmp = `${path}.tmp`
    await writeFile(tmp, payload, 'utf-8')
    try {
      await rename(tmp, path)
    } catch (e) {
      // Windows 上杀软/索引短暂占用目标文件时 rename 会 EPERM：等一次再试，仍失败退回直写
      const code = (e as { code?: string })?.code
      if (code !== 'EPERM' && code !== 'EACCES' && code !== 'EBUSY') throw e
      await new Promise((r) => setTimeout(r, 200))
      try {
        await rename(tmp, path)
      } catch {
        await writeFile(path, payload, 'utf-8')
      }
    }
  })().catch((e) => console.warn('[PythonCache] 缓存落盘失败（不影响本次启动）:', e))
}

/** 作废缓存：幂等删除，文件不存在/被占用都只尽力而为 */
export function clearPythonCache(userDataDir: string): void {
  if (!userDataDir) return // 同 load 的空路径守卫，幂等跳过
  try {
    rmSync(join(userDataDir, PYTHON_CACHE_FILE), { force: true })
  } catch (e) {
    console.warn('[PythonCache] 缓存删除失败（下次启动探测会重新覆盖）:', e)
  }
}
