# 玻璃主题改造：macOS vibrancy → Windows 7 Aero 玻璃

日期：2026-10-14
状态：**S1、S2 已落地；S3 只落了「顶缘镜面」一项**（半透条带 / 标题字辉光 / 三颗光泽胶囊未做，见实施回填 §I4）；**S4 仍卡在 S0 门禁**。
⚠ **一条批次冲突待用户裁**：同日 `2026-10-14-glass-light-translucency-design.md`（下称 B）顶部记着用户已裁「先做 B」，其 D1 要求亮档 `--bg-elevated` 从 0.55 **降到 0.40/0.34**；本批给的是 **0.62/0.55**，方向相反。本文件写于 B 之前、实施时未回头核对 specs 目录的兄弟决策 —— 详见文末「审查后新增开放项」第 1 条。

## 用户裁决（已定，不再讨论）

1. 折射机制走 **窗口级真透视桌面（`backgroundMaterial: 'acrylic'`）**，不是只换 CSS 参数。
2. **替换**现有两档，不新增第三档、不留 macOS 形态的切换开关。
3. 无参照图，按 **Win7 默认蓝玻璃（Aero）** 的通用常识复刻，先出一版再由用户逐条批注。

「Win7 风格」= 视觉复刻。本仓 Electron 35 跑不到 Windows 7 上（Chromium 早已不支持该 OS），
所以目标不是"能在 Win7 跑"，而是"在 Win11 上看着像 Aero"。

## 已查实的环境事实

| 事实 | 出处 | 影响 |
|---|---|---|
| 本机 Windows 11 Pro build **26300** | `os.release()`/`os.version()` | ≥ 22621，acrylic 的版本门槛满足 |
| Electron **35.7.5** | `node_modules/electron/package.json` | `backgroundMaterial` / `setBackgroundMaterial()` 可用 |
| 应用**强制全禁 GPU**：`disable-gpu` `disable-gpu-compositing` `disable-gpu-sandbox` `disable-software-rasterizer` | `electron/main.ts:1564-1570`（`76956f2` 引入，本机驱动与 v35 不兼容，不禁就 exit 3） | **本方案最大未知数**，见 S0 |
| 窗口已无边框：`frame:false` + `titleBarStyle:'hidden'` + `backgroundColor:'#0d1117'`，全仓唯一 `new BrowserWindow` | `electron/main.ts:129-145` | Aero 标题栏只能自绘（现状已是自绘 `src/layouts/TitleBar.vue:99`）；改材质只动这一处 |
| 全仓无 `transparent` / `vibrancy` / `setBackgroundColor` / 外观类 IPC 通道 | grep 证实 | acrylic 是净新增能力，不与既有窗口代码打架 |
| 现有玻璃是**纯 CSS**：`--canvas-bg: transparent` 透出 body 渐变 + 卡片 `backdrop-filter` | `src/styles/variables.css:380,466`、`src/styles/global.css:103-170` | 「应用内壁纸折射」这层机制要保留（它是 S4 失败时的回落形态） |
| `global.css:21-25` 曾**刻意做到不依赖窗口 backgroundColor**（浅墨出现「亮蓝→黑带→浅底」割裂后改成叠层式） | 上一轮 spec 实施回填 §1 | acrylic 恰恰要求窗口透明 —— 这两条正面冲突，S4 必须重排层级并重新取证 |

## S0 可行性门禁（先做，不过就不碰 S4）

要回答的唯一问题：**在 `disable-gpu + disable-gpu-compositing` 下，`backgroundMaterial:'acrylic'`
还能不能真透出模糊桌面？**

DWM 负责窗口背后的模糊，Chromium 只负责把内容按 alpha 合成到窗口表面 —— 理论上软件合成也能成，
但 `transparent` / 无 GPU 的组合在 Electron 上是历史高发坑。**不实测不许写进实现。**

