# Docker 镜像「清理未使用」设计

日期：2026-10-20
状态：已获用户批准（入口形态、引用列方案、整体设计）

## 背景与目标

Docker 页镜像 Tab 目前只有「清理悬空」入口（`docker image prune -f`），
只回收无标签的中间层。带标签但没有容器引用的镜像（手动 pull 的备用镜像、
旧版本发布镜像、按摘要拉取的镜像）无法在页面上清理，用户必须手敲命令。

本次目标：新增「清理未使用」能力（`docker image prune -a -f`），并在列表中
标出每个镜像是否被容器引用，让用户在删除前看清作用面。

## 已确认的约束

- 入口形态：保留「清理悬空」，新增独立的「清理未使用」按钮（两个按钮并列）
- 使用状态：镜像表新增「引用」列，复用已加载的容器列表交叉计算，不新增远端命令
- 「未使用」定义对齐 Docker：未被任何容器（含已停止容器）引用
- 不做：清理前预览勾选、按镜像年龄过滤、`docker system prune` 聚合清理

## 改动范围

| 文件 | 改动 |
|---|---|
| `src/stores/docker-core.ts` | 新增 `imagePruneAllCmd`；`ContainerRow.imageRef`；引用匹配纯函数 |
| `src/stores/docker-core.test.ts` | 命令形状、`parsePs` 新字段、匹配矩阵 |
| `src/components/ssh/DockerView.vue` | 引用列、未使用计数、新按钮、确认框、清理编排 |
| `src/components/ssh/DockerView.guards.test.ts` | 页面级守卫：按钮、列、计数、重拉 |

## 命令层设计

```ts
// 携带 -a：删除所有未被任何容器引用的镜像，含带标签镜像
export const imagePruneAllCmd = (): string => `docker image prune -a -f`
```

`ContainerRow` 增加 `imageRef: string`，由 `parsePs` 直接取 `docker ps` 的
IMAGE 原始列（`f[1]`）。`PS_CMD` 不变，容器列表加载路径零额外开销。

引用匹配（`imageRefMatches(im, ref)`）按以下顺序判定，全部小写比较：

| 容器 `imageRef` 形态 | 命中条件 |
|---|---|
| 短 ID `8f3c2a1d9e4b` | 等于镜像短 ID |
| 完整 ID `sha256:8f3c…` / 裸 64 位 hex | 与镜像 ID 前缀匹配 |
| `repo:tag` | 等于 `imageLabel(im)` |
| 裸 `repo`（Docker 对 latest 的显示） | `im.tag === 'latest'` 且等于 `im.repo` |
| `repo@sha256:…`（按摘要创建容器） | 前缀为 `im.repo + '@'` |

保守原则：任一条件无法确定时按「在用」处理。多算引用只会少删，
不会把在用的镜像误判成可删。`repo` 或 `tag` 为 `<none>` 时只走 ID 匹配。

`imageUsageCount(im, containers)` 返回引用该镜像的容器数（按容器 ID 去重），
供「引用」列与确认框计数使用。

## 页面设计

### 引用列

镜像表列宽调整为：镜像 38% / 镜像 ID 18% / 内容大小 14% / 引用 16% / 操作 14%。

- 在用：`N 个容器`（ok 色），与卷的「引用」列同款 `.use-hint` 样式
- 未使用：`未使用`（warn 色）
- 容器列表输出被截断：`—`（muted），不凭空断言

### 状态栏

`镜像 N（M 未使用 · K 悬空）`，`K = 0` 时省略「· K 悬空」；
容器列表截断时只显示总数，不显示 M。

### 清理按钮

- 「清理未使用」使用 `.btn-danger-ghost`，busy key `prune:images-all`
- 禁用条件与「清理悬空」一致：`!!busy || !images.length`
- 确认框标题「清理未使用镜像」，正文：

  > 将删除所有未被任何容器（包括已停止容器）引用的镜像；
  > 带标签的镜像也在范围内，删除后需重新 pull，不可恢复。

  容器列表可信时追加「当前 M 个未使用镜像（其中 K 个悬空）」；
  截断时改为「列表可能不完整，实际删除数由 Docker 判定」。

- 成功后复用 `countPrunedLines` / `parseReclaimed`：
  `已清理 N 个未使用镜像，回收 X`，随后 `reloadImages()`
- 审计沿用 `actionExec`（`AUDIT_ACTION_DOCKER`）

## 错误处理与安全

- 引用关系由 Docker 守护进程判定，被容器（含已停止）引用的镜像不会删除；
  前端计数只用于展示与确认文案，不作为删除清单，列表滞后也不会造成误删
- `image prune -a` 自 Docker 1.13 起可用，无需额外版本分支
- 清理失败按既有 `dockerErrorFromResult` 归类，toast 上浮，不清空列表

## 测试

1. `docker-core.test.ts`
   - `imagePruneAllCmd()` 生成 `docker image prune -a -f`
   - `parsePs` 带出 `imageRef`，且不影响既有 repo/tag 拆分
   - 匹配矩阵：短 ID / 完整 ID / `repo:tag` / latest 裸名 / digest / 未命中
   - `imageUsageCount` 对同一镜像的多容器引用按容器去重
2. `DockerView.guards.test.ts`
   - 新按钮与 `prune:images-all` busy key
   - 引用列渲染、截断降级为 `—`
   - 确认框包含未使用计数口径
   - 清理后调用 `reloadImages()`
3. 命令验证
   - `npx vitest run src/stores/docker-core.test.ts src/components/ssh/DockerView.guards.test.ts`
   - `npx vue-tsc -p tsconfig.web.json --noEmit`

## 风险与回退

- 改动集中在前端 2 个源文件与 2 个测试文件，`git revert` 单个提交即可整体回退
- `prune -a` 属破坏性操作，确认框文案必须保留「带标签镜像也在范围内、不可恢复」
- 匹配规则偏保守：极端情况下（同一 repo 多个摘要、容器引用被 Docker 改写）
  可能多算「在用」，表现为少删；不会造成误删
