/**
 * 技能 Store — 管理技能的启用、安装、导出
 * - 内置技能从用户目录 ~/.agents/skills 读取（只读）
 * - 用户自定义/上传技能持久化到 localStorage
 * - 不再包含任何假数据或模拟数据
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import JSZip from 'jszip'
import { load as yamlLoad } from 'js-yaml'
import { useToastStore } from './toast'

export type SkillSource = 'builtin' | 'upload' | 'custom' | 'manual' | 'auto'

export interface Skill {
  id: string
  name: string
  slug: string
  version: string
  description: string
  category: string
  content: string
  triggerWords: string[]
  icon: string
  enabled: boolean
  source: SkillSource
  metadata: Record<string, unknown>
  /** 本地资源目录绝对路径（builtin/upload 来源存在） */
  localPath?: string
  /** 本地资源文件相对路径列表（不含 SKILL.md） */
  assets?: string[]
  created_at: string
  updated_at: string
}

/** SKILL.md frontmatter 解析结果 */
export interface SkillManifest {
  name: string
  slug: string
  version: string
  description: string
  category: string
  metadata: Record<string, unknown>
  content: string
}

/** 安装结果 */
export interface InstallResult {
  success: boolean
  skillId?: string
  error?: string
  overwritten?: boolean
}

const SKILL_MD_NAMES = ['SKILL.md', 'skill.md', 'Skill.md']
const MAX_ZIP_SIZE = 10 * 1024 * 1024 // 10MB
const STORAGE_KEY = 'ckun-skills-v2'

/** 为没有 category 的技能按 slug 归类 */
const CATEGORY_MAP: Record<string, string> = {
  'code-guidelines': '效率提升',
  'code-review': '开发工具',
  'docker-essentials': '开发工具',
  'find-skills': '效率提升',
  'frontend-design': '界面设计',
  'git': '开发工具',
  'brainstorming': '内容创作',
  'self-improving-agent': '效率提升',
  'ui-ux-pro-max': '界面设计',
  'using-superpowers': '效率提升',
}

/** 用技能名称生成两字母图标（不使用 emoji） */
export function skillInitials(name: string): string {
  const cleaned = name.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]/g, ' ')
  const words = cleaned.split(/\s+/).filter(Boolean)
  if (words.length >= 2) {
    return (words[0][0] + words[1][0]).toUpperCase()
  }
  return cleaned.slice(0, 2).toUpperCase()
}

function persistState(enabledMap: Record<string, boolean>, userSkills: Skill[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabledMap, userSkills }))
}

function loadPersistedState(): { enabledMap: Record<string, boolean>; userSkills: Skill[] } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { enabledMap: {}, userSkills: [] }
    const parsed = JSON.parse(raw)
    return {
      enabledMap: parsed.enabledMap && typeof parsed.enabledMap === 'object' ? parsed.enabledMap : {},
      userSkills: Array.isArray(parsed.userSkills) ? parsed.userSkills : [],
    }
  } catch {
    return { enabledMap: {}, userSkills: [] }
  }
}

/**
 * 从 SKILL.md 文本解析 frontmatter + 正文
 * frontmatter 格式：
 * ---
 * name: my-skill
 * slug: my-skill
 * version: 1.0.0
 * description: "技能描述"
 * category: coding
 * metadata: {"emoji": "⚡"}
 * ---
 * ## 使用场景
 * ...
 */
export function parseSkillMd(text: string): SkillManifest {
  const matched = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/)
  if (!matched) {
    throw new Error('SKILL.md 缺少 frontmatter（需以 --- 开头和结尾的 YAML 头）')
  }
  const [, frontmatter, body] = matched

  let parsed: any
  try {
    parsed = yamlLoad(frontmatter) || {}
  } catch (e: any) {
    throw new Error(`SKILL.md frontmatter YAML 解析失败：${e?.message || e}`)
  }

  const name = String(parsed.name || '').trim()
  if (!name) throw new Error('SKILL.md frontmatter 缺少 name 字段')

  return {
    name,
    slug: String(parsed.slug || name).trim(),
    version: String(parsed.version || '1.0.0').trim(),
    description: String(parsed.description || '').trim(),
    category: String(parsed.category || '').trim(),
    metadata: parsed.metadata && typeof parsed.metadata === 'object' ? parsed.metadata : {},
    content: body.trim(),
  }
}

