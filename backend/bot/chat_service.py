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

    def _system_prompt(self, persona: dict) -> str:
        sections = [f"你叫{persona['name']}，是用户的个人机器人伙伴。"]
        if persona.get("personality"):
            sections.append(f"性格与说话方式：{persona['personality']}")
        if persona.get("background"):
            sections.append(f"背景设定：{persona['background']}")
        sections.append("保持以上人格，进行自然、真诚的文字陪聊。不要执行命令或调用工具，不要声称自己是人类。")
        # QQ 是纯文本通道，**、#、反引号会被原样显示成符号
        sections.append("只输出纯文本，不要使用 Markdown 标记（如 **、#、反引号）。")
        return "\n\n".join(sections)

    async def stream_reply(
        self,
        conversation_id: str,
        content: str,
        external_msg_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        conversation = await asyncio.to_thread(db.get_bot_conversation, conversation_id)
        if not conversation:
            raise ValueError("会话不存在")
        model = await asyncio.to_thread(self._model_for_persona, conversation["persona"])
        # 先落库再读历史：QQ 重投同一事件时 INSERT OR IGNORE 不会重复入库，
        # 历史里这条用户消息恰好出现一次；旧顺序是「读历史 → 再 append 原文」，
        # 重试时会把同一条喂给模型两遍。
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "user", content, external_msg_id
        )
        history = await asyncio.to_thread(db.get_recent_bot_messages, conversation_id, 40)
        messages = [{"role": "system", "content": self._system_prompt(conversation["persona"])}]
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)

        from llm_adapter import chat_stream

        # 输出预算按上下文窗口封顶：max_output_tokens 若被选成接近窗口（如 262144），
        # 原样下发会让 vLLM 判定 prompt 挤不进剩余空间 → 400。陪聊与 QQ 共用此路径，
        # 与 routes/agent 用同一个 safe_max_tokens 口径。
        max_tokens = safe_max_tokens(
            model.get("max_tokens"),
            "\n".join(str(item["content"]) for item in messages),
            max_output_tokens=model.get("max_output_tokens"),
        )
        if max_tokens is None:
            max_tokens = model.get("max_output_tokens")

        parts: list[str] = []
        async for chunk in chat_stream(
            base_url=model["base_url"],
            api_key=model.get("api_key") or "EMPTY",
            model=model.get("model_id") or model["id"],
            messages=messages,
            temperature=model.get("temperature", 0.7),
            max_tokens=max_tokens,
            top_p=model.get("top_p", 0.9),
            enable_thinking=False,
            tools=None,
        ):
            if chunk.content:
                parts.append(chunk.content)
                yield chunk.content
        reply = "".join(parts).strip()
        if not reply:
            raise RuntimeError("模型未返回可显示的内容")
        await asyncio.to_thread(db.add_bot_message, conversation_id, "assistant", reply)

    async def complete_reply(
        self,
        conversation_id: str,
        content: str,
        external_msg_id: str | None = None,
    ) -> str:
        return "".join([
            part async for part in self.stream_reply(conversation_id, content, external_msg_id)
        ])