探针已备好（在仓库外，未污染工作树）：`%TEMP%\aero-probe\`（`main.cjs` 起一个 640×360 无边框窗，
按 `PROBE_MODE` 组合 material/transparent/GPU 开关，2.2s 后用 `desktopCapturer` 截全屏存 PNG，自退）。
跑法：`node %TEMP%\aero-probe\run.cjs mat0-gpuoff mat0-gpuon trans-gpuoff`，然后人眼看 PNG 里
窗口那块是「模糊壁纸」还是「纯黑/清晰壁纸」。

**当前阻塞**：本机连 `node_modules/electron/dist/electron.exe --version` 都是 exit 3 零输出，
且 `dist/` 在 11:12 被并行打包会话重写过 —— 与 `packaging-exe-2026-10` 记的 AV 独占/mmap 症状同族。
等并行打包收尾后重跑；若探针始终起不来，S0 记为「本机无法验证」，S4 挂起、只交付 S1–S3。

判定表：

| S0 结果 | 走法 |
|---|---|
| gpuoff 下 acrylic 真透桌面 | S1–S4 全做 |
| 仅 gpuon 可用 | S4 加「GPU 可用才开 acrylic」门控；dev/生产在本机自动回落 CSS 形态 |
| 都不行 / 起不了探针 | 停 S4。Aero 观感由 S1–S3 的 CSS 层承担，把「真透视」记为待环境修复 |

## 主题命名：复用 `glass` / `glass-light` 两个键

不新起名（`aero`/`aero-light`），理由：用户存的是 `settings.appearance.theme`，`asEnum` 遇到不认识的值
**静默回落 'dark'**（`src/stores/settings.ts:257`）—— 改键名等于把正在用玻璃的人悄悄踢回深色主题。
留中文标签承担"这是 Aero 了"的表达：`玻璃` 保持，`浅墨` → **`晶白`**（待用户认可）。

代价：`data-theme="glass"` 这个属性值与 Aero 语义不再贴字面，靠注释与文档纠偏。

## 改动范围（全量触点清单，逐条打勾）

### 主题名注册点（漏一处即编译红或静默丢档）

- `src/utils/appearance-tokens.ts:18` — `Theme` 联合类型（键名不变则**无需改**，仅注释）
- `src/stores/settings.ts:189` — `THEMES` 白名单（同上）
- `src/components/settings/panels/AppearancePanel.vue:329-330` — `themeLabels` 中文名
- `src/monaco-setup.ts:72-73` + 注释 `70-71` — `THEME_MAP`（键不变则只改注释：编辑器不跟玻璃材质）
- `DESIGN.md:56` — **文档里的 Theme 枚举清单今天本就漏了 glass/glass-light**，这次补齐两档并写明 Aero

### 令牌层 `src/styles/variables.css`

| 位置 | 动作 |
|---|---|
| `341-373` 块头注释（通篇 macOS/iOS/Sonoma 对标） | 整段重写为 Aero 材质口径 |
| `374-452` `[data-theme="glass"]` 52 条 | 重写为 Aero 暗蓝玻璃 |
| `456-459` `[data-theme="glass"][data-gradient="on"]` 2 条 | 保留组合块结构，参数按 Aero 重设 |
| `461-463` 浅墨块头注释 | 重写 |
| `464-545` `[data-theme="glass-light"]` 64 条 | 重写为 Aero 亮玻璃（**必须保住 532-544 那 13 条 `--card-perm-*`/`--danger-*`**，暗档没有它们，覆盖集不对称是现状） |
| `548-551` 渐变覆盖块 | 同上 |
| `:root:139` `--canvas-bg: var(--bg-base)` | **不许动**（`variables.test.ts:207-218` 钉了逐字默认值，动了拖坏其余 9 档） |

Aero 材质要点（对应用户说的"win7 风格"）：

- **蓝调玻璃**而非 macOS 的中性白灰：面板底色带青蓝（暗档约 `rgba(140,195,240,0.18)`，亮档约 `rgba(232,244,255,0.55)`），`saturate` 比现状高一档。
- **玻璃壁厚 + 镜面**：`inset 0 0 0 1px` 内圈白边（Aero 那圈"玻璃厚度"）叠 `inset 0 1px 0` 顶缘高光；外阴影带一点冷蓝辉光（Aero glow）。
- **圆角收窄**：现状 6/10/14 是 macOS 大圆角，Aero 取 **3/5/8**。守 `DESIGN.md:27`「主题块不改尺寸令牌」的既有例外做法（玻璃今天已在改 radius/shadow，注释要说明这是材质不是版面）。
- **禁止**在主题块写 `--accent-primary/-rgb/--accent-on`（`variables.test.ts:197-205` 硬守卫）——Aero 的蓝只能来自 bg/边框/渐变，不能劫持强调色系统。
- 终端仍 `--bg-terminal` 深底、Monaco 仍走 `ckun-dark/light`（xterm 不支持透明，既有约定不因 Aero 破例）。

### 折射层 `src/styles/global.css`

- `103-110` 段注释（macOS 材质口径）→ 重写为 Aero；`111-121` / `123-126` / `128-138` / `140-143` 四段 blur 参数按 Aero 提一档（大浮层/中卡片/sticky 三档结构不变）。
- `145-159` 与 `161-170` 的 **6 个 sticky/对话框高特异度覆盖**（`.audit-table th` `.ptable th` `.rhv-table th` `.rm-row-header` `.file-list-head` `.dt-dialog`）：保留 (0,3,*) 提权手法，只换参数。
- `21-27` 的 body 叠层式背景（`background-color` + `background-image`）**保留**；S4 若开 acrylic，需在这段旁边补一条「窗口透明时透出顺序」说明并重新取证。
- `43-46` 通用 `[data-gradient="on"]` 的 `color-mix(...-solid)` 派生会把面板拖回实心，玻璃两档靠 (0,2,0) 组合块绕开 —— 新档必须沿用同一手法，否则渐变开启即回到"闷"。
- 无关的 `blur(2px)` 遮罩 scrim（9 处，`SettingsDialog.vue:153` 等）**不动**。

### 组件级主题分支（全仓只有两处，已穷尽）

- `src/components/settings/panels/AppearancePanel.vue:16-22` — 渐变关闭时的提示文案，措辞从「玻璃质感」改 Aero 口径（键名不变则 `v-if` 条件不用动）。
- `src/components/ssh/GpuNvtopPanel.vue:673-680` — 浅底翻牌三档列表含 `glass-light`（`668-672` 注释同改）。**键名复用 = 这里零改动**，这是复用键名的第二个理由。

### Aero 窗口外壳（S3，唯一新增视觉面积）

`src/layouts/TitleBar.vue`：玻璃标题栏（条带本身半透 + 顶缘镜面）、标题字带轻微辉光、
最小/最大/关闭三颗改 Aero 光泽胶囊（关闭那颗带红调）。
约束：`layouts/` 在 `variables.test.ts:282-326` 的字面量禁区内 —— 所有颜色必须走既有令牌，
或在块内声明成 `--x: #hex` 自定义属性（豁免形状只有这一种，`GpuNvtopPanel` 立的先例）。

