import asyncio
from typing import AsyncGenerator

import database as db
from safe_tokens import safe_max_tokens


class BotChatService:
    def _model_for_persona(self, persona: dict) -> dict:
        model_ref = persona.get("model_id")
        model = db.get_model_config(model_ref, mask_key=False) if model_ref else None
        model = model or db.get_default_model(mode=None, mask_key=False)
        model = model or db.get_default_model(mode="ssh", mask_key=False)
        if not model:
            models = db.list_models_config()
            model = db.get_model_config(models[0]["id"], mask_key=False) if models else None
        if not model:
            raise ValueError("尚未配置可用模型，请先在设置中添加模型")
        return model

    # 回复长度与语气风格指令：按 persona.reply_style 选一条，moderate 为默认。
    # 与「可控长文/人性化简洁回复」直接对应——长/中/短三档可控。
    _STYLE_DIRECTIVE = {
        "concise": "回复风格：一两句话、口语化、点到为止，别铺陈。",
        "moderate": "回复风格：以简洁自然为主，确有内容再展开。",
        "long": "回复风格：可分点、有条理地展开细节。",
    }

    def _system_prompt(self, persona: dict, channel: str = "local") -> str:
        sections = [f"你叫{persona['name']}，是用户的个人机器人伙伴。"]
        if persona.get("personality"):
            sections.append(f"性格与说话方式：{persona['personality']}")
        if persona.get("background"):
            sections.append(f"背景设定：{persona['background']}")
        sections.append(
            self._STYLE_DIRECTIVE.get(
                persona.get("reply_style") or "moderate",
                self._STYLE_DIRECTIVE["moderate"],
            )
        )
        # 排版规则按渠道分：本地会话走 Markdown 渲染，QQ 被动回复是纯文本，
        # **、#、反引号会被原样显示成符号，故禁止 Markdown 标记与表格/链接/代码块。
        if channel == "qq":
            sections.append(
                "排版规则：只用纯文本与少量表情；禁止表格、链接、代码块和 # 等 Markdown 标记（会被原样显示成符号）。"
            )
        else:
            sections.append("排版规则：可用 Markdown 排版与适量表情。")
        sections.append("保持以上人格，进行自然、真诚的文字陪聊。不要执行命令或调用工具，不要声称自己是人类。")
        return "\n\n".join(sections)

    async def stream_reply(
        self,
        conversation_id: str,
        content: str,
        external_msg_id: str | None = None,
        *,
        channel: str = "local",
    ) -> AsyncGenerator[tuple[str, str], None]:
        conversation = await asyncio.to_thread(db.get_bot_conversation, conversation_id)
        if not conversation:
            raise ValueError("会话不存在")
        persona = conversation["persona"]
        model = await asyncio.to_thread(self._model_for_persona, persona)
        # 先落库再读历史：QQ 重投同一事件时 INSERT OR IGNORE 不会重复入库，
        # 历史里这条用户消息恰好出现一次；旧顺序是「读历史 → 再 append 原文」，
        # 重试时会把同一条喂给模型两遍。
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "user", content, external_msg_id
        )
        history = await asyncio.to_thread(db.get_recent_bot_messages, conversation_id, 40)
        messages = [{"role": "system", "content": self._system_prompt(persona, channel)}]
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)

        from llm_adapter import chat_stream

        # thinking 只看 persona.enable_thinking（不 gate 模型 supports_*，否则手动
        # 加的模型永远开不了思考）；只有模型真的回 reasoning chunk 时才自然展示。
        enable_thinking = bool(persona.get("enable_thinking"))
        reasoning_budget = model.get("reasoning_tokens") if enable_thinking else None
        # 简洁风格把输出上限压到 512；其余沿用模型 max_output_tokens（长度主要由提示词控制）。
        style_cap = 512 if persona.get("reply_style") == "concise" else model.get("max_output_tokens")
        # 输出预算按上下文窗口封顶：max_output_tokens 若被选成接近窗口（如 262144），
        # 原样下发会让 vLLM 判定 prompt 挤不进剩余空间 → 400。陪聊与 QQ 共用此路径，
        # 与 routes/agent 用同一个 safe_max_tokens 口径。
        max_tokens = safe_max_tokens(
            model.get("max_tokens"),
            "\n".join(str(item["content"]) for item in messages),
            max_output_tokens=style_cap,
            reasoning_tokens=reasoning_budget,
        )
        if max_tokens is None:
            max_tokens = style_cap

        parts: list[str] = []
        reasoning_parts: list[str] = []
        async for chunk in chat_stream(
            base_url=model["base_url"],
            api_key=model.get("api_key") or "EMPTY",
            model=model.get("model_id") or model["id"],
            messages=messages,
            temperature=model.get("temperature", 0.7),
            max_tokens=max_tokens,
            top_p=model.get("top_p", 0.9),
            enable_thinking=enable_thinking,
            tools=None,
            reasoning_max_tokens=reasoning_budget,
        ):
            # reasoning 只在本地面下发：QQ 被动回复是单条纯文本，无思考展示位。
            if chunk.reasoning and channel == "local":
                reasoning_parts.append(chunk.reasoning)
                yield "reasoning", chunk.reasoning
            if chunk.content:
                parts.append(chunk.content)
                yield "content", chunk.content
        reply = "".join(parts).strip()
        if not reply:
            raise RuntimeError("模型未返回可显示的内容")
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "assistant", reply,
            None, "".join(reasoning_parts) or None,
        )

    async def complete_reply(
        self,
        conversation_id: str,
        content: str,
        external_msg_id: str | None = None,
        *,
        channel: str = "qq",
    ) -> str:
        out: list[str] = []
        async for kind, text in self.stream_reply(
            conversation_id, content, external_msg_id, channel=channel
        ):
            if kind == "content":
                out.append(text)
        return "".join(out).strip()
