"""人格编辑器每个分区是否真的进了模型输入：真库 + 真人格 + 真 BotChatService。

编辑器里填得再全，生成侧没读就是「存了但没用」。这里钉的是"填进去就生效"：
自由文本按标记串逐条查，档位按「只改这一档，提示词必须跟着变」查，
模型/长度/思考这类行为差异查下发参数。
"""

import os
from types import SimpleNamespace

import pytest

import database as db
from bot.character import REFUSAL_STYLES, SAFETY_CHARTER, REACTION_LIBRARY
from bot.chat_service import BotChatService
from bot.voice import EMOJI, PARTICLES, PUNCTUATION, SENTENCE


@pytest.fixture
def effect_db(tmp_workspace):
    """先配一个模型：陪聊走 _model_for_persona，没模型可解析时直接抛，测不到人格。"""
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "persona-effect.db"))
    model = db.create_model_config(
        name="陪聊模型",
        base_url="http://model.test/v1",
        api_key="secret",
        model_id="chat-v1",
        max_output_tokens=1024,
    )
    yield model
    db._close_all_connections()


@pytest.fixture
def capture(monkeypatch):
    """截下发给模型的调用参数；messages 里的 system 条目就是人格生效位。"""
    captured: dict = {}

    async def fake_stream(**kwargs):
        captured.clear()
        captured.update(kwargs)
        yield SimpleNamespace(content="好", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    return captured


async def _say(conversation_id: str, text: str = "今天怎么样") -> None:
    """整轮跑完：只取首块会跳过后半段（落库与记忆），断言不到完整行为。"""
    [chunk async for chunk in BotChatService().stream_reply(conversation_id, text)]


def _prompt_of(captured: dict) -> str:
    return "\n".join(
        item["content"] for item in captured["messages"] if item["role"] == "system"
    )


TEXT_MARKERS = {
    "personality": "话不多但接得住情绪",
    "background": "住在一个靠海的小城",
    "likes_landmines": "喜欢夜里散步；雷区是被催",
    "memory_directive": "记住我在准备面试",
    "dialogue_samples": "晚风：又熬到几点",
    "advanced_play": "可以玩树洞模式",
}


@pytest.mark.asyncio
async def test_every_free_text_section_reaches_the_model(effect_db, capture):
    """六个自由文本分区 + 称呼 + 口头禅 + 禁用词：一个都不能只是存着。"""
    persona = db.create_bot_persona(
        name="晚风",
        address_user="小林",
        expression_dna={"catchphrase": "说来话长", "taboo": ["绝绝子"]},
        **TEXT_MARKERS,
    )
    conversation = db.create_bot_conversation(persona["id"])
    await _say(conversation["id"])

    prompt = _prompt_of(capture)
    for field, marker in TEXT_MARKERS.items():
        assert marker in prompt, field
    for marker in ("晚风", "小林", "说来话长", "绝绝子"):
        assert marker in prompt, marker
    # 底线仍压尾：新加的分区不许挤掉宪章的末位
    assert prompt.rstrip().endswith(SAFETY_CHARTER.rstrip())


@pytest.mark.asyncio
async def test_empty_sections_inject_nothing(effect_db, capture):
    """没填的分区不留空节：只有标题的节会挤掉一段有效指令。"""
    persona = db.create_bot_persona(name="极简", personality="温和")
    conversation = db.create_bot_conversation(persona["id"])
    await _say(conversation["id"])

    prompt = _prompt_of(capture)
    assert "温和" in prompt
    for marker in ("喜好与雷区", "说话样板", "进阶玩法", "记忆的分寸"):
        assert marker not in prompt, marker


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,fragment", [
    ("expression_dna", {"sentence": "short"}, SENTENCE["short"].rstrip("。")),
    ("expression_dna", {"particles": "none"}, PARTICLES["none"].rstrip("。")),
    ("expression_dna", {"punctuation": "ellipsis"}, PUNCTUATION["ellipsis"].rstrip("。")),
    ("expression_dna", {"emoji": "none"}, EMOJI["none"].rstrip("。")),
    ("refusal_style", "direct", REFUSAL_STYLES["direct"]),
])
async def test_each_gear_changes_the_prompt(effect_db, capture, field, value, fragment):
    """档位不是摆设：只动这一档，编译出来的提示词必须出现对应那句指令。"""
    persona = db.create_bot_persona(name="小暖", **{field: value})
    conversation = db.create_bot_conversation(persona["id"])
    await _say(conversation["id"])
    assert fragment in _prompt_of(capture)