### 窗口材质（S4，受 S0 门禁）

`electron/main.ts:129-145` 加 `backgroundMaterial: 'acrylic'` + `backgroundColor: '#00000000'`，
门控条件 = build ≥ 22621 且（按 S0 结论决定是否再看 GPU 可用性）；不满足则保持现状 `#0d1117`。
不开新 IPC、不加设置开关（YAGNI：材质是主题的一部分，不是用户可调项）。
注意 `backgroundMaterial` 与 `transparent: true` 的文档口径互相矛盾（一处说配透明背景色，一处说不能同用于 transparent），**以 S0 实测为准**。

## 测试与验证

1. `npx vue-tsc --noEmit -p tsconfig.web.json` / `-p tsconfig.node.json`（带 `--noUnusedLocals --noUnusedParameters`）各 0 错。
2. `npx vitest run src/styles/variables.test.ts`：主题块只含令牌无嵌套、无孤儿 `:root` 令牌、
   `STRUCTURAL_CANVASES`（`123-127`）仍读 `--canvas-bg`、`sshView` 仍读 `--bg-terminal`、裸 hex 守卫（`282-326`）全绿。
   ⚠️ 该守卫**对玻璃两档没有任何专项断言**（全文件零 `glass` 字面量，也没有 light⊆glass 或 `-solid` 同值检查）——
   所以"两档覆盖集合不对称"这件事只有人工矩阵能兜。
