// useTabContextMenu.ts — 终端标签右键菜单
import { ref, onMounted, onUnmounted } from 'vue'
import { menuLeft, menuTop } from '@/utils/menu'

export function useTabContextMenu() {
  const tabCtxMenu = ref({ visible: false, x: 0, y: 0, targetId: '' })
  // 修复：标记菜单是否刚被显示，避免同一次右键事件立即触发 dismiss
  let justShown = false

  function showTabContextMenu(e: MouseEvent, id: string) {
    // 菜单约 7 个菜单项，每个 ~28px，总计 ~210px
    tabCtxMenu.value = {
      visible: true,
      x: menuLeft(e.clientX, 140),
      y: menuTop(e.clientY, 210),
      targetId: id,
    }
    justShown = true
  }

  function dismissTabCtxMenu() {
    // 修复：如果菜单刚被显示，忽略本次 dismiss（避免同一次右键事件立即关闭菜单）
    if (justShown) {
      justShown = false
      return
    }
    tabCtxMenu.value.visible = false
  }

  onMounted(() => {
    document.addEventListener('click', dismissTabCtxMenu)
    // 修复：右键点击其他标签时也关闭菜单，避免多个菜单同时存在
    document.addEventListener('contextmenu', dismissTabCtxMenu)
  })

  onUnmounted(() => {
    document.removeEventListener('click', dismissTabCtxMenu)
    document.removeEventListener('contextmenu', dismissTabCtxMenu)
  })

  return { tabCtxMenu, showTabContextMenu }
}
