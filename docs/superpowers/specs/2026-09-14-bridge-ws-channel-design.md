# Electron Bridge WS 反向通道设计（阶段 1）

日期：2026-09-14　状态：已评审通过（用户确认 A1 客户端结构 + 短等待降级）

> ✅ **后续进展（2026-09-23 第六轮清理标注）**：阶段 1 与阶段 2 均已落地 ——
> WS 通道在 `electron/bridge-ws-client.ts` + `backend/agent/bridge_relay.py`，
> HTTP 端口桥（30000-30100）已整体移除，下文「保留为兜底」的描述已成历史；
> 文中引用的《ElectronBridge不可达问题诊断与修复-v1.md》已随孤儿文档清理删除。

## 背景与目标

定时任务的无头远程执行依赖 `Python 后端 → Electron 主进程` 的桥接。现状是主进程监听
回环 HTTP 端口（30000-30100），后端入站调用。Windows 防火墙对回环入站按可执行文件放行，
`electron.exe` 无规则时端口"假监听"（已发生并修复，见
`docs/ElectronBridge不可达问题诊断与修复-v1.md`）。该依赖脆弱：打包换路径、新机器、
沙箱规则出现都会再次击穿。

**目标**：主进程改为**出站拨号**连后端 8010 的 WebSocket（python.exe 入站规则已存在且
实测可达，出站长连接不受入站规则影响），命令经此通道下发。阶段 1 为纯增量：
现有 HTTP 端口桥保留为兜底通道，稳定后再做阶段 2 移除。

## 验收标准

1. 删除 electron.exe 防火墙规则后，定时任务端到端执行成功（走 WS 通道）
2. 规则保留时行为不变（WS 优先，HTTP 兜底可用）
3. 杀 Python 后端触发 BackendManager 自动重启 → WS 数秒内自动重连
4. 执行中断线 → 命令不双发（fail-closed，在途请求立即失败）
5. `tsc -p tsconfig.node.json --noEmit` 与 `pytest backend/tests` 全绿

## 架构

```
  Python 后端 (127.0.0.1:8010, uvicorn)
    ├─ WS /ws/bridge  ◀─── 主进程出站拨号，长期保持（新主链路）
    └─ 其余 HTTP（现状不变）

  Electron 主进程
    ├─ WS 客户端 ───────▶ 拨号 /ws/bridge（新）
    └─ HTTP 桥 30000-30100（保留，降为兜底）
```

- 主进程是拨号方；单实例锁保证唯一主进程
- 后端同一时刻只保留一条 bridge WS；新连接完成 hello 后替换旧连接（新者胜，close(4000)）
- 客户端结构 **A1**：`ElectronBridge`（Python）公开 API 不变，内部 WS 优先 / HTTP 兜底；
  4 个调用方（schedule_engine / ops_common / shell_exec / saddle）零改动

## WS 协议（文本帧 JSON）

| 帧 | 方向 | 说明 |
|---|---|---|
| `hello` | 主→后 | 连接后 3s 内必须；`{type, token}`；后端 `hmac.compare_digest` 比对自身 env `ELECTRON_BRIDGE_TOKEN`；不符 close(4401) |
| `hello-ack` | 后→主 | `{type, ok:true}` |
| `req` | 后→主 | `{type, id, op, payload}`；op ∈ ping / ssh_exec / ssh_connect / ssh_disconnect |
| `res` | 主→后 | `{type, id, ok, result}`；result 与现有 HTTP 响应体结构一致 |
| `ping` / `pong` | 双向 | 应用层心跳 30s；主进程连续 2 轮无 pong 判死重连 |

- uvicorn `ws_max_size` 显式 16MB
- 请求超时：后端 `send_request` 按 `id→Future` 等待，超时语义沿用 `timeout + 5s 余量`
- 断线时在途请求立即失败（新错误码 `BRIDGE_WS_LOST`），**绝不重发**（远程命令非幂等）

## 降级时序（任务触发）

```
probe()：WS 已连接→OK；未连接→等 ≤5s（200ms 轮询）→仍不通→HTTP 探活→结果
exec/connect/disconnect：调用瞬间 WS 已连接→WS；否则→现有 HTTP 路径
```

## 文件改动

| 文件 | 动作 |
|---|---|
| `electron/bridge-ws-client.ts` | 新建：出站 WS 客户端（hello/重连退避 1→15s/应用心跳/dispatch 回调） |
| `electron/electron-bridge.ts` | 重构：4 个 handler 拆为 `dispatch(op, data)`，HTTP 变薄包装；`getBridgeToken()` |
| `electron/main.ts` | ~10 行：后端 ready 时启动 WS 客户端，退出时关闭 |
| `backend/agent/bridge_relay.py` | 新建：WS 服务端中枢（单连接槽/hello 鉴权/新者胜/send_request/Future 表） |
| `backend/main.py` | ~5 行：注册 `ws /ws/bridge`（非 /api 前缀）+ lifespan 接线 + ws_max_size |
| `backend/agent/electron_bridge.py` | 核心：WS 优先传输层；probe 加 5s 短等待；新增 `BRIDGE_WS_LOST` |
| `backend/agent/schedule_engine.py` | 预期零改动（probe 语义覆盖短等待） |

## 测试

- `backend/tests/test_bridge_relay.py`：hello 通过/拒绝(4401)、req/res 往返、超时、
  新连接替换、ping/pong、未 hello 发 req 被拒（Starlette TestClient.websocket_connect）
- `backend/tests/test_electron_bridge_client.py`：WS 优先、回退（含 5s 等待超时）、
  在途断线抛 WS_LOST 且不重发
- 现有 4 个 monkeypatch `ElectronBridge` 的测试文件预期零改动（回归验证）
- 主进程侧：tsc + 实机冒烟（无单测基建）

## 依赖

零新增：Electron 35（Node 22 原生 WebSocket）；后端 uvicorn[standard] 已含 websockets。

## 阶段 2（本次不做）

WS 稳定后移除 HTTP 端口桥（listenFirstFree/probeSelf/防火墙提示/fix 脚本降级路径），
`ELECTRON_BRIDGE_PORT/TOKEN` env 仅保留 token。
