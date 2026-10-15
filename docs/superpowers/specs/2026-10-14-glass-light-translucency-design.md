# 浅墨（glass-light）通透度修正：让材质真的透出来

日期：2026-10-14
状态：**待批准**
前置裁决（用户已定）：A 换成 Aero / B 先修通透度 —— **先做 B**。
与 `2026-10-14-aero-glass-theme-design.md` 的关系：不冲突、不替代。A 仍卡在 S0（本机 `electron.exe` exit 3，
acrylic 能否真透桌面未取证）。B 只动 `[data-theme="glass-light"]` 的令牌值，**三条结论是 A 亮档的前置**，
所以 A 批下来时 B 的成果直接搬走，不返工。

## 背景：浅墨现在看起来不像材质，像"带柔和阴影的浅色主题"

浅墨的身份是**材质**不是配色：`--canvas-bg: transparent` 让 body 渐变透到结构画布之下，
面板靠 `backdrop-filter` 把背后的颜色折射上来（机制见 spec 2026-10-13 §根因）。
暗玻璃这条成立，浅墨不成立 —— 原因是三条具体的、可算出来的机制错配，不是"审美不够"。

### D1 面板 α 在浅底上把渐变洗成了白，折射看不见

`variables.css:468,470` 现值压在本主题 `--bg-base: #eef1f7` 上的合成结果：

| 令牌 | 现值 | 合成后有效色 |
|---|---|---|
| `--bg-elevated` | `rgba(255,255,255,0.55)` | ≈ `#f8f9fc` |
| `--bg-input` | `rgba(255,255,255,0.45)` | ≈ `#f5f7fb` |

两者相距 2–3 级，且都已经贴到白顶：**面板后面画什么都被拉回白**，`blur(30) saturate(180%)`
折射上来的色相几乎没有落脚处。同一套 α 逻辑在暗档成立（半透白压在深板岩上仍有 8–16% 的
环境色漏上来），是因为暗底离白很远、浅底离白只有 15 级 —— **同一个 α 值在两种底上不是同一个透明度**。

### D2 `--border-default` 用白做线，在浅底上等于没有线

`variables.css:477` = `rgba(255,255,255,0.70)`，承担全仓 40+ 处结构线
（`SettingsDialog.vue:175`、`ConfirmDialog.vue:144`、`AuditView.vue:316`、`RunHistoryView.vue:205`、
`approval-card.css:68`、`AgentMemoryView.vue:615`、以及 `global.css:50` 的滚动条 thumb）。
白线画在 ≈`#f8f9fc` 的卡面上：浅墨的分层**只剩阴影**，对话框/表格/审批卡族边缘是糊的。

⚠️ 一处需要说破以免误判：招牌的首页命令卡 `.hcc` 用的是 `--border-subtle`（`HomeCommandCard.vue:486`，
墨 α 0.12）**已经是可见的**，所以"浅墨全糊"不成立 —— 病灶精确集中在 `--border-default` 这一族。

顺带一条约定冲突：Apple 浅色材质的分隔线本来就是**墨线**（`light` 档给的是 `#d0d0d0` ≈ 墨 0.19），
白断口高光是**暗**玻璃的语言。浅墨现在拿暗档的边框色搬到浅底，属于跨档照抄。

### D3 输入框比卡面还透：是"玻璃上挖的洞"，不是 macOS 的字段

`--bg-input` α(0.45) **低于** `--bg-elevated` α(0.55)。macOS 浅色模式的字段是"亮而干净的板"，
靠 1px 边框成形、不靠挖暗；而浅墨当前的方向是反的。同族先例见 `HomeCommandCard.vue:542-544`
的注释："先前它们是 `--bg-base`，比卡面还暗一档，看着像卡上挖的三个洞" —— 同一个病灶在按钮上已被抓过一次。

## 改动清单（全部在 `[data-theme="glass-light"]` 及其渐变覆盖块内）

| 令牌 | 现值 | 新值 | 依据 |
|---|---|---|---|
| `--bg-elevated` | `rgba(255,255,255,0.55)` | `rgba(255,255,255,0.40)` | D1：让折射有落脚处 |
| `--bg-input` | `rgba(255,255,255,0.45)` | `rgba(255,255,255,0.62)` | D3：字段反过来比卡面**更实**，文字无论背后是什么都可读 |
| `--border-default` | `rgba(255,255,255,0.70)` | `rgba(20,25,45,0.14)` | D2：墨线；与既有 `--border-subtle 0.12` / `--divider-primary 0.10` 排出"边框 > 细分隔 > 分隔"的正确强弱 |
| `[data-gradient="on"]` 覆盖 `--bg-elevated` | `0.50` | `0.34` | 用户明确开了渐变时更透（D1） |
| `[data-gradient="on"]` 覆盖 `--bg-input` | `0.40` | `0.55` | 同上，仍保持"字段比卡面实" |

