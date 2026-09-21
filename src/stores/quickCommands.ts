import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export type QuickCommand = {
  id: string
  name: string
  command: string
  color: string
  order: number
}

const STORAGE_KEY = 'ckun-ssh-quick-commands'

// 编辑器色板与本列表同源：原先两边各抄一份 8 色，改一处就会与另一处错位
export const PRESET_COLORS = [
  '#58a6ff', // blue
  '#3fb950', // green
  '#d29922', // yellow
  '#f85149', // red
  '#a371f7', // purple
  '#f778ba', // pink
  '#39c5cf', // cyan
  '#8b949e', // gray
]

function generateId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}

export const useQuickCommandsStore = defineStore('quickCommands', () => {
  const commands = ref<QuickCommand[]>([])
  const loaded = ref(false)

  const sortedCommands = computed(() =>
    commands.value.slice().sort((a, b) => a.order - b.order)
  )

  function load(): void {
    if (loaded.value) return
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed)) {
          // 逐项过滤而非整体 try 兜底：一条脏数据（null/字符串混进数组）就让
          // item.name 抛错，会走到 catch 把 commands 清成 [] —— 之后任何一次
          // addCommand 的 save() 都会把「只剩这一条」写回 localStorage。
          commands.value = parsed
            .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
            .map((item, index) => ({
              id: typeof item.id === 'string' && item.id ? item.id : generateId(),
              name: String(item.name ?? '').slice(0, 30),
              command: String(item.command ?? '').slice(0, 1000),
              color: typeof item.color === 'string' && item.color
                ? item.color
                : PRESET_COLORS[index % PRESET_COLORS.length],
              // NaN/Infinity 的 order 会让 sortedCommands 比较结果不确定，退回位置序号
              order: Number.isFinite(item.order) ? item.order as number : index,
            }))
        }
      }
    } catch (err) {
      console.error('[quickCommands] load failed:', err)
      commands.value = []
    }
    loaded.value = true
  }

  function save(): void {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(commands.value))
    } catch (err) {
      console.error('[quickCommands] save failed:', err)
    }
  }

  function addCommand(name: string, command: string, color?: string): QuickCommand {
    const nextOrder = commands.value.length > 0
      ? Math.max(...commands.value.map(c => c.order)) + 1
      : 0
    const item: QuickCommand = {
      id: generateId(),
      name: name.trim().slice(0, 30),
      command: command.trimEnd().slice(0, 1000),
      color: color || PRESET_COLORS[commands.value.length % PRESET_COLORS.length],
      order: nextOrder,
    }
    commands.value.push(item)
    save()
    return item
  }

  function updateCommand(id: string, updates: Partial<Omit<QuickCommand, 'id' | 'order'>>): void {
    const idx = commands.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const current = commands.value[idx]
    commands.value[idx] = {
      ...current,
      name: updates.name !== undefined ? updates.name.trim().slice(0, 30) : current.name,
      command: updates.command !== undefined ? updates.command.trimEnd().slice(0, 1000) : current.command,
      color: updates.color !== undefined ? updates.color : current.color,
    }
    save()
  }

  function removeCommand(id: string): void {
    const idx = commands.value.findIndex(c => c.id === id)
    if (idx === -1) return
    commands.value.splice(idx, 1)
    save()
  }

  function reorder(sourceId: string, targetId: string): void {
    // 拖拽发生在 sortedCommands（按 order 排过）上，而 commands 的内部顺序不保证与
    // 之一致（历史数据里 order 可能乱序）。按内部数组下标换位会让落点与拖拽结果不符，
    // 故先在显示序上定位，再把重排后的序号写回 order。
    const ordered = commands.value.slice().sort((a, b) => a.order - b.order)
    const sourceIndex = ordered.findIndex(c => c.id === sourceId)
    const targetIndex = ordered.findIndex(c => c.id === targetId)
    if (sourceIndex === -1 || targetIndex === -1 || sourceIndex === targetIndex) return

    const [source] = ordered.splice(sourceIndex, 1)
    ordered.splice(ordered.findIndex(c => c.id === targetId), 0, source)
    ordered.forEach((cmd, index) => {
      cmd.order = index
    })

    save()
  }

  return {
    commands,
    sortedCommands,
    load,
    addCommand,
    updateCommand,
    removeCommand,
    reorder,
  }
})
