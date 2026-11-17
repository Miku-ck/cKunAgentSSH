# cKunBot 语音全链路方案（ASR + TTS）—— 借鉴 dsh-pet

> 方案日期：2026-11-16（2026-11-17 按用户拍板修订）
> 参照对象：`PC2005-cloud/dsh-pet` v0.4.9（MIT 开源，源码直读，证据见 `dsh-pet-chat-rea-report.md`）
> 服务选型：**ASR/TTS 均可插拔**——DeepSeek 官方接口仅为内置默认之一，用户可接入自有模型（本地 FunASR/SenseVoice 或任意兼容实现）
> 性质：**方案文档，尚未改动代码**。分阶段实施，含拍板结论、每项证据、代码骨架与验证。

---

## 0. 用户拍板结论（2026-11-17）

用户明确三点，本方案据此修订：

1. **ASR 可自己接入模型** —— 不绑死 DeepSeek。provider 抽象是一等公民，能接本地 FunASR/SenseVoice、其他云厂商、或任何自定义实现。
2. **智能语音回复** —— 由模型自己判断何时用语音回（B 标记触发），升级为**主策略**，取代原推荐的点播。
3. **听语音** —— ASR 转写走对话链路（微信当前静默丢弃是明确缺口，必修）。

### 定稿策略

| 维度 | 定稿 | 说明 |
|---|---|---|
| **ASR provider** | 可插拔注册表 + 内置默认 | DeepSeek 官方为开箱默认；本地 FunASR/SenseVoice 为推荐自建选项；接口统一 |
| **TTS 播报触发** | **B 模型智能标记（主）** + C 渠道开关（底座） | 模型在回复里输出 `[语音]` 标记才播报；A 点播降级为兜底开关，默认不用 |
| **播报音色/语速** | provider 提供，配置选择 | 自定义 TTS provider 时可开放语速 |

**为什么 B 适合这个诉求**：cKunBot 已有成熟的"模型输出标记 → 渠道解析 → 投递 → 失败回落"骨架（`stickers.py:74-119`）——表情包 `[表情:开心]` 就是这么工作的。让模型智能决定"这句要不要念出来"只是同一机制的另一个标记，**不需要新造调度逻辑**，而且语义自然（安慰的话念出来、查资料的句子不念）。

**ASR 为什么必须可插拔**：用户可能想用本地模型（隐私/成本/离线），也可能想换云厂商。dsh-pet 的多槽注册表（`speech-registry.ts:20-27`）正是这个设计——注册任意 `transcribe(audio) -> text` 实现，配置里选一个 id。原样借鉴。

---

## 1. 现状与选型（可插拔）

**cKunBot 现在完全没有语音**（`bot/voice.py` 是语气风格不是语音；requirements 无任何 ASR/TTS 库；微信语音被静默丢弃 `wechat_manager.py:103-105`；QQ 空 content 直接 return `qq_channel.py:223`；本地无音频上传）。

**架构决策：ASR/TTS 都做成可插拔 provider，不绑死任何一家。** 内置默认实现开箱即用，用户可随时接入自有模型。这是本方案相对上一版的核心修订。

### ASR 可选实现（搜集成一张表）

| 实现 | 形态 | 中文准确率(CER) | 速度 | 部署成本 | 适用 |
|---|---|---|---|---|---|
| **DeepSeek 官方** | 云 API `POST /v1/tools/asr` | — | ≤120s/8MB，非流式 | 0.39 元/小时（五折） | 开箱默认，零运维 |
| **SenseVoice-Small** | 本地 280MB | **7.81%** | CPU 17x / GPU 170x 实时 | 免费，CPU 可跑 | ⭐ 自建首选：中文最准、极快、低配可跑 |
| **FunASR-Nano / Paraformer** | 本地 2.4GB 显存 | 8.20% / 10.18% | CPU 3.6x / GPU 120-340x | 免费，可 GGUF 单二进制 | 边缘设备/批量长音频、需流式+VAD+说话人分离 |
| OpenAI Whisper 兼容 | 本地或第三方端点 | ~20% | 较慢 | 视部署 | 已有 whisper.cpp 基础设施时复用 |

