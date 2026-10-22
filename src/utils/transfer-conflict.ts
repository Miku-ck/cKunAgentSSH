/**
 * 传输冲突处理 —— 拖拽/上传下载落到已存在同名目标时的决策逻辑。
 *
 * 这里是纯函数：把「每个冲突选 覆盖/跳过/重命名」翻成 store.uploadFiles/
 * downloadFiles 真正需要的入参（保留哪些 path、哪些改名成什么），所以能脱离
 * DOM 直接单测（本仓前端测试跑 node 环境，见 vitest.config.ts 首行注释）。
 * 冲突检查是**顶层粒度**的 —— 只判 payload.paths 每一项在目标目录是否撞名，
 * 目录内部同名交给传输本身覆盖，与既有覆盖确认口径一致。
 */

/** 单个冲突项的处理选择。overwrite=保持原名覆盖（现状行为）；skip=整项不传输；rename=换顶层名传输。 */
export type ConflictAction = 'overwrite' | 'skip' | 'rename'

/** 文件大小/修改时间快照（字节 / epoch 毫秒）。目录侧 size 可能为 0，仅用于展示。 */
export interface ConflictMeta {
  size: number
  modified: number
}

/** 一条待用户裁决的冲突：源与目标各自的元信息 + 二者在哪个目录。 */
export interface ConflictItem {
  /** 稳定渲染键，取目标路径 */
  id: string
  /** payload.paths 里的原始路径（决策按它回挂） */
  path: string
  /** 顶层名（文件/目录名） */
  name: string
  /** 将要落到的已存在目标路径 */
  targetPath: string
  source: ConflictMeta
  target: ConflictMeta
}

/** 用户对一条冲突的裁决。rename 时 newName 为新顶层名。 */
export interface ConflictDecision {
  path: string
  action: ConflictAction
  newName?: string
}

/** 拆分 `name` 为 { base, ext }：`.gitignore` 这类无扩展名点归 base，`a.tar.gz` 只切最后一段。 */
function splitExt(name: string): { base: string; ext: string } {
  const dot = name.lastIndexOf('.')
  // 点在最前（隐藏文件）或没有点 → 整体当 base，无扩展名
  if (dot <= 0) return { base: name, ext: '' }
  return { base: name.slice(0, dot), ext: name.slice(dot) }
}

/**
 * 为 `name` 生成一个不与 takenNames 冲突的「(n)」后缀新名：先试 `a (1).txt`，
 * 占用则递增。返回的名字不会自动加入 takenNames（由调用方按最终计划登记）。
 */
export function autoRename(name: string, takenNames: Set<string>): string {
  if (!takenNames.has(name)) return name
  const { base, ext } = splitExt(name)
  for (let n = 1; n < 10000; n++) {
    const candidate = `${base} (${n})${ext}`
    if (!takenNames.has(candidate)) return candidate
  }
  // 理论不可达的一万重名兜底：挂时间戳，绝不返回与原名相同导致覆盖
  return `${base} (${Date.now()})${ext}`
}

/** 取路径最后一段（本地路径同时认两种分隔符）。 */
export function basenameLocal(path: string): string {
  const idx = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'))
  return idx >= 0 ? path.substring(idx + 1) : path
}

/** buildTransferPlan 的产物：过滤掉 skip 后的路径序列 + 需要改名的 原路径→新顶层名 映射。 */
export interface TransferPlan {
  paths: string[]
  renameMap: Record<string, string>
}

/**
 * 把用户裁决落到传输入参。
 * - 没有对应 decision 的 path 视为覆盖（保持原名），与「无冲突直接传」等价；
 * - 顺序沿用传入的 paths 原序（store 内部再按大小做优先排序，这里不打乱语义）。
 * @param takenNames 目标目录当前已存在的顶层名集合，rename 时用于挑不撞名的后缀。
 */
export function buildTransferPlan(
  paths: string[],
  decisions: ConflictDecision[],
  takenNames: Set<string> = new Set(),
): TransferPlan {
  const byPath = new Map(decisions.map(d => [d.path, d]))
  const outPaths: string[] = []
  const renameMap: Record<string, string> = {}
  for (const p of paths) {
    const d = byPath.get(p)
    if (!d || d.action === 'overwrite') {
      outPaths.push(p)
      continue
    }
    if (d.action === 'skip') continue
    // rename：优先用用户显式给的名，为空再按 takenNames 自动生成
    let name = d.newName?.trim()
    if (!name) {
      name = autoRename(basenameLocal(p), takenNames)
    }
    renameMap[p] = name
    takenNames.add(name)
    outPaths.push(p)
  }
  return { paths: outPaths, renameMap }
}