3. `npx vitest run src/stores/settings.test.ts`：现状 `403-429` 只测 `neon→dark` 与 `forest`，
   **没有 glass 用例**（上一轮 spec 自称测过，实际没落）。本次补一条 `theme:'glass'→'glass'` 放行断言。
4. `npx vitest run` 全量：基线 59 文件 / 758 条（`cleanup-fullcensus-port-core-2026-10`）。
5. 手动矩阵（浏览器 computed-style 取证，上一轮的做法）：`{暗玻璃, 亮玻璃} × {渐变关, 渐变开 glow/linear/radial × 强度, 自定义背景图}`，
   逐格看折射透出、文字对比、标题栏与 sticky 表头、终端/Monaco 衔接、GPU 面板浅底翻牌。
6. 回归：**其余 9 档主题逐像素不变**（只动 `[data-theme=glass*]` 两处块与 glass 专属 blur；`:root` 默认值禁改）。
7. S4 另加：`npx electron .` 起真窗，截图看窗口之外区域与窗口之内的模糊边界，并验 Win10 版本门控回落。

## 风险与回退

- **R1（最高）**：本机禁 GPU 下 acrylic 可能整窗黑或不合成 → 由 S0 拦在开工前；S1–S3 与 S4 是两个独立提交，S4 可单独 `git revert`。
- **R2**：窗口透明后 `global.css:21-25` 那条"不依赖窗口底色"的修正被重新引入依赖 —— 渐变透明处会露出系统桌面。缓解：acrylic 只在玻璃两档开，且 body 仍保 `background-color: var(--bg-base)` 打底。
- **R3**：并行会话正在动 `electron/main.ts`（打包那条线）与 `SftpView.vue` 一族，`TitleBar.vue` 也在玻璃触点里 —— 实施按文件切提交 + 显式 pathspec，避开在途文件。
- 回退：改动集中在 6 个文件的样式/注册层 + `main.ts` 一处窗口选项，两个提交各自可独立 revert。

## 实施顺序

S0 探针 →（通过则）S1 令牌两块 → S2 blur 参数 → 注册点/文案/注释同步 + 补测（第 3 项）→ 手动矩阵取证
→ S3 标题栏外壳 → S4 窗口材质（独立提交，门禁未过则跳过并留档）。

---

## 实施回填（2026-10-14）

S1–S3 已落，S4 未落（S0 仍无答案）。改动集中在 6 个文件，`:root` 与其余 9 档主题零改动（已按归一 CRLF 与 HEAD 逐字比对取证）。

### 落了什么

| 阶段 | 落点 |
|---|---|
| S1 令牌 | `variables.css` 玻璃块头重写为 Aero 三条材质原理；`[data-theme=glass]` 52 条、`[data-theme=glass-light]` 64 条**只换值不换令牌名**（覆盖集合前后完全一致，实算钉过）；两档渐变覆盖块按 Aero 重设 α |
| S2 折射 | `global.css:103-178` 四段 blur + 两段 frosted 的 saturate 提档（185–215%，玻璃区外 0 处 saturate） |
| S3 窗框 | `global.css:182-192` 标题栏顶缘镜面两档 —— 用 `:root[data-theme=…]` 提权，**没有**进 `TitleBar.vue`（layouts/ 在裸色值禁区内，且写进组件等于让其余 9 档长出窗框高光） |
| 注册点/文案 | `AppearancePanel.vue:16-23` 渐变提示改口、`:330` 标签 `浅墨 → 晶白`；`GpuNvtopPanel.vue:671`、`monaco-setup.ts:70`、`global.css:24` 注释改口；`DESIGN.md:42,44,56,59` 补齐（Theme 清单原本就漏玻璃两档）；本 spec 与 2026-10-13 那份的取代关系写在旧件头部 |