> 数据来自 FunASR 官方测评（184 条中文长音频，H100）与社区评测，2026 年。**中文场景 SenseVoice/FunASR 全面优于 Whisper**（错字率约一半、速度快一个数量级），且 CPU 可跑——自建 ASR 的首选中SenseVoice-Small。FunASR 1.3.3+ 提供 OpenAI 兼容 API 与 WebSocket 流式服务，接入成本低。

**DeepSeek 官方接口**（2026-11-13 上线，dsh-pet 在用）作为内置默认：
- ASR：`POST /v1/tools/asr`，≤120s/≤8MB、WAV/Ogg Opus、21 语种、非流式、只取左声道，0.39 元/小时（五折）
- TTS：`POST /v1/tools/tts`，整段或 WebSocket 流式、音色 stella/tide、无语速参数、≤1 万字符/≤30 分钟，1.5 元/万字符（五折）
- 鉴权复用 `DEEPSEEK_API_KEY`，与现有模型 key 同源 → 凭据层几乎零新增

> 无论选哪家，provider 接口统一（`transcribe(audio) -> text` / `synthesize(text, voice) -> bytes`），换实现只需改配置 + 新增一个 provider 模块，不碰对话管线。

---

## 2. 总体架构

```
                          ┌─────────────────────────────────────┐
   ASR 入口（入站适配层）    │  provider 契约层（新增 backend/bot/speech/）│
  微信 voice MediaId ──┐   │  ┌─────────────┐  ┌──────────────┐    │
  QQ   富媒体附件 ─────┼──>│  │ ASR 注册表   │  │ TTS 注册表    │    │
  本地 录音上传 ───────┘   │  │ transcribe() │  │ synthesize() │    │
                          │  └─────────────┘  └──────────────┘    │
                          │  失败分类 / 三层超时 / 磁盘缓存 / 串行队列  │
                          └─────────────────────────────────────┘
                                   │ 文字                    │ 音频
                                   v                         v
                        现有对话链路（不动）          TTS 出口（出站 send 闭包）
                    wechat_manager/qq_channel/bot.py      三条渠道各自 send 分支
                                                    （参照 stickers 标记+投递模式）
```

**核心原则**（沿用 cKunBot 现有架构）：
1. **ASR 集中在入站适配层**，不碰 `chat_service.py` 的文本管线——转写后的文字就是普通用户输入。
2. **TTS 挂在出站 send 闭包**，不碰落库文本——音频是投递形态，不是对话内容。
3. **provider 注册表借鉴 dsh-pet**，但 ASR/TTS 各自独立（它们本来就是两条链路，dsh-pet 也是分开的）。

---

## 3. provider 契约层（新增 `backend/bot/speech/`）

参照 dsh-pet 的注册表 + 失败分类 + 超时设计，做成 Python 版：

```python
# backend/bot/speech/types.py（骨架）
from dataclasses import dataclass
from enum import Enum

class FailureReason(str, Enum):
    BAD_REQUEST = "bad-request"        # 音频缺失/超长/格式错
    UNSUPPORTED = "unsupported"        # 没配 key / 没装 provider（能力缺失）
    GENERATE_ERROR = "generate-error"  # 识别/合成失败、超时
    BUSY = "busy"                      # TTS 队列满 / 排队过久

class SpeechUnavailableError(Exception):  # 用异常类型区分"能力缺失"，不靠文案
    pass

@dataclass(frozen=True)
class AsrProvider:
    id: str                    # kebab-case，如 "deepseek"
    name: str
    timeout_s: float = 60.0
    async def transcribe(self, audio: bytes, *, signal=None) -> str: ...

@dataclass(frozen=True)
class TtsProvider:
    id: str
    name: str
    voices: tuple              # ({"id": "stella", "name": "海星", "tag": "女声"}, ...)
    timeout_s: float = 60.0
    async def synthesize(self, text: str, voice: str, *, signal=None) -> bytes: ...
```

