<template>
  <div class="glg" :style="{ '--glg-lanew': `${laneW}px` }">
    <!-- 车道列：连线/圆点描边填充走 --glg-lane 车道色（色档类只声明局部令牌） -->
    <svg class="glg-svg" :width="laneW" :height="svgHeight" :viewBox="`0 0 ${laneW} ${svgHeight}`" aria-hidden="true">
      <path
        v-for="(e, i) in edges"
        :key="'e' + i"
        class="glg-line"
        :class="'glg-c' + (e.lane % PALETTE)"
        :d="e.d"
      />
      <circle
        v-for="(d, i) in dots"
        :key="'d' + i"
        class="glg-dot"
        :class="'glg-c' + (d.lane % PALETTE)"
        :cx="d.x"
        :cy="d.y"
        r="3.5"
      />
      <circle v-if="headDot" class="glg-ring" :class="'glg-c' + (headDot.lane % PALETTE)" :cx="headDot.x" :cy="headDot.y" r="6.5" />
    </svg>
    <div v-for="r in viewRows" :key="r.hash" class="glg-row" :title="r.hash">
      <span class="glg-refs">
        <span v-for="ref in r.refs" :key="ref" class="glg-ref" :class="'r-' + graphRefKind(ref)">{{ ref.replace('tag: ', '') }}</span>
      </span>
      <!-- 实时 HEAD 标记（巡检 SHA 定位）：主仓蓝 / 镜像绿，实心区别于远端跟踪引用的描边徽章 -->
      <span v-if="r.markers.length" class="glg-mks">
        <span
          v-for="(mk, i) in r.markers"
          :key="i"
          class="glg-mk"
          :class="mk.role === 'primary' ? 'mk-pri' : 'mk-mir'"
          :title="(mk.role === 'primary' ? '主仓实时 HEAD（巡检）' : mk.name + ' 实时 HEAD（巡检）') + ' · ' + r.hash.slice(0, 7)"
        >{{ mk.role === 'primary' ? '主仓' : mk.name }}</span>
      </span>
      <span class="glg-subject">{{ r.subject }}</span>
      <span class="glg-meta">{{ r.author }} · {{ r.relTime }}</span>
      <span class="glg-hash mono">{{ r.hash.slice(0, 7) }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// GitLaneGraph.vue — 提交图谱的 SVG 车道渲染。
// 布局来自 shared/git-repos-core.layoutGraphLanes（纯函数，单测同源）：
// 每条提交一行，车道号 + 指向父提交的边；--graph 的 ASCII 连接线行不参与
// 渲染（其拓扑已含在 parent 边里）。配色走令牌五档循环，连线/圆点用
// currentColor 继承色档类的 color，避免逐元素写描边规则。
import { computed } from 'vue'
import { graphRefKind, layoutGraphLanes, type GraphCommitRow } from '../../../../shared/git-repos-core'

const props = defineProps<{
  rows: GraphCommitRow[]
  /** 实时 HEAD 标记：SHA → 端点（role + name），由巡检的真实 SHA 定位提交行 */
  remoteMarkers?: Record<string, Array<{ role: 'primary' | 'mirror'; name: string }>>
}>()

const ROW_H = 24
const GAP = 12
const PAD = 10
const PALETTE = 5

const layout = computed(() => layoutGraphLanes(props.rows))
const laneW = computed(() => PAD * 2 + layout.value.laneCount * GAP)
const svgHeight = computed(() => layout.value.rows.length * ROW_H)

const x = (lane: number): number => PAD + lane * GAP + GAP / 2
const y = (row: number): number => row * ROW_H + ROW_H / 2

/** 同车道直落，跨车道三次贝塞尔（控制点取两行中点，与 git graph 的圆角斜线同形） */
const edges = computed(() => {
  const out: Array<{ d: string; lane: number }> = []
  layout.value.rows.forEach((r, i) => {
    for (const e of r.edges) {
      const x1 = x(r.lane)
      const y1 = y(i)
      const x2 = x(e.toLane)
      const y2 = y(e.toRow)
      const d = x1 === x2
        ? `M${x1} ${y1}L${x1} ${y2}`
        : `M${x1} ${y1}C${x1} ${(y1 + y2) / 2},${x2} ${(y1 + y2) / 2},${x2} ${y2}`
      out.push({ d, lane: r.lane })
    }
  })
  return out
})

const dots = computed(() => layout.value.rows.map((r, i) => ({ x: x(r.lane), y: y(i), lane: r.lane })))

/** HEAD 行加一圈外环（refs 含 HEAD -> … 的提交行） */
const headDot = computed(() => {
  const i = layout.value.rows.findIndex(r => r.row.refs.some(ref => ref.startsWith('HEAD')))
  if (i < 0) return null
  const lane = layout.value.rows[i].lane
  return { x: x(lane), y: y(i), lane }
})

/** 视图行：把 commit 摊平成普通字段（parseGraphLog 只返回提交行，commit 恒非空）；
 * markers 用巡检真实 SHA 命中本行提交（图谱 -n 截断之外的不显示）。 */
const viewRows = computed(() => layout.value.rows.map(r => ({
  hash: r.row.commit.hash,
  subject: r.row.commit.subject,
  author: r.row.commit.author,
  relTime: r.row.commit.relTime,
  refs: r.row.refs,
  markers: props.remoteMarkers?.[r.row.commit.hash] ?? [],
})))
</script>

<style scoped>
.glg { position: relative; font-size: 0.9231rem; }
.glg-svg { position: absolute; left: 0; top: 0; }

.glg-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  height: 24px;
  border-bottom: 1px solid color-mix(in srgb, var(--divider-primary) 70%, transparent);
  padding-left: calc(var(--glg-lanew) + 10px);
  min-width: 0;
}
.glg-row:hover { background: var(--bg-hover); }

