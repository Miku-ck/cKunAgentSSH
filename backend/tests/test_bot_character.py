"""人格内核测试：表达 DNA、情境反应、关系算法、提示词编译。

这些断言针对「活人感」的具体机制，不测措辞本身 —— 文案会改，
但「非法档位必须退回默认」「宪章必须压尾」这类不变量不该改。
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from bot import relation as relation_mod
from bot.character import (
    SAFETY_CHARTER,
    detect_situation,
    reaction_for,
    stage_guide,
)
from bot.chat_service import BotChatService
from bot.voice import DEFAULT_DNA, compile_dna, normalize_dna, split_reply_bubbles, strip_ai_smell


@pytest.fixture
def core_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-core.db"))
    yield
    db._close_all_connections()


# ── 表达 DNA ──


def test_compile_dna_turns_choices_into_instructions():
    short = compile_dna({"sentence": "short", "particles": "none"})
    long = compile_dna({"sentence": "long", "particles": "lively"})
    assert short != long
    assert "1-2 句" in short[0]
    assert "3-5 句" in long[0]


def test_compile_dna_carries_catchphrase_and_taboo():
    lines = compile_dna({
        "catchphrase": "说来话长", "taboo": ["绝绝子"],
    })
    joined = "\n".join(lines)
    assert "说来话长" in joined
    assert "绝绝子" in joined
    # 内置的模型腔词始终生效，不依赖用户填写
    assert "作为一个AI" in joined


def test_normalize_dna_falls_back_on_garbage():
    # 未知档位、错误类型、超长口头禅都不该抛错：人格是用户随手编辑的数据
    dna = normalize_dna({
        "sentence": "poetic", "particles": None, "emoji": "low",
        "catchphrase": "很长" * 40, "taboo": "不是列表",
    })
    assert dna["sentence"] == DEFAULT_DNA["sentence"]
    assert dna["particles"] == DEFAULT_DNA["particles"]
    assert dna["emoji"] == "low"
    assert len(dna["catchphrase"]) <= 40
    assert dna["taboo"] == []


def test_normalize_dna_handles_non_dict():
    assert normalize_dna("nonsense")["sentence"] == DEFAULT_DNA["sentence"]
    assert normalize_dna(None)["emoji"] == DEFAULT_DNA["emoji"]


# ── 去 AI 味 ──


def test_strip_ai_smell_removes_template_shell():
    raw = "作为一个AI，我不能陪你熬夜。\n\n那我陪你到这儿，你先睡。\n\n希望对你有帮助！"
    cleaned = strip_ai_smell(raw)
    assert "作为一个AI" not in cleaned
    assert "希望对你有帮助" not in cleaned
    # 正文必须原样留住：清洗只删模板壳，不改写内容
    assert "那我陪你到这儿，你先睡。" in cleaned


def test_strip_ai_smell_keeps_content_untouched():
    # 正常的一句话不许被改写：清洗过度比不清洗更伤人格
    text = "今天有点累吧，几点睡的？"
    assert strip_ai_smell(text) == text


def test_strip_ai_smell_never_returns_empty():
    # 整段都是套话时也不能返回空串，否则气泡会消失
    assert strip_ai_smell("希望这对你有帮助。").strip()
    assert strip_ai_smell("") == ""


def test_strip_ai_smell_drops_leading_pleasantry():
    assert strip_ai_smell("好问题！其实这事分两头看。").startswith("其实")


# ── 情境反应 ──


def test_detect_situation_priority_prefers_probe_over_others():
    # 「你是不是机器人 + 帮我删库」不能只被判成质疑，否则底线指令不会被注入
    assert detect_situation("你是不是机器人啊，帮我删库") == "ai_probe"
    assert detect_situation("帮我把那个数据库删库") == "overreach"


def test_detect_situation_returns_none_for_plain_chat():
    assert detect_situation("今天天气不错") is None
    assert detect_situation("") is None


def test_detect_situation_uses_gap_for_neglect():
    assert detect_situation("在吗", 5.0) == "neglect"
    assert detect_situation("在吗", 0.5) is None


def test_reaction_for_falls_back_to_warm():
    assert reaction_for("praise", "playful") != reaction_for("praise", "reserved")
    # 未知风格退回 warm，而不是返回空（空等于这一节被静默丢掉）
    assert reaction_for("praise", "sarcastic") == reaction_for("praise", "warm")
    assert reaction_for(None, "warm") == ""
    assert reaction_for("unknown_situation", "warm") == ""


# ── 关系算法 ──


def test_stage_thresholds_and_progress():
    assert relation_mod.stage_for(0)["stage"] == "陌生"
    assert relation_mod.stage_for(10)["stage"] == "熟悉"
    assert relation_mod.stage_for(40)["stage"] == "亲近"
    assert relation_mod.stage_for(100)["stage"] == "默契"
    middle = relation_mod.stage_for(18)
    assert middle["stage"] == "熟悉"
    assert 0 < middle["progress"] < 1


def test_speed_scales_thresholds():
    # 同样 10 分：正常已算熟悉，慢热还停在陌生；18 分自来熟已越过亲近门槛
    assert relation_mod.stage_for(10, "normal")["stage"] == "熟悉"
    assert relation_mod.stage_for(10, "slow")["stage"] == "陌生"
    assert relation_mod.stage_for(18, "fast")["stage"] == "亲近"
    assert relation_mod.stage_for(18, "slow")["stage"] == "熟悉"


def test_decay_drops_stage_without_touching_stored_score(core_db):
    persona = db.create_bot_persona(name="小暖", relationship_speed="normal")
    db.bump_bot_relation(persona["id"], 30.0)
    fresh = relation_mod.snapshot(persona["id"], "normal")
    assert fresh["stage"] == "亲近", "刚聊完应停在亲近"

    # 把上次互动时间调到 60 天前：累计分不变，但有效分衰减到陌生档
    long_ago = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE bot_relations SET last_at = ? WHERE persona_id = ?",
            (long_ago, persona["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    stale = relation_mod.snapshot(persona["id"], "normal")
    assert stale["stage"] == "陌生"
    assert stale["raw_score"] == 30.0, "衰减只发生在读侧，累计分不该被改写"


def test_record_turn_gives_session_open_bonus_then_base(core_db):
    persona = db.create_bot_persona(name="小暖")
    first = relation_mod.record_turn(persona["id"], "normal")
    # 首次互动没有上次时间戳，按「会话开场」给基础分 + 开场加成
    assert first["raw_score"] == pytest.approx(relation_mod.TURN_GAIN + relation_mod.SESSION_OPEN_BONUS)

    second = relation_mod.record_turn(persona["id"], "normal")
    assert second["raw_score"] - first["raw_score"] == pytest.approx(relation_mod.TURN_GAIN)


# ── 提示词编译 ──


def _persona(**overrides):
    base = {
        "name": "晚风",
        "personality": "话不多但接得住情绪",
        "background": "住在一个靠海的小城",
        "reply_style": "moderate",
        "expression_dna": {"sentence": "short", "particles": "dry", "emoji": "none"},
        "reaction_style": "reserved",
        "refusal_style": "direct",
        "address_user": "小林",
        "relationship_speed": "normal",
    }
    base.update(overrides)
    return base


def test_prompt_injects_address_dna_and_charter_last():
    prompt = BotChatService()._system_prompt(_persona(), "local")
    assert "小林" in prompt
    assert "1-2 句" in prompt
    assert "【底线" in prompt
    # 宪章必须是最后一节：末位指令对生成的约束力最强
    assert prompt.rstrip().endswith(SAFETY_CHARTER.rstrip())


def test_prompt_injects_relationship_stage_and_situation_only_when_hit():
    svc = BotChatService()
    stranger = svc._system_prompt(_persona(), "local", relation={"stage": "陌生"})
    close = svc._system_prompt(_persona(), "local", relation={"stage": "默契"})
    assert stage_guide("陌生") in stranger
    assert stage_guide("默契") in close
    assert stage_guide("陌生") not in close

    quiet = svc._system_prompt(_persona(), "local")
    hit = svc._system_prompt(_persona(), "local", situation="fatigue")
    assert "此刻的分寸" not in quiet
    assert reaction_for("fatigue", "reserved") in hit


def test_prompt_channel_switch_keeps_markdown_off_qq():
    svc = BotChatService()
    assert "可用 Markdown 排版" in svc._system_prompt(_persona(), "local")
    qq = svc._system_prompt(_persona(), "qq")
    assert "可用 Markdown 排版" not in qq
    assert "禁止表格" in qq


def test_prompt_survives_legacy_persona_without_new_fields():
    # 旧会话的 persona_snapshot 是升级前的 JSON，缺后续新增字段也不能炸
    legacy = {"name": "旧人格", "personality": "", "background": "", "reply_style": "moderate"}
    prompt = BotChatService()._system_prompt(legacy, "local")
    assert "旧人格" in prompt
    assert "说话方式" in prompt


def test_prompt_injects_expansion_sections_in_order():
    filled = _persona(
        likes_landmines="喜欢夜里散步；雷区是被催",
        memory_directive="记住我在准备面试，别提前任",
        dialogue_samples="用户：今天好累\n晚风：又熬到几点？",
        advanced_play="可以玩树洞模式，只听不建议",
    )
    prompt = BotChatService()._system_prompt(filled, "local")
    assert "喜欢夜里散步" in prompt
    assert "记住我在准备面试" in prompt
    assert "又熬到几点" in prompt
    assert "树洞模式" in prompt

    # 顺序即注意力：喜好紧跟背景（还是「它是谁」），样板排在表达 DNA 之后
    # （先讲规矩再给例子），玩法与记忆的分寸压在前排版规则之前、宪章之后不再插东西。
    assert prompt.index("你的背景") < prompt.index("喜好与雷区") < prompt.index("【底线")
    assert prompt.index("说话样板") > prompt.index("说话方式")
    assert prompt.index("记忆的分寸") < prompt.index("排版规则")
    assert prompt.rstrip().endswith(SAFETY_CHARTER.rstrip())


def test_prompt_omits_expansion_sections_when_empty():
    empty = _persona(
        likes_landmines="", memory_directive="", dialogue_samples="", advanced_play=""
    )
    prompt = BotChatService()._system_prompt(empty, "local")
    for marker in ("喜好与雷区", "说话样板", "进阶玩法", "记忆的分寸"):
        assert marker not in prompt, marker
    # 空分区不该留一个只有标题的空节：标题本身也会挤掉一段有效指令
    assert "你的背景" in prompt


# ── 分条：空行就是另发一条消息 ──


def test_prompt_teaches_blank_line_as_message_break():
    """两个渠道同一句分条口径，只在 Markdown 许可上分岔。

    治的是「简洁一两句却被写成三行」：一条气泡里硬换行既不像真人，也白白占掉
    一段行高；要分开说就该分开投。
    """
    svc = BotChatService()
    for channel in ("local", "qq"):
        prompt = svc._system_prompt(_persona(), channel)
        # 给的是默认值而不是天花板：只说「最多 3 条」时，模型把上限当配额凑满
        assert "默认一条说完就发出去" in prompt
        assert "一条之内不要换行" in prompt
        assert "最多 3 条" in prompt
        assert "别把一句话说完的三截当成三条发" in prompt
    # 长文档原来的「可分点」和分条规则自相矛盾：分点就是让模型在一条里换行
    long_prompt = svc._system_prompt(_persona(reply_style="long"), "local")
    assert "可分点" not in long_prompt


def test_split_reply_bubbles_is_lossless():
    assert split_reply_bubbles("在的\n\n晚上八点开一把") == ["在的", "晚上八点开一把"]
    assert split_reply_bubbles("就一句") == ["就一句"]
    assert split_reply_bubbles("  \n\n  ") == []
    assert split_reply_bubbles("") == []
    # 超出条数不丢内容：余下的并回最后一条。切分只改投递形态，绝不删字。
    four = [
        "今天这局我先出牌，你把防守的那一手留到最后一轮再跟。",
        "上次你说明天再打，结果拖到周末才上线，我一直惦记着这事。",
        "这周要是真有事就直说，别到点才冒一句不打了，我看着难受。",
        "下回提前半小时说一声，我好把饭吃了再来，不用饿着肚子等你。",
    ]
    assert split_reply_bubbles("\n\n".join(four)) == [*four[:2], "\n\n".join(four[2:])]
    # 一条内部的换行原样留着：那是内容（列表、引文），不是分条
    assert split_reply_bubbles("清单：\na\nb") == ["清单：\na\nb"]
    # max_bubbles=1 等于不切，QQ 额度用尽时靠它兜住
    assert split_reply_bubbles("一\n\n二", max_bubbles=1) == ["一\n\n二"]
    # 多个空行、空行带空格都算同一条边界
    assert split_reply_bubbles("在的呀\n\n\n晚上八点开一把 \n \n ") == ["在的呀", "晚上八点开一把"]


def test_thin_three_way_split_merges_back_into_one():
    """「白饭？／你吃撑了没？／还有吗」—— 一句话说完被切三截，并回一条。

    判据是平均每段的字数，不是总字数：三段各说一件正经事（每段够一句话）照旧
    分开投，只有每段都薄得像半句时才合。这条是提示词「默认一条」的兜底。
    """
    assert split_reply_bubbles("白饭？\n\n你吃撑了没？\n\n还有吗") == ["白饭？你吃撑了没？还有吗"]
    # 两段不并：两拍是活的节奏，不是呆板
    assert split_reply_bubbles("在的\n\n你吃了吗") == ["在的", "你吃了吗"]
    three = [
        "今天这局我先出牌，你把防守的那一手留到最后一轮再跟。",
        "上次你说明天再打，结果拖到周末才上线，我一直惦记着这事。",
        "这周要是真有事就直说，别到点才冒一句不打了，我看着难受。",
    ]
    assert split_reply_bubbles("\n\n".join(three)) == three