**注册表**：多槽（provider id → 实例），重复注册拒绝、注销只删自己那条（照搬 dsh-pet `speech-registry.ts:20-27` 的"迟到注销不顶掉后来者"逻辑）。配置里只存一个限定串 `deepseek/stella`，前缀的拼拆只在注册表一处。

### 3.1 如何自己接入 ASR 模型（核心诉求）

实现一个 `AsrProvider` 子类并注册即可，对话管线零改动。给两个开箱模板：

**模板 A：接本地 SenseVoice / FunASR（推荐自建，中文最准最快）**

FunASR 1.3.3+ 提供 OpenAI 兼容 API 和 WebSocket 流式服务，最简单是起一个本地 funasr-server，然后用 OpenAI 音频转写端点对接：

```python
# backend/bot/speech/providers/local_funasr.py
from openai import AsyncOpenAI          # 复用项目已有的 openai SDK
from ..types import AsrProvider

class LocalFunasrProvider(AsrProvider):
    id = "local-funasr"
    name = "本地 FunASR (SenseVoice)"
    timeout_s = 60.0
    def __init__(self, base_url: str):
        self._client = AsyncOpenAI(base_url=base_url, api_key="EMPTY")

    async def transcribe(self, audio: bytes, *, signal=None) -> str:
        # audio 已是 16k/mono/WAV（入站适配层统一转码好）
        import io
        result = await self._client.audio.transcriptions.create(
            model="SenseVoiceSmall",
            file=("speech.wav", io.BytesIO(audio), "audio/wav"),
        )
        return result.text
```
部署：`pip install funasr` → `funasr-server --model SenseVoiceSmall --port 10095`（CPU 可跑，280MB 模型）。配置 `asr.provider=local-funasr` + `asr.base_url=http://127.0.0.1:10095/v1`。

**模板 B：接任意云服务 / OpenAI 兼容端点**

同上换 `base_url` + `api_key` + `model` 即可。`transcribe(audio) -> str` 是唯一契约，任何 ASR 只要能包成这个形状就能接入。

**注册方式**（启动时装配，dsh-pet `ensureDeepseekAsrProvider` 同款）：

```python
# backend/bot/speech/__init__.py
def setup_speech():
    register_asr(DeepseekAsrProvider(resolve_key=deepseek_key))
    if cfg.asr_provider == "local-funasr":
        register_asr(LocalFunasrProvider(cfg.asr_base_url))
    # 用户新增实现：在这里多 register 一行，或做插件自动发现
```

**关键约束**：所有 provider 收到的 `audio` 必须是 **16kHz / 单声道 / PCM16 WAV**（入站适配层统一转码，见 §4），这样任何实现都无需自己处理微信 AMR/Silk 等格式差异。

**三层超时（借鉴 dsh-pet 最精髓的时序设计）**：

| 层 | 值 | 作用 |
|---|---|---|
| provider 自身 | 60s | DeepSeek 官方接口超时 |
| 服务层 withDeadline | 60s + signal | 用 `asyncio.wait_for` + signal 双保险，兜住"不理 signal 的实现" |
| 调用方兜底 | 62s（比服务层宽一点） | 让服务层先返回结构化失败，调用方只防死等 |

**失败分类要点**：ASR 的"没配 key"抛 `SpeechUnavailableError` → `unsupported`；识别失败/超时 → `generate-error`。TTS 侧 dsh-pet 没区分（没 key 也算 generate-error），**我们要补上**（这是它的一个已知短板）。

---

## 4. ASR 三渠道接入（按优先级）

### 4.1 微信（最关键——当前语音被静默丢弃）

现状：`wechat_manager.py:103-105` 拿到空 text 直接 `return ACK`，语音消息石沉大海。

改法：
1. 扩展 `WeChatInbound`（`wechat_channel.py:42-51`）增加 `voice_media_id: str | None`，从 XML `<voice><media_id>` 解析（现只解 `<content><msg>`，`wechat_channel.py:61-84`）。
2. `wechat_manager.py:103-105`：`extract_text` 空 + 有 voice_media_id → 调素材下载接口拿音频 → ASR → 用转写文本构造 `InboundMessage`。
3. 缺 key/识别失败 → 显式回复一句"我暂时听不清语音，打字说给我听"（不静默丢弃）。