/**
 * 新 IPC 契约（主进程侧同步实现，preload 方法名/签名固定）：
 * - skillsMkdir：带 skillsRoot 边界断言的 mkdir
 * - skillsWriteFile：带 skillsRoot 边界断言的二进制写入
 * - fsReadFileBase64：10MB 上限的 base64 读取
 * 本地类型断言 — env.d.ts 的 ElectronAPI 由主进程代理同步更新
 */
interface SkillsIpcApi extends ElectronAPI {
  skillsMkdir: (dirPath: string) => Promise<void>
  skillsWriteFile: (filePath: string, base64: string) => Promise<void>
  fsReadFileBase64: (filePath: string) => Promise<{ base64: string; size: number }>
}

/** C-1 修复：slug 校验 — 拒绝 `..` 段、以 / 或盘符（如 C:）开头、含 \ 的 slug */
function isValidSkillSlug(slug: string): boolean {
  if (!slug) return false
  if (slug.includes('\\')) return false
  if (slug.startsWith('/')) return false
  if (/^[a-z]:/i.test(slug)) return false
  if (slug.split('/').some(seg => seg === '..')) return false
  return true
}

/** C-1 修复：zip 条目 relPath（前缀剥离后）校验 — 拒绝 `..` 段、以 / 开头、含 \、含盘符 */
function isValidSkillRelPath(relPath: string): boolean {
  if (!relPath) return false
  if (relPath.startsWith('/')) return false
  if (relPath.includes('\\')) return false
  if (/^[a-z]:/i.test(relPath)) return false
  if (relPath.split('/').some(seg => seg === '..')) return false
  return true
}

async function findSkillMdPath(api: ElectronAPI, dirPath: string): Promise<string | null> {
  try {
    const entries = await api.fsReadDir(dirPath)
    const target = entries.find(e => !e.isDirectory && SKILL_MD_NAMES.includes(e.name))
    return target?.path || null
  } catch {
    return null
  }
}