线族取 `rgba(20,25,45,…)` 而非纯黑：与本块既有的 hover/active/divider/border-input 同一种冷墨，
不引入新色相。玻璃的高光身份**不靠边框承担**，靠 `--shadow-sm/-lg` 已有的
`inset 0 1px 0 rgba(255,255,255,0.80/0.90)` 顶缘内高光 —— 那才是光打在玻璃上沿的位置。

### 刻意不动

- `--bg-base` 保持 `#eef1f7`；`--canvas-bg: transparent` 机制不动（B 不动机制，只调透明度分配）。
- `global.css` 的 blur/saturate 参数**一处不改**（一次只动一个变量，改了看不出是谁的功劳）。
- 暗玻璃 `[data-theme="glass"]` 整块不动；其余 9 档不动；`:root` 默认值禁改（`variables.test.ts:207-218` 钉着）。
- 不新增任何 `:root` 令牌、不引入第三背景层级、不碰版面尺寸令牌。
- 名字仍叫「浅墨」：`晶白` 归 A 的命名裁决，B 不预支。
- `--bg-elevated-solid: #ffffff` / `--bg-input-solid: #f5f7fb` 不动（玻璃两档的 `-solid` 与实值不等是**材质主题的固有例外**，
  同值断言只钉手记，见 `variables.test.ts:161-168`）。
- `--card-perm-*` / `--danger-*` 那 13 条（`variables.css:532-544`）逐字保留。

## 测试与验证

1. **补一条守卫**（现状全文件零 `glass` 断言，两档覆盖集不对称只能人工兜）：
   照 `variables.test.ts:154-159` 手记那条的形状，加「`light` 覆盖的令牌，浅墨一个都不能少」。
   已核：当前 `glass-light` 64 条 ⊇ `light` 55 条，且 ⊇ `glass` 52 条 —— **这条今天即绿**，是防漂移不是补债。
2. `npx vitest run src/styles/variables.test.ts`。
3. `npx vitest run` 全量：基线 59 文件 / 758 条（`cleanup-fullcensus-port-core-2026-10`）。
4. 无 TS 改动，`vue-tsc` 两个 config 只需确认仍 0。
5. 手动矩阵（computed-style + 截图取证，沿用上一轮做法）：
   `浅墨 × {渐变关, 渐变开 glow/linear/radial × 强度 0/50/100, 自定义背景图}`，逐格看
   ① 面板是否真透出色相（D1 的验收点）② 对话框/表格/审批卡边缘是否有线（D2）
   ③ 输入框比卡面亮还是暗（D3）④ sticky 表头文字下面滚过内容还可不可读
   ⑤ 终端深底/标题栏/GPU 面板浅底翻牌的衔接。
6. 回归：暗玻璃与其余 9 档逐像素不变（只动 `glass-light` 两处块）。

## 风险与回落杠杆

- **R1 渐变关时分层变薄**：α 降到 0.40 后，无渐变可折射时卡面与 `#eef1f7` 底只差 6 级 + 一条墨线。
  回落杠杆（按此顺序试，一次一条）：① `--bg-base` 深一档到 `#e9edf5`；② `--bg-elevated` 回调到 0.46。
- **R2 sticky 表头可读性**：`global.css:161-170` 六个 frosted 例外面读 `--bg-elevated`，α 降了它们也降。
  若取证发现滚过的内容透上来，就在那一格里用 `color-mix(in srgb, var(--bg-elevated-solid) 66%, transparent)`
  单独提回一档 —— **不新增令牌、不写死 hex**（`global.css` 不在 `TOKENIZED_SCOPE` 的字面量禁区内，但仍守既有手法）。
- **R3 墨字压在渐变上**：结构面全透，`--text-secondary/tertiary` 是半透墨，遇到极端亮的渐变起始色时对比吃紧。
  这条**不是 B 引入的**（`--canvas-bg: transparent` 今天就是这样），记为已知取舍，由矩阵第 ① 格复核。

## 交给 A 的三条结论（写在这里是为了别丢）

1. 浅底档的面板 α **必须**低于暗档同位值，否则折射为白。
2. 浅底档的结构线只能是墨线，白边是暗档语言。
3. 字段靠 1px 线成形，不靠"比卡面更透"挖洞。