**音频格式注意**：微信语音是 AMR/Silk，DeepSeek ASR 只收 WAV/Ogg Opus → **需要转码**。用 `pydub` + ffmpeg（或 `soundfile`）。这是新增依赖，需在实施时验证 wx 素材下载后的实际格式。

### 4.2 本地 Electron（用户主动录音，最可控）

1. 前端：`src/` 加录音按钮（Web Audio `MediaRecorder`），录完导出 16kHz 单声道 WAV（对齐 dsh-pet 的 `SpeechInput` 契约 `speech-types.ts:8-13`）。
2. 后端：`BotChatRequest`（`bot.py:151-154`）加 `audio: str | None`（base64）；`/chat/upload`（`chat.py:101`）的 `ALLOWED_UPLOAD_EXTS` 加音频扩展名。
3. `bot.py:432` 调 `stream_reply` 前先 ASR 转写，把文字填入 `req.content`。

### 4.3 QQ（富媒体附件）

`qq_channel.py:220-224` `_emit`：content 为空但消息带语音附件时，下载 → ASR → 填 text。QQ 富媒体入站解析需要在 botpy 事件里读附件字段（出站的 `msg_type=7` 模式在 `qq_channel.py:295-299`，入站要另查 SDK 字段）。**这块优先级最低**，可放到最后。

**统一层备选**：给 `InboundMessage`（`provider.py:55-67`）加 `audio` 字段，在 `inbound_pipeline.py:112-121` 调 `complete_reply` 前统一做 ASR——三渠道适配层只填 audio，转写逻辑集中一处。改动更小但会动到核心管线，二选一即可。

---

## 5. TTS 播报（挂各渠道出站 send 闭包）

**核心原则**：不碰 `chat_service.py` 的文本清洗/落库链路（`chat_service.py:400-429`）——音频是投递形态。照搬 stickers 的"标记 → 素材 → 渠道分支 → 失败回落"骨架（`stickers.py:74-119`、`bot_stickers.py:14-24`）。

### 5.1 服务层（新增 `backend/bot/speech/speak.py`）

借鉴 dsh-pet `speak.ts` 的三大机制：
- **串行 Promise 链（Python 用 `asyncio.Lock` + 队列）**：并发上限 `MAX_QUEUE=3`，超出返回 `busy`；一个任务失败不影响后续任务（dsh-pet `speak.ts:168-172` 的"拒绝净化"）。
- **过时丢弃**：排队超过 28s 就丢弃（客户端都超时放弃了，再合成是白烧上游且说的是上一件事，`speak.ts:95-109`）。
- **磁盘缓存**：键 = `sha1(voice + rate + text)`（`cache.ts:50-52`），`<userRoot>/voice-cache/<key>.wav`，原子落盘（`.part` → `rename`，`speak.ts:129-144`），空字节按失败，只在成功后淘汰，双上限（字节+条数）按 mtime 删。

### 5.2 三渠道出口

| 渠道 | 挂载点 | 投递方式 |
|---|---|---|
| 本地 | `routes/bot.py:430-446` SSE 的 `done` 前追加 `sse_event({"type":"audio", "url":...})` | 前端 `<audio>` 播放；音频文件走 `GET /assets/tts/{key}` 静态路由 |
| QQ | `qq_channel.py:247-262` `_reply` 加音频分支 | 复用 `_upload_sticker`（`qq_channel.py:264-293`）拿 file_info → `msg_type=7` 发语音 |
| 微信 | `wechat_manager.py:125-133` `_sender` 加分支 | 参照 `_send_sticker`（`wechat_manager.py:147-168`）公网音频 URL 塞富内容 JSON |

### 5.3 智能语音回复（B 主策略，用户拍板）

让模型自己判断"这句要不要念出来"——复用表情包同款标记机制，不新造调度逻辑。