.glg-refs { display: flex; gap: var(--spacing-4); flex-shrink: 0; }

/* 实时 HEAD 标记：实心小标，与描边的引用徽章区分开（避免和 gitee/main 这类远端跟踪引用混淆） */
.glg-mks { display: flex; gap: var(--spacing-4); flex-shrink: 0; }
.glg-mk {
  flex-shrink: 0;
  font-size: 0.7692rem;
  line-height: 14px;
  padding: 0 5px;
  border-radius: var(--radius-lg);
  font-weight: 600;
  white-space: nowrap;
}
.mk-pri { background: var(--accent-primary); color: var(--accent-on); }
.mk-mir { background: var(--accent-green); color: #04240f; }

/* 引用徽章：HEAD 实底强调，本地/远端/标签描边色分（不单靠颜色：文字本身可辨） */
.glg-ref {
  flex-shrink: 0;
  font-size: 0.7692rem;
  line-height: 14px;
  padding: 0 5px;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  white-space: nowrap;
}
.r-head { background: var(--accent-primary); border-color: var(--accent-primary); color: var(--accent-on); }
.r-local { color: var(--accent-purple); border-color: color-mix(in srgb, var(--accent-purple) 45%, transparent); }
.r-remote { color: var(--accent-cyan); border-color: color-mix(in srgb, var(--accent-cyan) 45%, transparent); }
.r-tag { color: var(--accent-yellow); border-color: color-mix(in srgb, var(--accent-yellow) 45%, transparent); }

.glg-subject {
  flex: 1;
  min-width: 0;
  font-size: 0.9231rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.glg-meta { font-size: 0.8462rem; color: var(--text-tertiary); white-space: nowrap; flex-shrink: 0; }
.glg-hash { font-size: 0.8462rem; color: var(--accent-orange); width: 56px; flex-shrink: 0; }

.glg-line { fill: none; stroke: var(--glg-lane); stroke-width: 1.5; opacity: .9; }
.glg-dot { stroke: var(--bg-base); stroke-width: 2; fill: var(--glg-lane); }
.glg-ring { fill: none; stroke: var(--glg-lane); stroke-width: 1.2; opacity: .75; }

/* 车道色档：只声明局部令牌 --glg-lane（填充/描边可读强调色；文字色另有 --accent-text 口径） */
.glg-c0 { --glg-lane: var(--accent-primary); }
.glg-c1 { --glg-lane: var(--accent-purple); }
.glg-c2 { --glg-lane: var(--accent-green); }
.glg-c3 { --glg-lane: var(--accent-orange); }
.glg-c4 { --glg-lane: var(--accent-cyan); }

.mono { font-family: var(--font-mono); }
</style>