键名复用这条按计划生效：`Theme` 联合 / `THEMES` 白名单 / `THEME_MAP` / `GpuNvtopPanel` 浅底翻牌清单**零改动**，存量用户的 `settings.appearance.theme` 不会被 `asEnum` 静默踢回 dark。

### 与设计稿的一处偏离（实施后审查发现，已改）

设计稿把「壁厚 + 镜面」两道 inset 一起塞进 `--shadow-sm` 和 `--shadow-lg`。实测这是错的：`--shadow-lg` 的 27 个消费方里 **22 个本来就画着 `border: 1px solid var(--border-default)`**（`.modal-dialog` / `.dt-dialog` / `.editor-dialog` / 三处 `.ctx-menu` / `.dialog` / `.ntm-menu` / `.hcc`…），inset 环落在描边**内侧 1px** = 同一只窗框画两遍；而我同时把 `--border-default` 从 0.20 提到 0.38，外线更亮、双线更显，Aero 最讲究的那条切边正好被毁掉。

**现规矩（第二版，代码审查后）**：壁厚环 `inset 0 0 0 1px` **两档 shadow 都不进**。顶缘镜面 `inset 0 1px 0` 保留（它贴内缘、不贴轮廓，与任何描边都不重叠），切边由各面自己的描边承担，`--border-default` 提到 0.38/0.85 就是为它准备的。唯二没有描边、又被测试钉死不许加边框的浮层（`.hcc-menu` / `.htt-menu`）在 `global.css` 的玻璃作用域里补一根**外**发丝 `0 0 0 1px var(--border-default)`。

两次都错在同一处，值得留档：第一版把环塞进 `--shadow-lg`，第二版「修好」后把它留在 `--shadow-sm` 并写下「4 个消费方全都无描边」—— 那句是假的，因为我**只按 `--border-default` 一种写法扫过**就下了结论。实测 `--shadow-sm` 的 4 个里 3 个有描边（`.conn-card`、`.tunnel-row` 用 `--border-subtle`，`.nvtop` 用固定深色的 `--nv-frame`），双线框原样留在了另一侧，且 `.nvtop` 上最刺眼（浅蓝内线打在 `#050505` 面板上）。另：`.hcc-menu` 的填充与宿主 `.hcc` 读**同一枚** `--bg-elevated`，玻璃下两层同色 —— 只删环不补外发丝的话，那层菜单与身后就只剩投影分界。

### 审查后新增开放项（2026-10-14，代码审查回来的东西）

