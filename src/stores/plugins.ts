/**
 * 插件 Store — MCP server / 外部进程的定义与同意流。
 *
 * 三步缺一不可：安装（保存即停用）→ 验证并加载工具（启动进程、看到清单）
 * → 确认启用。配置变更由后端撤销同意并递增 config_rev。
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { pluginsApi, type Plugin, type PluginPayload, type PluginToolSpec } from '@/api/core'

export const usePluginsStore = defineStore('plugins', () => {
  const plugins = ref<Plugin[]>([])
  const loading = ref(false)
  const error = ref('')

  const enabled = computed(() => plugins.value.filter(p => p.enabled && p.consented_at))

  function toolsOf(plugin: Plugin): PluginToolSpec[] {
    try {
      return JSON.parse(plugin.tools_json || '[]') as PluginToolSpec[]
    } catch {
      return []
    }
  }

  function readonlyOf(plugin: Plugin): string[] {
    try {
      return JSON.parse(plugin.readonly_tools_json || '[]') as string[]
    } catch {
      return []
    }
  }

  async function fetchPlugins(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      plugins.value = await pluginsApi.list()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function create(payload: PluginPayload): Promise<Plugin> {
    const created = await pluginsApi.create(payload)
    await fetchPlugins()
    return created
  }

  async function validate(id: string): Promise<PluginToolSpec[]> {
    const result = await pluginsApi.validate(id)
    await fetchPlugins()
    return result.tools
  }

  async function consent(id: string): Promise<void> {
    await pluginsApi.consent(id)
    await fetchPlugins()
  }

  async function setReadonly(id: string, names: string[]): Promise<void> {
    await pluginsApi.setReadonly(id, names)
    await fetchPlugins()
  }

  async function setEnabled(id: string, value: boolean): Promise<void> {
    await pluginsApi.setEnabled(id, value)
    await fetchPlugins()
  }

  async function remove(id: string): Promise<void> {
    await pluginsApi.remove(id)
    await fetchPlugins()
  }

  return {
    plugins, loading, error, enabled,
    toolsOf, readonlyOf,
    fetchPlugins, create, validate, consent, setReadonly, setEnabled, remove,
  }
})