export const useSkillStore = defineStore('skills', () => {
  const persisted = loadPersistedState()
  const builtinSkills = ref<Skill[]>([])
  const userSkills = ref<Skill[]>(persisted.userSkills)
  const remoteSkills = ref<Skill[]>([])
  const enabledMap = ref<Record<string, boolean>>(persisted.enabledMap)
  const isLoading = ref(false)
  const loadError = ref<string | null>(null)
  const disabledByMode = ref<Record<string, Set<string>>>({})

  const skills = computed(() => [...builtinSkills.value, ...userSkills.value, ...remoteSkills.value])
  const enabledSkills = computed(() => skills.value.filter(s => s.enabled))
  const categories = computed(() => {
    const cats = new Set(skills.value.map(s => s.category).filter(Boolean))
    return ['全部', ...Array.from(cats)]
  })
  const marketSkills = computed(() => builtinSkills.value)


  /** 从 ~/.agents/skills 读取内置技能 */
  async function loadBuiltinSkills(): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true
    loadError.value = null

    const api = (typeof window !== 'undefined' ? (window as Window).electronAPI : undefined) as
      | ElectronAPI
      | undefined
    if (!api) {
      isLoading.value = false
      return
    }

    try {
      const home = await api.fsGetHomePath()
      const skillsDir = await api.fsPathJoin(home, '.agents', 'skills')
      const entries = await api.fsReadDir(skillsDir)
      const loaded: Skill[] = []
      const now = new Date().toISOString()

      for (const entry of entries) {
        if (!entry.isDirectory) continue
        const skillMdPath = await findSkillMdPath(api, entry.path)
        if (!skillMdPath) continue

        try {
          const text = await api.fsReadFile(skillMdPath)
          const manifest = parseSkillMd(text)
          const slug = manifest.slug || entry.name
          const category = manifest.category || CATEGORY_MAP[slug] || '未分类'
          const triggerWords = Array.isArray(manifest.metadata?.triggerWords)
            ? manifest.metadata.triggerWords.map(String)
            : []

          loaded.push({
            id: `builtin-${slug}`,
            name: manifest.name,
            slug,
            version: manifest.version || '1.0.0',
            description: manifest.description,
            category,
            content: manifest.content,
            triggerWords,
            icon: typeof manifest.metadata?.icon === 'string' ? manifest.metadata.icon : skillInitials(manifest.name),
            enabled: enabledMap.value[slug] ?? false,
            source: 'builtin',
            metadata: manifest.metadata,
            localPath: entry.path,
            created_at: now,
            updated_at: now,
          })
        } catch (e: any) {
          console.warn(`[skills] 加载技能失败 ${entry.path}: ${e?.message || e}`)
        }
      }

      builtinSkills.value = loaded
      // 同步内置技能到后端数据库，让 Agent 模式的 SkillRein 也能匹配到它们
      await syncBuiltinToBackend(loaded)
    } catch (e: any) {
      loadError.value = e?.message || String(e)
      console.error('[skills] 扫描内置技能失败:', e)
    } finally {
      // 无论内置技能扫描是否成功，都尝试加载后端 manual/auto 技能
      await loadRemoteSkills()
      isLoading.value = false
    }
  }

  /** 将内置技能同步到后端数据库（upsert + 孤儿清理） */
  async function syncBuiltinToBackend(loaded: Skill[]): Promise<void> {
    const api = (typeof window !== 'undefined' ? (window as Window).electronAPI : undefined) as
      | ElectronAPI
      | undefined
    if (!api?.syncBuiltinSkills) return
    try {
      const payload = loaded.map(s => ({
        name: s.name,
        slug: s.slug,
        system_prompt: s.content,
        description: s.description || '',
        category: s.category || '',
        trigger_words: s.triggerWords || [],
      }))
      const result = await api.syncBuiltinSkills(payload)
      // 同步成功 — 无需日志
      void result
    } catch (e: any) {
      console.warn('[skills] 同步内置技能到后端失败:', e?.message || e)
    }
  }

  /** 从后端加载 manual/auto 技能（含自进化生成的 source='auto' 技能） */
  async function loadRemoteSkills(): Promise<void> {
    try {
      const api = (typeof window !== 'undefined' ? (window as Window).electronAPI : undefined) as
        | ElectronAPI
        | undefined
      if (!api) return

      const items = await api.listSkills()
      const modeDisabled: Record<string, Set<string>> = {}
      remoteSkills.value = items.map(item => {
        // trigger_words 是 JSON 字符串，需解析
        let triggerWords: string[] = []
        try {
          const parsed = typeof item.trigger_words === 'string'
            ? JSON.parse(item.trigger_words)
            : item.trigger_words
          if (Array.isArray(parsed)) {
            triggerWords = parsed.filter((w: unknown) => typeof w === 'string')
          }
        } catch (e) {
          console.warn(`[skills] 解析 trigger_words 失败 (skill ${item.id}):`, e)
          triggerWords = []
        }

        // mode_configs 可能是 JSON 字符串或对象，解析并按 mode 缓存禁用状态
        let modeConfigs: Record<string, { enabled?: boolean }> = {}
        try {
          modeConfigs = typeof item.mode_configs === 'string'
            ? JSON.parse(item.mode_configs)
            : item.mode_configs || {}
        } catch (e) {
          console.warn(`[skills] 解析 mode_configs 失败 (skill ${item.id}):`, e)
          modeConfigs = {}
        }
        for (const [mode, cfg] of Object.entries(modeConfigs)) {
          if (cfg && cfg.enabled === false) {
            if (!modeDisabled[mode]) modeDisabled[mode] = new Set<string>()
            modeDisabled[mode].add(item.id)
          }
        }

        return {
          id: item.id,
          name: item.name,
          slug: item.slug,
          version: '1.0.0',
          description: item.description,
          category: item.category || '未分类',
          content: item.system_prompt,
          triggerWords,
          icon: skillInitials(item.name),
          enabled: item.enabled === 1,
          source: item.source as SkillSource,
          metadata: { priority: item.priority || 0 },
          created_at: item.created_at,
          updated_at: item.updated_at,
        }
      })
      disabledByMode.value = modeDisabled
    } catch (e: any) {
      console.error('[skills] 加载后端技能失败:', e)
    }
  }

  function save(): void {
    persistState(enabledMap.value, userSkills.value)
  }

  async function deleteSkill(id: string): Promise<void> {
    const skill = skills.value.find(s => s.id === id)
    if (!skill) return

    if (skill.source === 'auto' || skill.source === 'manual') {
      const api = (window as Window).electronAPI
      if (api?.deleteSkill) {
        await api.deleteSkill(id)
      }
      remoteSkills.value = remoteSkills.value.filter(s => s.id !== id)
      return
    }

    if (skill?.localPath) {
      const api = (window as Window).electronAPI
      if (api?.fsRmRF && api?.fsGetSkillsDir) {
        try {
          const skillsRoot = await api.fsGetSkillsDir()
          if (skill.localPath.startsWith(skillsRoot)) {
            await api.fsRmRF(skill.localPath)
          }
        } catch {
          // 删除本地目录失败不阻断技能删除
        }
      }
    }
    userSkills.value = userSkills.value.filter(s => s.id !== id)
    save()
  }

  async function toggleSkill(id: string, mode?: string): Promise<void> {
    const skill = skills.value.find(s => s.id === id)
    if (!skill) return
    const prevEnabled = skill.enabled
    skill.enabled = !skill.enabled
    skill.updated_at = new Date().toISOString()

    if (skill.source === 'builtin') {
      enabledMap.value[skill.slug] = skill.enabled
      save()
      return
    }

    if (skill.source === 'auto' || skill.source === 'manual') {
      const api = (window as Window).electronAPI
      if (api?.updateSkillEnabled) {
        try {
          await api.updateSkillEnabled(id, skill.enabled, mode)
        } catch {
          skill.enabled = prevEnabled
          skill.updated_at = new Date().toISOString()
          throw new Error('更新技能状态失败')
        }
      }
      return
    }

    save()
  }

  /** 根据触发词自动匹配技能（最多 3 个）
   * @param message 用户消息
   * @param mode 可选模式，传入时排除该模式下被禁用的技能
   */
  function matchSkills(message: string, mode?: string): Skill[] {
    const msg = message.toLowerCase()
    const matched: Skill[] = []
    const disabledIds = mode ? (disabledByMode.value[mode] || new Set<string>()) : new Set<string>()
    for (const skill of skills.value) {
      if (!skill.enabled) continue
      if (mode && disabledIds.has(skill.id)) continue
      for (const word of skill.triggerWords) {
        if (msg.includes(word.toLowerCase())) {
          matched.push(skill)
          break
        }
      }
      if (matched.length >= 3) break
    }
    return matched
  }

  /** 获取技能内容作为 system prompt 片段 */
  function getSkillContext(skillIds: string[]): string {
    const selected = skills.value.filter(s => skillIds.includes(s.id) && s.enabled)
    if (selected.length === 0) return ''
    return selected.map(s =>
      `以下是一个名为「${s.name}」的技能提供的参考信息：\n---技能内容开始---\n${s.content.slice(0, 3000)}\n---技能内容结束---`
    ).join('\n\n')
  }

  /** 将 ZIP 内所有文件解压到本地技能目录 {userData}/skills/{slug}/ */
  async function extractZipToLocalStorage(
    zip: JSZip,
    skillMdPath: string,
    slug: string,
  ): Promise<{ localPath: string; assets: string[] } | null> {
    const api = (window as Window).electronAPI as SkillsIpcApi | undefined
    if (!api?.fsGetSkillsDir || !api?.skillsMkdir || !api?.skillsWriteFile || !api?.fsPathJoin) {
      return null
    }
    // C-1 修复：安装前拦截非法 slug（`..` 逃逸 / 绝对路径 / 盘符 / 反斜杠）
    if (!isValidSkillSlug(slug)) {
      useToastStore().error(`非法的技能目录名: ${slug}，已阻止安装`)
      throw new Error(`非法的技能目录名: ${slug}`)
    }
    const skillsRoot = await api.fsGetSkillsDir()
    const skillDir = await api.fsPathJoin(skillsRoot, slug)
    await api.skillsMkdir(skillDir)

    const assets: string[] = []
    const skipped: string[] = []
    const skillMdDir = skillMdPath.includes('/') ? skillMdPath.slice(0, skillMdPath.lastIndexOf('/')) : ''

    for (const path of Object.keys(zip.files)) {
      const entry = zip.files[path]
      if (entry.dir) continue
      let relPath = path
      if (skillMdDir && path.startsWith(skillMdDir + '/')) {
        relPath = path.slice(skillMdDir.length + 1)
      }
      if (!relPath || relPath === skillMdPath) continue

      // C-1 修复：Zip-Slip — 逐条校验前缀剥离后的 relPath，
      // 非法条目跳过并 warn（不整体拒绝），最后统一 toast 提示
      if (!isValidSkillRelPath(relPath)) {
        skipped.push(relPath)
        console.warn('[skills] 跳过非法 zip 条目路径（Zip-Slip 防护）:', relPath)
        continue
      }

      const fullPath = await api.fsPathJoin(skillDir, relPath)
      if (relPath.includes('/')) {
        const subDir = await api.fsPathJoin(skillDir, relPath.slice(0, relPath.lastIndexOf('/')))
        await api.skillsMkdir(subDir)
      }
      const blob = await entry.async('blob')
      const base64 = await blobToBase64(blob)
      // 新契约：带 skillsRoot 边界断言的二进制写入（主进程二次校验）
      await api.skillsWriteFile(fullPath, base64)
      assets.push(relPath)
    }

    if (skipped.length > 0) {
      useToastStore().warning(`${skipped.length} 个非法路径条目被跳过`)
    }

    return { localPath: skillDir, assets }
  }

  function blobToBase64(blob: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onloadend = () => {
        const result = reader.result as string
        const commaIdx = result.indexOf(',')
        resolve(commaIdx === -1 ? result : result.slice(commaIdx + 1))
      }
      reader.onerror = reject
      reader.readAsDataURL(blob)
    })
  }

  /** 从 manifest 写入/覆盖用户技能 */
  function upsertSkillFromManifest(
    manifest: SkillManifest,
    forceOverwrite: boolean,
    localInfo?: { localPath: string; assets: string[] },
  ): { id: string; overwritten: boolean } {
    const existingIdx = userSkills.value.findIndex(s => s.slug === manifest.slug)
    if (existingIdx !== -1) {
      if (!forceOverwrite) {
        throw new Error(`技能「${manifest.slug}」已存在（v${userSkills.value[existingIdx].version}），勾选"强制覆盖已有版本"以覆盖`)
      }
      const existing = userSkills.value[existingIdx]
      userSkills.value[existingIdx] = {
        ...existing,
        name: manifest.name,
        slug: manifest.slug,
        version: manifest.version,
        description: manifest.description,
        category: manifest.category || existing.category,
        content: manifest.content,
        metadata: manifest.metadata,
        source: 'upload',
        localPath: localInfo?.localPath ?? existing.localPath,
        assets: localInfo?.assets ?? existing.assets,
        updated_at: new Date().toISOString(),
      }
      save()
      return { id: existing.id, overwritten: true }
    }
    const now = new Date().toISOString()
    const id = `skill-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    userSkills.value.push({
      id,
      name: manifest.name,
      slug: manifest.slug,
      version: manifest.version,
      description: manifest.description,
      category: manifest.category || '未分类',
      content: manifest.content,
      triggerWords: [],
      icon: skillInitials(manifest.name),
      enabled: true,
      source: 'upload',
      metadata: manifest.metadata,
      localPath: localInfo?.localPath,
      assets: localInfo?.assets,
      created_at: now,
      updated_at: now,
    })
    save()
    return { id, overwritten: false }
  }

  /** 从 ZIP 文件安装技能包 */
  async function installFromZip(file: File, forceOverwrite: boolean): Promise<InstallResult> {
    if (!file.name.toLowerCase().endsWith('.zip')) {
      return { success: false, error: '请选择 .zip 格式的技能包' }
    }
    if (file.size > MAX_ZIP_SIZE) {
      return { success: false, error: `ZIP 大小超过限制（最大 ${MAX_ZIP_SIZE / 1024 / 1024}MB）` }
    }
    try {
      const zip = await JSZip.loadAsync(file)
      const candidates = Object.keys(zip.files).filter(p => {
        const base = p.split('/').pop() || ''
        return SKILL_MD_NAMES.includes(base)
      })
      if (candidates.length === 0) {
        return { success: false, error: 'ZIP 包中未找到 SKILL.md 文件' }
      }
      const target = candidates.sort((a, b) => a.split('/').length - b.split('/').length)[0]
      const text = await zip.files[target].async('string')
      const manifest = parseSkillMd(text)

      const existing = userSkills.value.find(s => s.slug === manifest.slug)
      if (existing && !forceOverwrite) {
        return {
          success: false,
          error: `技能「${manifest.slug}」已存在（v${existing.version}），勾选"强制覆盖已有版本"以覆盖`,
        }
      }

      const localInfo = await extractZipToLocalStorage(zip, target, manifest.slug)
      const { id, overwritten } = upsertSkillFromManifest(manifest, forceOverwrite, localInfo ?? undefined)
      return { success: true, skillId: id, overwritten }
    } catch (e: any) {
      if (e?.message && /已存在|frontmatter|YAML/.test(e.message)) {
        return { success: false, error: e.message }
      }
      return { success: false, error: `ZIP 解压失败：${e?.message || e}` }
    }
  }

  /** 从本地文件夹安装技能包 */
  async function installFromFolder(forceOverwrite: boolean): Promise<InstallResult> {
    const api = (window as Window).electronAPI
    if (!api?.fsSelectFolder) {
      return { success: false, error: '当前环境不支持文件夹选择（需 Electron 环境）' }
    }
    try {
      const folder = await api.fsSelectFolder()
      if (!folder) return { success: false, error: '已取消' }
      const entries = await api.fsReadDir(folder)
      const target = entries.find(e => !e.isDirectory && SKILL_MD_NAMES.includes(e.name))
      if (!target) {
        return { success: false, error: `文件夹中未找到 SKILL.md 文件：${folder}` }
      }
      const text = await api.fsReadFile(target.path)
      const manifest = parseSkillMd(text)

      const existing = userSkills.value.find(s => s.slug === manifest.slug)
      if (existing && !forceOverwrite) {
        return {
          success: false,
          error: `技能「${manifest.slug}」已存在（v${existing.version}），勾选"强制覆盖已有版本"以覆盖`,
        }
      }

      let localInfo: { localPath: string; assets: string[] } | null = null
      if (api.fsListFilesRecursive) {
        const allFiles = await api.fsListFilesRecursive(folder)
        const assets = allFiles.filter(f => f !== target.name && !SKILL_MD_NAMES.includes(f))
        localInfo = { localPath: folder, assets }
      }

      const { id, overwritten } = upsertSkillFromManifest(manifest, forceOverwrite, localInfo ?? undefined)
      return { success: true, skillId: id, overwritten }
    } catch (e: any) {
      return { success: false, error: `文件夹读取失败：${e?.message || e}` }
    }
  }

  /** 导出技能为 ZIP 包 */
  async function exportSkill(id: string): Promise<{ success: boolean; error?: string }> {
    const skill = skills.value.find(s => s.id === id)
    if (!skill) return { success: false, error: '技能不存在' }
    try {
      const metadataObj = { ...skill.metadata }
      if (skill.icon) {
        metadataObj.emoji = skill.icon
      }
      const metadataStr = Object.keys(metadataObj).length > 0
        ? `metadata: ${JSON.stringify(metadataObj)}`
        : ''
      const frontmatter = [
        '---',
        `name: ${skill.name}`,
        `slug: ${skill.slug}`,
        `version: ${skill.version}`,
        `description: "${skill.description.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`,
        `category: ${skill.category}`,
        metadataStr,
        '---',
      ].filter(Boolean).join('\n')
      const skillMd = `${frontmatter}\n${skill.content}`

      const zip = new JSZip()
      zip.file('SKILL.md', skillMd)

      // B17 修复：用 fsReadFileBase64 读二进制资源 —
      // 原 fsReadFile 按 UTF-8 文本读二进制文件会破坏字节序列，导出包损坏。
      // 文本/二进制统一走 base64，JSZip 支持 Uint8Array
      const api = (window as Window).electronAPI as SkillsIpcApi | undefined
      if (skill.localPath && skill.assets && skill.assets.length > 0 && api?.fsPathJoin && api?.fsReadFileBase64) {
        for (const relPath of skill.assets) {
          const fullPath = await api.fsPathJoin(skill.localPath, relPath)
          try {
            const { base64 } = await api.fsReadFileBase64(fullPath)
            const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0))
            zip.file(relPath, bytes)
          } catch {
            // 单个资源读取失败跳过
          }
        }
      }

      const blob = await zip.generateAsync({ type: 'blob' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${skill.slug || skill.name}.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)

      return { success: true }
    } catch (e: any) {
      return { success: false, error: `导出失败：${e?.message || e}` }
    }
  }

  return {
    skills,
    enabledSkills,
    categories,
    marketSkills,
    builtinSkills,
    userSkills,
    remoteSkills,
    disabledByMode,
    isLoading,
    loadError,
    deleteSkill,
    toggleSkill,
    matchSkills,
    getSkillContext,
    loadBuiltinSkills,
    loadRemoteSkills,
    installFromZip,
    installFromFolder,
    exportSkill,
  }
})
