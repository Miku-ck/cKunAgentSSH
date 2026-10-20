# SFTP 路径收藏 — 设计文档

日期：2026-10-20
状态：已确认，进入实施

## 目标

在 SFTP 双栏文件浏览器中提供路径收藏：收藏当前目录、点击收藏项一键跳转、删除收藏项。本地 / 远程两侧互相隔离，远程收藏按连接主机分档。

## 交互设计

设计图：`.workbuddy/design/sftp-bookmark-design.png`（v1 初稿，含标题/底注）。落地版按「简洁优先」去掉了面板标题、当前路径副行、底注与空状态图标，最终形态见 `.workbuddy/design/slim-dark.png`、`slim-empty3.png`、`slim-light.png`。

- **入口**：`pane-actions` 中新增书签图标按钮，位于「上一级」与「刷新」之间，规格与 `.pane-btn` 一致（22×22，12px 线性图标）。
- **当前路径状态**：当前 `cwd` 已在收藏中时，按钮图标转为 `--accent-cyan` 并做轻填充；未收藏为线框灰。不打开面板即可判断。
- **面板**（点击按钮展开，272px，右对齐，`--bg-elevated-solid` + `--border-default` + `--shadow-lg`），只保留三段：
  1. 动作行：书签图标 + `收藏当前路径` / `取消收藏当前路径`，右侧显示收藏数量
  2. 分隔线（仅在有收藏时出现）
  3. 收藏列表：名称（路径末段）+ 真实路径两行；行悬停时右侧出现 × 删除
- **空状态**：单行灰字「还没有收藏路径」，不多占高度。
- **不放的冗余**：面板标题、当前路径复读、按主机隔离说明、空状态图标与副标题——凡与按钮 title 或面包屑重复的内容一律不进面板。
- **关闭**：点击面板外、`Esc`、跳转成功后关闭。

## 边界规则

| 场景 | 行为 |
| --- | --- |
| 远程未连接 | 触发按钮禁用（`opacity .4`，与「上一级」一致） |
| 当前路径为空 | 动作行禁用，列表仍可浏览 |
| 收藏重复路径 | `toggle` 语义天然去重，同一路径只存一条 |
| 超过 50 条 | 拒绝新增，toast「收藏上限 50 条…」 |
| 跳转目标不存在 | 沿用 `toFriendlyMessage` toast，面板保持打开、不关闭 |
| localStorage 脏数据 | 逐项过滤（非对象、无 `path`、重复 path 丢弃），与 `quickCommands.load()` 同策略 |
| 主键缺失 | `genId()` 补；`order` 非有限数时退回位置序号 |

## 数据模型

```ts
type PathBookmark = { id: string; label: string; path: string; order: number }

// localStorage key: 'ckun-ssh-sftp-bookmarks'
// {
//   local: PathBookmark[],
//   remote: Record<string /* connectionHost */, PathBookmark[]>
// }
```

- `hostKey` = `sftpStore.connectionHost`：`connect()` 时为 `user@host:port`，`connectFromTerminal()` 时为 `host`；切连接自动换列表。
- `label` 由路径推导：去掉尾部分隔符后取末段；根路径（`/`、`C:\`）原样返回。
- 单侧上限 `MAX_BOOKMARKS_PER_SCOPE = 50`。
- `toggle()` 返回 `added | removed | limit | noop`；`noop` 表示空路径或远程缺 hostKey，调用方不得给成功提示。

## 组件结构

```
SftpPane.vue (×2)
└── <SftpBookmarkMenu :side :cwd :online />
     ├── .bm-trigger 触发按钮（scoped 样式无法继承父级 .pane-btn，规格复制并注明）
     └── .bm-dropdown 面板
```

- `src/stores/sftpBookmarks.ts`：Pinia setup store，`load / listFor / isBookmarked / toggle / remove`。
- `src/components/ssh/SftpBookmarkMenu.vue`：只负责展示与交互，跳转调用 `sftpStore.loadLocal/loadRemote`。
- `SftpPane.vue`：仅插入组件与 import。

## 测试

- `src/stores/sftpBookmarks.test.ts`
  - 脏数据逐项过滤、重复 path 去重、缺 id 补 id、非法 order 回退
  - `toggle` 增删、上限 50、按 host 隔离、本地与远程互不串档
  - `labelFromPath`：`/var/log/nginx` → `nginx`、`/` → `/`、`C:\` → `C:`、尾斜杠
- 组件层验证：远程离线按钮禁用、当前路径为空动作行禁用、跳转失败不关面板（随实现自检）。

## 不在本期范围

- 收藏项重命名、拖拽排序、跨设备同步
- 收藏面板的键盘方向键导航（仅在面板内支持 Esc）
