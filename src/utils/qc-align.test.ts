/**
 * QuickCommandBar 顶线对齐算式的回归测试。
 *
 * 核心锁定对象：**QC 的 border-top 必须与 .input-container 的 border-bottom
 * 落在同一像素行**，而不是"差 1~2px 看着差不多"。
 *
 * 原实现用 `--qc-h = 工具栏高度 - 1`（32 - 1 = 31）反推，而 `--qc-h` 是
 * border-box 的总高、含 QC 自身那 1px 上边框 —— 方向减反了：QC 的线落到
 * 距底 30~31px，右栏的线在 32~33px，左栏那条线比右栏低 2px。
 * 下面第一个用例就是钉死这个方向：旧实现返回 31，必须失败。
 */
import { describe, expect, it } from 'vitest'

import { SEPARATOR_PX, qcHeightForAlign } from './qc-align'

/** 面板底边 = 窗口内容底边，两栏共用 */
const BASE = 1000
/** 常规宽度下 .input-toolbar = padding 4 + 控件 24 + padding 4 */
const TOOLBAR_NORMAL = 32

describe('qcHeightForAlign', () => {
  it('常规工具栏（32px）应返回 33，而不是把 1px 减反得到的 31', () => {
    expect(qcHeightForAlign(BASE, BASE - TOOLBAR_NORMAL)).toBe(33)
  })

  it('不变量：QC 上边沿与分隔线上沿同 y（换行导致工具条变高时同样成立）', () => {
    for (const toolbarH of [TOOLBAR_NORMAL, 56, 80, 24]) {
      const containerBottom = BASE - toolbarH
      const h = qcHeightForAlign(BASE, containerBottom)
      const qcTop = BASE - h
      // 分隔线上沿 = 下沿 - 1
      expect(qcTop).toBe(containerBottom - SEPARATOR_PX)
    }
  })

  it('不变量：QC 上边沿不得落到分隔线下沿（那是"修过头"，会与工具栏重叠 1px）', () => {
    const containerBottom = BASE - TOOLBAR_NORMAL
    const h = qcHeightForAlign(BASE, containerBottom)
    expect(BASE - h).not.toBe(containerBottom)
  })

  it('容纳亚像素坐标（DPR 缩放后 rect 常带小数）', () => {
    expect(qcHeightForAlign(BASE + 0.5, BASE - TOOLBAR_NORMAL + 0.5)).toBe(33)
    expect(qcHeightForAlign(BASE + 0.4, BASE - TOOLBAR_NORMAL + 0.2)).toBe(33)
  })

  it('布局未铺开（rect 为 0）或几何反序时返回 0，交调用方跳过写入', () => {
    // 未布局的 rect 是 0。若照常计算会得到 baseBottom + 1（≈ 整个面板高度），
    // 调用方的 `h <= 0` 检查拦不住正数 → QC 被撑满左栏。
    expect(qcHeightForAlign(BASE, 0)).toBe(0)
    // 分隔线落在基线之下：几何上不可能出现，同样视为无效
    expect(qcHeightForAlign(BASE, BASE + 40)).toBe(0)
  })

  it('分隔线紧贴基线时退化为 1px（0 会被调用方当作无效值丢掉）', () => {
    expect(qcHeightForAlign(BASE, BASE)).toBe(1)
  })
})
