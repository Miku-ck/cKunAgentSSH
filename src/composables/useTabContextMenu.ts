// useTabContextMenu.ts — 终端标签右键菜单
import { ref, onMounted, onUnmounted } from 'vue'

export function useTabContextMenu() {
  const tabCtxMenu = ref({ visible: false, x: 0, y: 0, targetId: '' })

  function showTabContextMenu(e: MouseEvent, id: string) {
    // 菜单约 7 个菜单项，每个 ~28px，总计 ~210px
    const estimatedHeight = 210
    const viewportHeight = window.innerHeight
    const y = e.clientY + estimatedHeight > viewportHeight
      ? e.clientY - estimatedHeight
      : e.clientY
    tabCtxMenu.value = { visible: true, x: e.clientX, y, targetId: id }
  }

  function dismissTabCtxMenu() {
    tabCtxMenu.value.visible = false
  }

  onMounted(() => {
    document.addEventListener('click', dismissTabCtxMenu)
  })

  onUnmounted(() => {
    document.removeEventListener('click', dismissTabCtxMenu)
  })

  return { tabCtxMenu, showTabContextMenu, dismissTabCtxMenu }
}
