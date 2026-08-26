// useTabContextMenu.ts — 终端标签右键菜单
import { ref, onMounted, onUnmounted } from 'vue'

export function useTabContextMenu() {
  const tabCtxMenu = ref({ visible: false, x: 0, y: 0, targetId: '' })

  function showTabContextMenu(e: MouseEvent, id: string) {
    // 菜单约 7 个菜单项，每个 ~28px，总计 ~210px
    const estimatedHeight = 210
    const menuWidth = 140
    const viewportHeight = window.innerHeight
    const viewportWidth = window.innerWidth
    // 垂直方向溢出检测
    const y = e.clientY + estimatedHeight > viewportHeight
      ? e.clientY - estimatedHeight
      : e.clientY
    // 水平方向溢出检测
    const x = e.clientX + menuWidth > viewportWidth
      ? e.clientX - menuWidth
      : e.clientX
    tabCtxMenu.value = { visible: true, x, y, targetId: id }
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