1. **C1｜与 B 批次的亮档 α 正面冲突（要用户裁，不是代码问题）。** B（`2026-10-14-glass-light-translucency-design.md`，mtime 14:25，早于本批 15:19 的令牌改动）顶部记「用户已裁：先做 B」，其 D1 处方是亮档 `--bg-elevated` 0.55→**0.40**、渐变开 0.50→**0.34**，诊断原话是「面板 α 在浅底上把渐变洗成了白，折射看不见」。本批给的是 0.62 / 0.55，**比 HEAD 的 0.55/0.50 还高**，等于把 B 的病灶加重。B 第 69 行还明写 `--bg-elevated-solid: #ffffff` / `--bg-input-solid: #f5f7fb` **不动**，本批分别改成了 `#f7fbff` / `#ffffff`。两边对同一枚令牌给了相反处方，必须先裁：B 被 A 取代（那 B 的状态行要改），还是亮档按 D1 把 α 降下来、蓝调另由色相承担（那 `variables.css` 亮档那块要重写）。**我没有替两边做主。**
2. **I3｜「0 回归 / 差值 ≤0.3」这句口径偏窄。** 复算支持其中关键的一半：标题栏纯强调色带上暗档 `--text-primary` blue 3.41→3.14、mint 1.21→1.19、white 1.07→1.08，**两版同挂且 Δ≤0.32** ⇒「渐变带那条洞是既有的、不是 Aero 挖的」成立；晶白反而多格变好。但把 `--bg-hover`/`--bg-active` 的叠层算进格集就不止「≤0.3」：暗档 hover/active 从 0.10/0.16 的白换成 0.16/0.26 的亮蓝（pressed 底亮度约 +40%），文字只换成冷白，两向叠加 —— 例：`SftpPane.vue:566 .badge` 暗档 3.95→3.36、`HomeCommandCard.vue:688 .hcc-send:hover` 8.60→6.76、暗档卡面正文 13.53→11.71。都还达标，但**远超那句承诺的范围**。补暗档余量的两条杠杆（提文字 α / 压 hover-active 的蓝）留给用户与 C1 一起定。
3. **晶白 `--text-tertiary` 是全仓唯一不达标的 tertiary**：压在自身 `--bg-base` 上、**渐变关** = 3.27:1（其余 9 档与 `:root` 是 5.10–8.43；HEAD 的 0.48 是 3.13，同样不合格 ⇒ 既有，非本批引入）。要修得同时动 secondary 才保得住三级层级（0.70 的 secondary 与达标的 tertiary 会撞在一起），属亮档 α 那一批，与 C1 同裁。`DESIGN.md:42` 已按实测改写措辞，不再把不合格只归因于渐变带。
4. **M7｜全仓没有任何测试读过 `[data-theme="glass*"]` 块。** `variables.test.ts` 的 `blockOf()` 只对 `atelier`/`light` 调用过 —— 本批自证过的那套覆盖集合断言只活在一次性脚本里，没进套件。后果具体：**只改错 CSS 选择器名（`glass-light` 打成 `glass-ligth`）会让整档静默退回 `:root` 深色值而四条闸门全绿**。B 的测试第 1 条（补 `light` ⊆ 亮玻璃守卫，今天即绿、纯防漂移）两批都没落。补它比人工矩阵便宜得多。
5. **M5｜亮档 `-solid` 没跟着染色**（`#f7fbff` / `#ffffff`），而暗档已换 `#1e3246` / `#16283a`。`--bg-elevated-solid` 有 10+ 真实消费方（Toast、HistoryPanel、SideChatPanel 多处、`.htt-menu`…），晶白这些实心底仍是 macOS 中性白 —— 与 C1 的 B 第 69 行「不动」直接相关，同批裁。

### S0 复跑结果与一条被证伪的归因

原 §当前阻塞写「`dist/` 被并行打包会话重写过 → 与 AV 独占/mmap 症状同族」。**这条归因不成立**：

- `ELECTRON_RUN_AS_NODE=1 …electron.exe -e` → **exit 0**（二进制完好、Node 运行时正常）
- 同一个 exe 走浏览器进程（`--version`）→ **exit 3 零输出**；探针四格全 `0x80000003`，PNG 从未产出
- 探针的 `--user-data-dir` 根本没被创建，`Crashpad` 与事件日志 20 分钟内零 electron 条目 → 连崩溃报告都没生成

文件确实被在跑的 4 个 `electron.exe` 独占（`open r+` 实测 EBUSY），但 Windows 上执行中的 exe 映像被锁是常态，不是故障。**真正的墙是这条 shell 起不了 GUI 浏览器进程**，而 `9ed8b12` 的启动崩溃修复对用户正在跑的 app 是有效的。 ⇒ S0 的结论只能由**用户在普通终端**跑 `%TEMP%\aero-probe\run.cjs` 得出，本窗口无法代验；判定表原样保留。

### 已知开放项（都要用户点头，不是漏看）

