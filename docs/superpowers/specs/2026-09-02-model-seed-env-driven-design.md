# 模型种子去硬编码 — 设计文档

> 状态：已批准（2026-09-02）· 范围：后端 config/database + 前端模型空态 + README

## 背景与问题

`database.py::_seed_models()` 在 `models_config` 表为空时硬编码种子两个模型
（`Qwen3-VL` @ `192.168.150.40:9030`、`Qwen3-VL-Thinking` @ `:9031`），
地址来自 `config.py` 的默认值——是作者局域网 vLLM 的部署参数。
新机器/新用户首次启动会看到两个可能不存在的 8B 视觉模型，
直接点对话会连接失败，误判产品损坏。

## 目标

1. 默认**不种子**任何模型；模型列表为空时 UI 引导配置。
2. 显式设置 `VLLM_BASE_URL`（环境变量或 `.env`）时，首启**自动探测**
   `GET {base_url}/models` 取第一个模型名种子为默认模型；
   `VLLM_THINKING_URL` 同理种子第二条思考模型。
3. 已有数据库（表非空）零影响、零迁移。

## 行为契约

| 场景 | 改后行为 |
|---|---|
| 全新机器，未设环境变量 | 不种子。设置面板显示既有空态（"暂无模型配置"） |
| 设 `VLLM_BASE_URL`，首启探测成功 | 种子 1 条：探测到的第一个模型名，`is_default=1` + `is_default_ssh=1` |
| 另设 `VLLM_THINKING_URL` 且探测成功 | 再种子 1 条：`supports_thinking=1`，非默认 |
| 设了环境变量但探测失败（不可达/超时/404/空列表/坏响应） | 不种子，warning 日志，**静默降级**不阻塞启动 |
| 已有非空 `models_config` | 从不改动（含旧库已种子的 8B 模型，不清理） |

**自愈特性**：`_seed_models` 每次 `init_db` 都执行、仅表非空时提前返回。
"首启时 vLLM 未启动 → 探测失败 → 未种子" 后，服务启动再重启应用即自动补种子。

## 组件与数据流

```
config.py            新增最小 .env 加载（无依赖；已有环境变量优先；解析失败静默）
  │  vllm_base_url / vllm_thinking_url 默认值改为 ""（原硬编码 LAN IP 删除）
  ▼
database.py          _probe_first_model(base_url) → Optional[str]
  │  httpx.get({base_url}/models, timeout=3s)；取 data[0].id；任何异常 → None
  ▼
  _seed_models(conn)  表空 + base_url 非空 → 探测 → INSERT（独立 _insert_model 助手）
                      thinking_url（须与 base_url 不同）→ 同流程种子思考模型
  ▼
SideChatPanel.vue    models 为空时模型选择器替换为可点击空态提示
                      "尚未配置模型" → useSettings().openSettings('models')
  ▼
README.md            开发节补充 VLLM_BASE_URL / VLLM_THINKING_URL 的 .env 示例
```

## 错误处理

- 探测：3s/端点超时，总预算 ≤6s，仅在首启空表且显式配置时发生
- 探测失败一律降级为"不种子"，不抛异常、不弹 UI 错误
- `.env` 解析失败不影响启动；`.env` 不覆盖已存在的进程环境变量
- 探测成功但 `data` 为空数组：按失败处理
- 模型名原样入库（`model_id` 仅回传 LLM API，无注入面）

## 测试（backend/tests/test_seed_models.py，httpx 全 mock）

1. 未设 `VLLM_BASE_URL` → 0 条种子
2. 设 + mock `/models` 返回多模型 → 种子 1 条 `data[0].id`，双默认标记
3. 设 + thinking URL → 2 条，第二条 `supports_thinking=1` 非默认
4. mock 连接失败 → 0 条，不抛异常
5. 表非空 → 不探测、不改动（回归保护）
6. mock 返回空 `data` → 0 条

## 非目标

- 不清理旧库已种子的模型
- 不做启动时模型健康检测 UI
- 不引入 python-dotenv 依赖（手工最小解析）
