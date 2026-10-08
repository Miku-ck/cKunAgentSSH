/**
 * 技能 Store — 管理技能的加载与启用状态
 * - 内置技能从用户目录 ~/.agents/skills 读取（只读）
 * - 用户自定义/上传技能持久化到 localStorage
 * - 不再包含任何假数据或模拟数据
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { load as yamlLoad } from 'js-yaml'

type SkillSource = 'builtin' | 'upload' | 'custom' | 'manual' | 'auto'

interface Skill {
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
interface SkillManifest {
  name: string
  slug: string
  version: string
  description: string
  category: string
  metadata: Record<string, unknown>
  content: string
}

const SKILL_MD_NAMES = ['SKILL.md', 'skill.md', 'Skill.md']

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
function skillInitials(name: string): string {
  const cleaned = name.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]/g, ' ')
  const words = cleaned.split(/\s+/).filter(Boolean)
  if (words.length >= 2) {
    return (words[0][0] + words[1][0]).toUpperCase()
  }
  return cleaned.slice(0, 2).toUpperCase()
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
function parseSkillMd(text: string): SkillManifest {
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

async function findSkillMdPath(api: ElectronAPI, dirPath: string): Promise<string | null> {
  try {
    const entries = await api.fsReadDir(dirPath)
    const target = entries.find(e => !e.isDirectory && SKILL_MD_NAMES.includes(e.name))
    return target?.path || null
  } catch {
    return null
  }
}

/** 对话侧只有 SSH 助手一个模式：与设置页 SkillsPanel 的开关、/agent/run 的 mode 保持一致 */
const CHAT_MODE = 'ssh'

export const useSkillStore = defineStore('skills', () => {
  const builtinSkills = ref<Skill[]>([])
  const remoteSkills = ref<Skill[]>([])
  const isLoading = ref(false)

  // 本地扫描到的内置技能只用于同步入库（syncBuiltinToBackend）：菜单读的是
  // 后端那份（remoteSkills，带用户在设置页的开关状态）。这里若置 enabled，
  // 同一个技能会在 "/" 菜单里出现两条（id 还分别是 builtin-<slug> 与 DB id）。
  const skills = computed(() => [...builtinSkills.value, ...remoteSkills.value])
  const enabledSkills = computed(() => skills.value.filter(s => s.enabled))

  /** 从 ~/.agents/skills 读取内置技能 */
  async function loadBuiltinSkills(): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true

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
            enabled: false, // 见 skills 计算属性：菜单以后端副本为准，避免同名重复
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

      // 带 mode 才能拿到 per-mode 开关：设置页的开关写的就是 skill_mode_config，
      // 不带 mode 时后端只回全局 enabled —— 于是「本模式已停用」的技能仍出现在
      // 对话侧 "/" 菜单里，选中后被 agent.py 按 mode 过滤掉，表现为静默无效。
      const items = await api.listSkills(CHAT_MODE)
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
          // 指定了 mode 时以后端 mode_enabled 为准（与 SkillsPanel 同一口径）；
          // 字段缺失（旧后端/未带 mode）才退回全局 enabled
          enabled: item.mode_enabled === undefined ? item.enabled === 1 : !!item.mode_enabled,
          source: item.source as SkillSource,
          metadata: { priority: item.priority || 0 },
          created_at: item.created_at,
          updated_at: item.updated_at,
        }
      })
    } catch (e: any) {
      console.error('[skills] 加载后端技能失败:', e)
    }
  }

  return {
    skills,
    enabledSkills,
    loadBuiltinSkills,
    // 设置页改动技能后需回灌对话侧菜单：remoteSkills 只在 loadBuiltinSkills 时拉一次
    loadRemoteSkills,
  }
})