1. **渐变带下的对比度**（既有，非 Aero 引入）：玻璃两档结构面是 `--canvas-bg: transparent`，而渐变起点就是用户选的 `--accent-primary`（`useAppearance.ts:122`）。实算 151 格 `<4.5:1`，其中**每一格 HEAD 版本同样挂**（差值 ≤0.3），且 12 格比改前更好 —— 所以这不是回归，是这条模型本来就有的洞：标题栏（0–32px ≈ 视口顶部 4%）在默认强度下几乎直接坐在纯强调色上，白/薄荷/蜜蜂/珊瑚等浅色强调色时 `--text-secondary` 只有 1.0–1.6:1。状态色更宽：全仓 `--status-*-text` **66 处裸用**（不与自己那层 `--status-*-bg` 成对），它们一并暴露。修法三条各有代价（结构面给半透地板 / 渐变在玻璃档混向 `--bg-base` / 提示文案改口），**没有替用户选**；`DESIGN.md:42` 那条「≥4.5:1」的承诺已就地注明玻璃档例外。
2. `AppearancePanel` 的提示在**劝用户开渐变**，而渐变正是上一条的触发条件 —— 措辞要不要改，与第 1 条一起定。
3. 2026-10-14 登记的三条 blur 清单漏项（`.editor-dialog`、ModelsPanel `.modal-container`、SftpTransferDialog `.ctx-menu`）当轮裁决是「只做 A」，本轮未动。
4. 浅档 `--card-perm-*` / `--danger-*` 那 12 条是从 `light` 抄来的暖米黄+暖红，压在冷蓝玻璃上色温打架；暗档根本没有这 12 条（读 `:root` 的暖棕），换底色后同样显眼。属"要不要给 Aero 单独调一档语义色"的决定。
5. 圆角 14→8 波及 `--radius-*` 的 106 处消费方。其中 `DockerView .host-sel`（26px 高）原先 14px 圆角接近胶囊，现在变成圆角矩形 —— Aero 本来就该是方的，但这类形状变化只能靠肉眼验收，本机无法视觉回归。

### 验证口径（诚实记录实际跑到了什么）

- **跑成了**：静态取证 32 项全绿（覆盖集合 52/64 条前后一致、12 条 card-perm/danger 在位、块内无嵌套、不写强调色、`:root` 整块与 HEAD 归一后相同、两文件括号平衡、12 处 backdrop-filter、saturate 全在玻璃区、标题栏镜面只挂两档）；对比度审计 0 回归 / 12 格改善；`git diff` 改动面确认只落在玻璃区（两个 hunk，均在 341-565 行内）。
- **四条闸门（同日补齐，一律取真实退出码）**：`npx vitest run` → RC=0，**60 文件 / 760 例**（基线 759 + §测试与验证第 3 项那条 `theme:'glass'→'glass'` 放行用例，已落）；`npm run typecheck`（web + node 两段串联）→ RC=0；`npm run build` → RC=0 / 41.03s。build **第一次报 RC=1 是沙箱 Temp 目录 ACL 假红**（`[vite:worker] remove …AppData\Local\Temp\esbuild-<hash>: Access is denied`，这次撞的是 `json.worker.js`、上一轮是 `css.worker.js` —— 报错文件不固定，别拿它当判据），把 `TMP/TEMP/TMPDIR` 指到仓库内 `tmp/` 后同一条命令即绿；注意 `TMP="$TEMP"` 是空操作（那个目录本来就是被挡的一个）。顺带自证一次老教训：`npm run build 2>&1 | tail` 后面的 `echo $?` 拿的是 `tail` 的码，我一度把 RC=1 读成 RC=0。
- **新用例的探红**：往断言里临时塞一个不存在的键 `glass-bogus` → `AssertionError: expected 'dark' to be 'glass-bogus'`，证掉"这条永远绿"的可能。变异只在测试文件内进行，没动共享生产代码，探完撤回。这条钉住的正是「主题命名」那节的键名复用决定：**谁改成 `aero`/`aero-light`，这里当场红** —— 因为 `asEnum` 的回落不吭声，用户只会看到界面变暗、说不清主题去哪了。
- **视觉**：仍未肉眼验收（视口 0×0、`take_screenshot` 被拒），按本仓既有口径只能算 computed-style 级证据；观感需用户在应用里切到「玻璃 / 晶白」逐条批注。
