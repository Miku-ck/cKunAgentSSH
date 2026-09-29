# S13 · 设置外壳 / 外观 / 桌面偏好（⚪ 低产出切片）

锚点 `a8e588c`。本切片的后端契约面（`/settings/*` 的 PUT 收敛、http 权威源、诊断掩码）已在 **S0** 与 **S10** 记账，此处只审渲染外壳与桌面偏好三腿契约。
`src/stores/settings.ts`、`src/components/settings/panels/*`、`src/api/client.ts` 均有另一会话的未提交改动 ⇒ 引 anchor。

## 结论：这一面干净，且是全仓跨层契约做得最好的样本

### 正例 1 · `shared/desktop-prefs.ts`(53) 作为唯一实现源
- 文件头把约束写死：「必须保持平台无关 — 不得 import electron / node 内置模块」，生效动作留 in `electron/desktop-prefs.ts`。⇒ 字段表/默认值/合法性只有一份。
- `normalizeDesktopPrefs`（`:35-42`）**只认真布尔**，注释给出了理由：「手改过的 json 里塞 `'no'`/1 时 `??` 会原样放行，于是 truthy 的 `'no'` 会让用户以为通知关了、实际还开着」。这正是本项目在 `int(x or default)` 上反复踩过的同一类 0/假值陷阱，在这里被提前挡住了。
- `prefsPatchOrNull`（`:45-53`）丢弃未知键与非布尔，空 patch 返 `null` 让调用方拒写 —— 「不传 = 不改」的契约在**输入侧**就被表达，而不是靠后端兜。

### 正例 2 · 渲染腿的乐观更新姿势（`src/composables/useDesktopPrefs.ts` 82 行全读）
`toggle()`（`:52-79`）：本地先翻转 → IPC 回包用**主进程权威值**经 `asPrefs` 覆盖 → 失败回滚到 `prev` 并 `toast.error` → `pending` 单键闸门防连点（只置灰当前开关而非整组）。`load()`（`:34-45`）对主进程回包再过一遍 normalize，注释写明理由「主进程回包可能缺字段（版本不匹配）」。浏览器态 `supported = !!api()?.setDesktopPrefs` ⇒ 面板置灰而非假装能改。
另外：Linux 的 `setLoginItemSettings` 是空操作，`applyAutoLaunch`（`electron/desktop-prefs.ts:101-106`）**直接拒绝开启**而不是回一个成功——「开着等于骗人」这条判断在代码里，不在文档里。

### 正例 3 · 0 值塌陷类在本切片已被系统性封堵
anchor `src/stores/settings.ts:136` 的注释解释了为何 `saved?.x ?? 默认` 不够，并统一走 `asNumber(value, fallback, lo, hi)`（`:150`）带区间夹紧：`maxCostUsd`（`:198`，fallback 0 / lo 0）、`maxTotalTokens`、`backgroundOpacity` 等 18 个数值项全部经这把尺子。`SchedulesView.vue:468-477` 的 `numOr` 则是 09-24 双轴轮补上的同款（把 `interval_minutes || default` 换掉），我逐行确认 6 个字段全在。⇒ **0 塌陷这一类在设置面已无残留**，与 `database.create_schedule`（09-23 修）+ `schedule_engine:366`（09-24 修）共同闭合。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 核验 |
|---|---|---|---|---|---|
| S13-01 | d | Minor | `stores/settings.ts` 的 PUT 发送点只有 3 处（post-process / updateModelConfig / setDefaultModel），其余设置全走「本地 localStorage + 由具体面板各自显式 PUT」——**双轨写侧**没有单一收敛点，新增一个后端设置项时没有任何机制提醒要接上 | anchor `src/stores/settings.ts:328,430,462` | ✅ |
| S13-02 | b | Minor | `MemoryPanel.vue:59` 注释仍写「PUT 是全字段覆盖」，与 a8e588c 统一后的「不传 = 不改」相反（S3 亦独立记为文档漂移，此处为唯一条目）。无行为后果：store 恒发全六键 | `src/components/settings/panels/MemoryPanel.vue:59` | ✅（S3 交叉） |

两条都是文档/组织层面，无用户可见错误行为。

## 未覆盖声明（诚实划界）

- `SettingsDialog.vue`(294)、`AppearancePanel.vue`(448)、`ChatPanel.vue`(262)、`AgentPanel.vue`(819) 的模板/样式/局部响应式未逐行审。其中 `AgentPanel.vue` 的**审批策略区**（`:127-134` 授权保留时长、`:457`、`:478` 兜底时长）是 **S2-03 那条 Major 的触发入口**——面板本身按 S2 结论只需知道「它调的是同步端点」，不需重审。
- `stores/settings.ts` 本轮只核了 anchor 的数值读取与 PUT 发送点。该文件正被另一会话改（`±12` 行，round-23），其在飞的 debounce/pagehide 行为我**未**审——历史上 `pagehide` 监听具名化 + `onScopeDispose` 注销已在 1eadce9 落库，属已定论区。
- `useAppearance.ts`(199) / `useSettings.ts`(20) / `useConfirm.ts`(51) / `layouts/TitleBar.vue`(195) / `settings/fields/*` 未审（⚪ 权重，且 09-24 双轴轮已扫过一批组件面）。