**① 提示词注入**（仿 `stickers.prompt_block()` `stickers.py:128-141`）：

```python
# backend/bot/speech/prompt.py
VOICE_PROMPT = """
[语音播报规则]
你可以在回复末尾加上标记 [语音]，我会把这句话念给你听。
- 适合念出来：撒娇、关心、讲故事、情绪表达、轻松的碎碎念
- 不适合念：查资料、列清单、代码、长串数字、正经说明
- 一轮最多一个 [语音] 标记，只标你想念的那一句
- 用户明确说"别念/文字就行"时，这一轮不要加
"""
```
由 `chat_service._system_prompt()` 在人格宪章附近注入（与 stickers 同位置），受 `voice.tts_enabled` 开关控制。

**② 输出侧解析**（在 `stream_reply` 落库前，与 `stickers.normalize` 同阶段 `chat_service.py:392`）：

```python
# 解析 [语音] 标记 → 得到要播报的文本 + 从落库文本里剥掉标记
speak_text, reply = voice_mod.extract_and_strip(reply, enabled=tts_enabled)
# reply（干净文本）照常落库；speak_text 作为本轮的播报内容随 SSE done 事件带出
```

**③ 投递**：SSE `done` 事件附带 `{"speak": "<要念的文本>"}`（本地）/ 各渠道 send 分支检测到标记就合成并发送语音（QQ/微信）。合成走 §5.1 队列+缓存。

**④ 失败回落**：TTS 失败只静音，文字回复照常送达——绝不因为语音合成失败影响对话主链路（dsh-pet 的 `failed` 只换静音喇叭图标，不改气泡，`shared/voice.ts:315-317`）。

- **C 底座**：app_settings 加 `voice.tts_enabled`、`voice.asr_enabled` 按渠道配开关（本地/QQ/微信各自），人格级可覆盖——关掉时提示词不注入、标记不解析。
- **A 兜底**：保留"每条回复可手动点喇叭"作为无障碍兜底，但默认不显示（模型智能触发为主）。

**为什么这样最省**：cKunBot 已有 `[表情:开心]` 的完整标记链路（注入→解析→落库→多渠道投递→失败回落，见 `stickers.py` + `chat_service.py:114-118,392`），`[语音]` 是把同一套机制换个标记名复用，改动集中在 `speech/` 模块 + 三处挂载点。

---

## 6. 配置与凭据

沿用现有 app_settings 加密信封 + 环境变量覆盖模式（Tavily 样板 `routes/settings.py:505-534`、`web_tools.py:267-277`）：

```python
# 新增 app_settings 键（value 用 db.protect_secret 加密）
asr.provider = "deepseek"          # provider id：deepseek / local-funasr / 自定义
asr.base_url = "https://api.deepseek.com"   # 本地模型改成 http://127.0.0.1:10095/v1
asr.model    = "SenseVoiceSmall"   # 本地 FunASR 用；DeepSeek 可留空
asr.api_key  = <encrypted>         # 环境变量 DEEPSEEK_API_KEY 优先；本地模型填 EMPTY
tts.provider = "deepseek"
tts.voice    = "deepseek/stella"   # 限定 id
tts.base_url = "https://api.deepseek.com"
tts.api_key  = <encrypted>
voice.tts_enabled = true           # 智能语音回复总开关
voice.asr_enabled = true
```

**关键**：换 ASR 实现只改 `asr.provider` + `asr.base_url` + `asr.model` 三个键，不碰任何代码（前提是该 provider 已注册）。DeepSeek key 与现有对话模型 key 同源，若 persona 已用 DeepSeek 模型，可复用 `get_model_config` 明文 key（`db/models_config.py:130-163`）——实现时做"优先复用模型 key，无则读独立配置"的回落。

设置页下拉：拉 `/speech/asr-providers`、`/speech/voices`，候选来源与注册表同源（dsh-pet `settings.ts:126-156` 的做法），失败留空、绝不硬编码音色 id。缺 provider 时**显式失败**，不静默回落。自定义实现若不想写前端配置项，也可只在代码里 `register_asr()` 后用配置 id 选中。

