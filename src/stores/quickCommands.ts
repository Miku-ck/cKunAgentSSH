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

const PRESET_COLORS = [
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
          commands.value = parsed.map((item, index) => ({
            id: item.id || generateId(),
            name: String(item.name || '').slice(0, 30),
            command: String(item.command || '').slice(0, 1000),
            color: item.color || PRESET_COLORS[index % PRESET_COLORS.length],
            order: typeof item.order === 'number' ? item.order : index,
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
    const sourceIndex = commands.value.findIndex(c => c.id === sourceId)
    const targetIndex = commands.value.findIndex(c => c.id === targetId)
    if (sourceIndex === -1 || targetIndex === -1 || sourceIndex === targetIndex) return

    const [source] = commands.value.splice(sourceIndex, 1)
    const newTargetIndex = commands.value.findIndex(c => c.id === targetId)
    commands.value.splice(newTargetIndex, 0, source)

    commands.value.forEach((cmd, index) => {
      cmd.order = index
    })

    save()
  }

  function getDefaultColor(index: number): string {
    return PRESET_COLORS[index % PRESET_COLORS.length]
  }

  return {
    commands,
    sortedCommands,
    loaded,
    load,
    addCommand,
    updateCommand,
    removeCommand,
    reorder,
    getDefaultColor,
  }
})
