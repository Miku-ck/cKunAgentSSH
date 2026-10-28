import asyncio
import logging
from typing import AsyncGenerator

import database as db
from agent.memory.budget import cap_text_tokens
from bot import memory as memory_mod
from bot import relation as relation_mod
from bot.character import (
    REFUSAL_STYLES,
    SAFETY_CHARTER,
    detect_situation,
    reaction_for,
    stage_guide,
)
from bot.voice import compile_dna, strip_ai_smell
from safe_tokens import safe_max_tokens

logger = logging.getLogger(__name__)


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
    _STYLE_DIRECTIVE = {
        "concise": "回复长度：一两句话、口语化、点到为止，别铺陈。",
        "moderate": "回复长度：以简洁自然为主，确有内容再展开。",
        "long": "回复长度：可有条理地展开细节。",
    }

    def _system_prompt(
        self,
        persona: dict,
        channel: str = "local",
        *,
        relation: dict | None = None,
        situation: str | None = None,
    ) -> str:
        """把人格内核编译成分节提示词。

        分节顺序对应模型对 prompt 的注意力分布：身份和说话方式在最前，
        关系与此刻的分寸居中，硬约束压尾 —— 首尾是模型记得最牢的两处，
        放最不能被忽略的内容。
        """
        name = persona.get("name") or "cKunBot"
        address = (persona.get("address_user") or "").strip()

        sections: list[str] = []

        who = f"你是{name}，正在和一个你在意的人用文字聊天。"
        if address:
            who += f"你叫对方「{address}」。"
        sections.append(who)

        if persona.get("personality"):
            sections.append(f"你的性格与说话习惯：{persona['personality']}")
        if persona.get("background"):
            sections.append(f"你的背景：{persona['background']}")
        if persona.get("likes_landmines"):
            sections.append(f"你的喜好与雷区：{persona['likes_landmines']}")

        # 表达 DNA：枚举档位编译成可执行约束。personality 负责「是谁」，
        # 这里负责「怎么开口」—— 只有前者时模型会退化回通用助手腔。
        sections.extend(compile_dna(persona.get("expression_dna")))

        # 样板排在档位之后：先讲规矩再给例子，否则模型会把示例内容当事实复述。
        if persona.get("dialogue_samples"):
            sections.append(f"说话样板（照着这个口气说，别照抄内容）：\n{persona['dialogue_samples']}")

        sections.append(
            self._STYLE_DIRECTIVE.get(
                persona.get("reply_style") or "moderate",
                self._STYLE_DIRECTIVE["moderate"],
            )
        )

        stage = (relation or {}).get("stage")
        if stage:
            sections.append(f"你们的关系（{stage}）：{stage_guide(stage)}")

        reaction = reaction_for(situation, persona.get("reaction_style"))
        if reaction:
            sections.append(f"此刻的分寸：{reaction}")

        sections.append(
            "拒绝的方式：" + REFUSAL_STYLES.get(
                persona.get("refusal_style") or "soft", REFUSAL_STYLES["soft"]
            )
        )

        if persona.get("advanced_play"):
            sections.append(f"进阶玩法：{persona['advanced_play']}")
        # 记忆的分寸管的是「以后该记什么、该避讳什么」，与当前有没有召回到事实无关，
        # 所以留在人格 prompt 里，不跟那条动态记忆 system 消息绑在一起。
        if persona.get("memory_directive"):
            sections.append(f"记忆的分寸：{persona['memory_directive']}")

        # 排版规则按渠道分：本地会话走 Markdown 渲染，QQ 被动回复是纯文本，
        # **、#、反引号会被原样显示成符号，故禁止 Markdown 标记与表格/链接/代码块。
        if channel == "qq":
            sections.append(
                "排版规则：只用纯文本与少量表情；禁止表格、链接、代码块和 # 等 Markdown 标记（会被原样显示成符号）。"
            )
        else:
            sections.append("排版规则：可用 Markdown 排版与适量表情。")

        # 分条口径两个渠道共用，且排在排版之后压尾（末位指令记得最牢）：治的是
        # 「明明只说一两句，却被写成三行」—— 一条气泡内硬换行既不像真人也白占行高，
        # 要分开说就该分开投（本地按空行展开成多气泡，QQ 按空行连发多条消息）。
        sections.append(
            "分条规则：空一行就是另发一条消息，最多 3 条；一条之内不要换行、不要分点。"
            "一句话说完就发出去，别为了显得口语把一句话拆成三行；"
            "确实要分开说几件事，才用空行分开。"
        )

        # 宪章永远压尾，且自身声明优先级最高：这是唯一不允许被人格设定覆盖的一节。
        sections.append(SAFETY_CHARTER)
        return "\n\n".join(sections)

    @staticmethod
    def _to_llm_message(item: dict) -> dict:
        """历史条目转 OpenAI message：仅带图的用户消息构造成多模态数组；其余
        （含助手、无图用户消息）维持纯文本——助手图不回喂（控 token）。"""
        images = item.get("images") or []
        if item["role"] == "user" and images:
            return {
                "role": "user",
                "content": [{"type": "text", "text": item["content"]}]
                + [{"type": "image_url", "image_url": {"url": u}} for u in images],
            }
        return {"role": item["role"], "content": item["content"]}

    async def stream_reply(
        self,
        conversation_id: str,
        content: str,
        external_msg_id: str | None = None,
        *,
        channel: str = "local",
        images: list | None = None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        conversation = await asyncio.to_thread(db.get_bot_conversation, conversation_id)
        if not conversation:
            raise ValueError("会话不存在")
        persona = conversation["persona"]
        model = await asyncio.to_thread(self._model_for_persona, persona)

        # 关系快照必须在记账之前读：这一轮应按「开口之前的熟悉度」说话，
        # 而不是先升一档再回头用新阶段的口吻。久别重逢的判定同理。
        persona_id = conversation.get("persona_id") or ""
        speed = persona.get("relationship_speed") or "normal"
        relation = (
            await asyncio.to_thread(relation_mod.snapshot, persona_id, speed)
            if persona_id else None
        )
        gap_days = (
            await asyncio.to_thread(relation_mod.gap_days, persona_id)
            if persona_id else None
        )
        situation = detect_situation(content, gap_days)

        # 先落库再读历史：QQ 重投同一事件时 INSERT OR IGNORE 不会重复入库，
        # 历史里这条用户消息恰好出现一次；旧顺序是「读历史 → 再 append 原文」，
        # 重试时会把同一条喂给模型两遍。
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "user", content, external_msg_id,
            None, images if channel == "local" else None,
        )
        # 用户说了话就算一次互动，与模型成功与否无关 —— 关系是聊出来的，不是答对才给分。
        if persona_id:
            await asyncio.to_thread(relation_mod.record_turn, persona_id, speed)

        history = await asyncio.to_thread(db.get_recent_bot_messages, conversation_id, 40)
        messages = [{
            "role": "system",
            "content": self._system_prompt(
                persona, channel, relation=relation, situation=situation
            ),
        }]
        # 长期记忆：独立的一条 system 消息，插在人格 prompt 之后、历史之前。
        # 它是对模型的补充而不是一次对话，所以不写进 bot_messages（可见记录保持干净）。
        memory_note = self._memory_note(persona_id, content)
        if memory_note:
            messages.append({"role": "system", "content": memory_note})
        messages.extend(self._to_llm_message(item) for item in history)

        from llm_adapter import chat_stream

        # thinking 只看 persona.enable_thinking（不 gate 模型 supports_*，否则手动
        # 加的模型永远开不了思考）；只有模型真的回 reasoning chunk 时才自然展示。
        enable_thinking = bool(persona.get("enable_thinking"))
        reasoning_budget = model.get("reasoning_tokens") if enable_thinking else None
        # 输出预算按风格分档（spec §4.2）：concise 压到 512（人性化简短）；long 放到模型
        # max_output_tokens 的 4 倍、但不超上下文（可控长文）；moderate 走模型默认。
        # 长度还由提示词软指令配合；最终仍经 safe_max_tokens 按窗口封顶，防 vLLM 400。
        style = persona.get("reply_style") or "moderate"
        model_out = model.get("max_output_tokens")
        if style == "concise":
            style_cap = 512
        elif style == "long":
            style_cap = min(model.get("max_tokens") or 4096, (model_out or 0) * 4 or 4096)
        else:
            style_cap = model_out
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
            # 人格没勾思考就真的关掉（Qwen3 系模板默认开思考，不下发 False 等于关不掉）：
            # 只是「不显示」不够，白烧思考 token 会拖慢首字。端点不认该字段时
            # llm_adapter 会去掉参数降级重试。
            disable_thinking=not enable_thinking,
        ):
            # reasoning 只在「人格开了思考 + 本地面」下发：QQ 被动回复是单条纯文本，
            # 无思考展示位；人格没勾思考时，上游即便回了 reasoning chunk 也一律丢掉
            # （既不显示也不落库），否则人格上的思考开关形同虚设。
            if enable_thinking and channel == "local" and chunk.reasoning:
                reasoning_parts.append(chunk.reasoning)
                yield "reasoning", chunk.reasoning
            if chunk.content:
                parts.append(chunk.content)
                yield "content", chunk.content
        reply = "".join(parts).strip()
        if not reply:
            raise RuntimeError("模型未返回可显示的内容")

        # 去 AI 味只在落库前做一次：流式阶段按块清洗会切断跨块的模板句正则，
        # 反而把半句套话留在中间。前端在 done 之后用落库消息替换临时气泡，
        # 所以用户最终看到的就是清洗后的版本。
        reply = strip_ai_smell(reply)
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "assistant", reply,
            None, "".join(reasoning_parts) or None,
        )
        # 这一轮说完之后才记事实：模型失败的话，用户那句还没被承认过
        await self._remember_turn(persona_id, conversation_id, content)

    @staticmethod
    def _memory_note(persona_id: str, query: str) -> str:
        """这个人格记住的事 → 一条独立 system 消息。

        召回失败一律当作没有记忆：记忆是加分项，不能因为它拖垮陪聊。
        """
        if not persona_id:
            return ""
        try:
            contents = memory_mod.recall_sync(persona_id, query)
        except Exception as exc:  # noqa: BLE001 —— 记忆层任何异常都不该影响回复
            logger.warning("陪聊记忆召回失败: %s", exc)
            return ""
        if not contents:
            return ""
        body = cap_text_tokens(
            "\n".join(f"- {text}" for text in contents), 200
        )
        return "关于对方，你记得这些事（当事实用，别主动罗列，也别编造没记住的）：\n" + body

    @staticmethod
    async def _remember_turn(
        persona_id: str, conversation_id: str, user_text: str
    ) -> None:
        """把用户这句话里可记的事实落库。失败只记日志。"""
        if not persona_id:
            return
        try:
            await asyncio.to_thread(
                memory_mod.remember, persona_id, conversation_id, user_text
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("陪聊记忆写入失败: %s", exc)

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
