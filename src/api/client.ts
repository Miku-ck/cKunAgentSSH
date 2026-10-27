/**
 * HTTP 客户端封装 — 基于 backendPort 构建 API URL
 *
 * 三种访问场景：
 * 1. Electron 渲染进程：通过 IPC 拿主进程分配的 backendPort + 127.0.0.1（loopback，最稳）
 * 2. 浏览器本机访问（http://localhost:5555）：用当前页面 host + 7777（与后端 dev 端口一致）
 * 3. 浏览器外部访问（http://[本机IP]:5555）：必须用 window.location.hostname 替代 127.0.0.1，
 *    否则请求会指向用户本机的 7777（不存在）
 *
 * ARCH-008 拆分：本文件退化为 barrel，全部 import 路径（'@/api/client'）零改动。
 * 按 api 对象分域：base（token/URL/request/错误规范化）/ core（会话模型设置）/
 * audit / sse（流式运行）/ agent（agent/调度/管理端）。
 * 注意：base 的 request/getBaseUrl 等协作函数此前是模块私有，随拆分成为可导入
 * 导出（vi.mock('@/api/client') 仍整模块拦截，测试语义不变）。
 */
export * from './base'
export * from './core'
export * from './audit'
export * from './sse'
export * from './agent'