---

## 7. 风险控制

| 风险 | 缓解 |
|---|---|
| 微信语音是 AMR/Silk，各 ASR 实现多收 WAV/Ogg | 入站适配层统一转 16k/mono/WAV（pydub+ffmpeg）；实施第一步验证真实素材格式 |
| 智能标记被滥用导致每句都念 | 提示词明确"适合/不适合念"清单 + 一轮最多一个标记 + 用户可说"别念"；渠道开关兜底 |
| 模型忘加标记 → 该念的没念 | 标记是加分项不是必需，文字回复始终完整；靠提示词示例培养，不追求 100% |
| ASR/TTS 服务故障拖垮对话 | ASR 失败降级为"请打字"提示、不阻断对话；TTS 失败只静音不改文字回复；三层超时 |
| 本地模型推理慢/吃资源 | SenseVoice CPU 17x 实时够用；provider 超时 + 队列背压；重模型可选 GPU/GGUF |
| TTS 成本失控（长文本反复合成） | 磁盘缓存 + 文本长度上限 + 队列背压；缓存键含 voice/text 防串 |
| key 泄露 | 复用 protect_secret（DPAPI）+ env 覆盖；前端只拿音频 URL，不接触 key |
| 缓存脏文件 | `.part→rename` 原子写 + `size>0` 判据 + 空字节按失败 |

---

## 8. 分阶段实施计划

| 阶段 | 内容 | 依赖 | 预估 |
|---|---|---|---|
| **P1 契约层 + ASR 本地闭环** | `speech/` 注册表/失败分类/超时；DeepSeek ASR + 本地 FunASR 两个 provider；本地录音上传→转写→进对话 | 已拍板 | 核心，先做 |
| **P2 微信 ASR** | 扩展 WeChatInbound 解析 voice；素材下载+转码；失败显式提示 | P1 + 转码验证 | 收益最大（修复静默丢弃） |
| **P3 TTS 服务层 + 智能播报** | 串行队列/缓存/过时丢弃；`[语音]` 标记注入+解析；SSE audio 事件 + 前端播放 | P1 | 见效快，主策略落地 |
| **P4 QQ 双工** | QQ 语音入站 ASR + 出站 msg_type=7 播报（含 `[语音]` 标记） | P1/P3 | 收尾 |
| **P5 设置页** | provider/音色下拉配置；自定义 ASR 接入文档 | P3 | 可选增强 |

**验证**：每阶段配单测——ASR 转写/失败分类/超时降级/可插拔切换；TTS 队列净化/缓存命中/过时丢弃/原子落盘；`[语音]` 标记注入→解析→剥离→播报全链路；三渠道各发一条真实语音走通。全量 backend 测试需保持绿（当前基线 4213 passed）。

---

## 9. 一句话总结

cKunBot 语音是从零新建子系统。技术路径清晰：**ASR/TTS 都做成可插拔 provider**——DeepSeek 官方接口作开箱默认，本地 FunASR/SenseVoice（中文最准最快、CPU 可跑）作自建首选，换实现只改配置零改代码；ASR 补三渠道入站（微信静默丢弃是必修缺口）、TTS 挂三渠道出站；中间套一层借鉴 dsh-pet 的契约（注册表/失败分类/三层超时/队列缓存/过时丢弃）。**播报按用户拍板走"模型智能标记 `[语音]` 触发"主策略**——复用现有表情包标记骨架，模型自己决定哪句该念，文字回复始终完整，语音失败只静音。

---

## 10. 修订记录

- **2026-11-16**：初版，推荐点播起步（A+C），DeepSeek 为唯一选型。
- **2026-11-17**：按用户拍板修订——① ASR/TTS 改为可插拔 provider，DeepSeek 降级为内置默认，新增本地 FunASR/SenseVoice 自建方案与接入模板；② 播报触发从点播改为"模型智能 `[语音]` 标记"主策略（B+C），点播降为兜底；③ 补充智能标记的提示词/解析/投递/失败回落完整设计。