@pytest.mark.asyncio
async def test_reaction_style_surfaces_only_when_its_situation_hits(effect_db, capture):
    """反应风格走情境库：八条全塞会互相稀释，所以未命中时一条都不该出现。"""
    persona = db.create_bot_persona(name="小暖", reaction_style="blunt")
    conversation = db.create_bot_conversation(persona["id"])

    await _say(conversation["id"])
    assert "此刻的分寸" not in _prompt_of(capture)

    await _say(conversation["id"], "你确定吗，别是我搞错了")
    assert REACTION_LIBRARY["doubt"]["blunt"] in _prompt_of(capture)


@pytest.mark.asyncio
async def test_model_choice_reply_length_and_thinking_reach_the_call(effect_db, capture):
    """聊天模型、回复长度、思考三项是行为差异，不在文案里，在下发参数里。"""
    chosen = db.create_model_config(
        name="指定", base_url="http://chosen/v1", model_id="chosen-m", max_output_tokens=2048
    )
    persona = db.create_bot_persona(
        name="小暖", model_id=chosen["id"], reply_style="concise", enable_thinking=1
    )
    conversation = db.create_bot_conversation(persona["id"])
    await _say(conversation["id"])

    assert capture["base_url"] == "http://chosen/v1"
    assert capture["model"] == "chosen-m"
    assert capture["enable_thinking"] is True
    assert capture["max_tokens"] == 512, "concise 的输出预算没接上"


@pytest.mark.asyncio
async def test_greeting_is_the_first_message_and_avatar_stays_out_of_prompt(effect_db, capture):
    """开场白只作为会话首条助手消息落库；头像 data URL 不该进 prompt（白烧 token）。"""
    persona = db.create_bot_persona(
        name="小暖", greeting="嗨，今天想聊点什么？", avatar="data:image/webp;base64,AAAA"
    )
    conversation = db.create_bot_conversation(persona["id"])
    assert [m["content"] for m in db.get_bot_messages(conversation["id"])] == ["嗨，今天想聊点什么？"]

    await _say(conversation["id"])
    assert "data:image" not in _prompt_of(capture)


@pytest.mark.asyncio
async def test_editing_takes_effect_in_an_ongoing_conversation(effect_db, capture):
    """「改了就生效」：同一会话在编辑前后各生成一次，第二次的 prompt 必须是新人格。"""
    persona = db.create_bot_persona(name="晚风", personality="温和", background="靠海小城")
    conversation = db.create_bot_conversation(persona["id"])

    await _say(conversation["id"])
    before = _prompt_of(capture)
    assert "温和" in before and "克制" not in before

    db.update_bot_persona(
        persona["id"],
        personality="克制",
        likes_landmines="雷区是被催",
        dialogue_samples="用户：在吗\n晚风：在。",
    )
    await _say(conversation["id"])
    after = _prompt_of(capture)
    assert "克制" in after and "雷区是被催" in after and "用户：在吗" in after
    assert "温和" not in after, "旧人格还留在会话快照里"


@pytest.mark.asyncio
async def test_relationship_speed_changes_the_stage_spoken_at(effect_db, capture):
    """升温速度决定同一累计分下用哪一档口气说话。"""
    slow = db.create_bot_persona(name="慢热", relationship_speed="slow")
    fast = db.create_bot_persona(name="自来熟", relationship_speed="fast")
    for persona in (slow, fast):
        db.bump_bot_relation(persona["id"], 40.0)

    prompts = []
    for persona in (slow, fast):
        conversation = db.create_bot_conversation(persona["id"])
        await _say(conversation["id"])
        prompts.append(_prompt_of(capture))

    assert prompts[0] != prompts[1], "relationship_speed 没参与阶段判定"
