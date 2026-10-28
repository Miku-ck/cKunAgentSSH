"""表达 DNA —— 把「说话方式」从形容词变成可执行约束。

问题：用户写「温柔、幽默、自然」，对模型几乎没有信息量，生成结果仍是通用助手腔。
但用户能准确判断「要不要用语气词」「句末用不用省略号」这类离散选项。

所以表达拆成若干档位，每档对应一句可执行的写作约束；枚举负责活人感下限，
personality 自由文本负责上限（具体的世界观与经历）。
"""

import re
from typing import Any

# ── 档位字典 ──
# 键即值为合法取值；文案直接进 system prompt，因此必须是一句可执行指令，
# 不能是形容词（"活泼一点"是形容词，"句尾用呀/啦"是指令）。

SENTENCE = {
    "short": "多数回复 1-2 句，一句话说清一件事，不铺垫、不复述。",
    "mixed": "长短交错，有的一句话抢答，有的三五句展开，别通篇一个节奏。",
    "long": "需要讲清楚时用 3-5 句说透，不写成分点报告。",
}

PARTICLES = {
    "none": "不用语气词，情绪靠句子本身承载。",
    "soft": "偶尔带语气词（嗯、啊、欸），平均两三条一次，别每句都挂。",
    "lively": "语气词可以多些（呀、啦、嘛），句尾有起伏。",
    "dry": "语气克制（哦、行、嗯），少用情绪化语气词。",
}

PUNCTUATION = {
    "plain": "标点正常，该断句就断句。",
    "ellipsis": "句末偶尔用省略号，留点没说完的意思。",
    "tilde": "轻松场合句尾可用～，严肃话题不用。",
    "exclaim": "情绪上来用感叹号，一条最多一个。",
}

EMOJI = {
    "none": "不发表情。",
    "low": "偶尔一个 emoji（约五六条一次），不连发。",
    "medium": "常用 emoji，一条最多两个。",
}

# 内置禁用词：这些是模型腔最集中的标记，用户不必填写，始终生效。
BUILTIN_TABOO = (
    "作为一个AI",
    "作为人工智能",
    "作为语言模型",
    "作为大模型",
    "希望对你有帮助",
    "有什么我可以帮",
    "很高兴为您服务",
    "总的来说",
    "综上所述",
)

DEFAULT_DNA: dict[str, Any] = {
    "sentence": "mixed",
    "particles": "soft",
    "punctuation": "plain",
    "emoji": "low",
    "catchphrase": "",
    "taboo": [],
}

# 供前端渲染控件：只给 value + label，prompt 文案留在服务端，避免两边各写一份。
DNA_OPTIONS = {
    "sentence": {
        "label": "句子长短",
        "options": [
            {"value": "short", "label": "短句为主"},
            {"value": "mixed", "label": "长短交错"},
            {"value": "long", "label": "允许展开"},
        ],
    },
    "particles": {
        "label": "语气词",
        "options": [
            {"value": "none", "label": "不用"},
            {"value": "soft", "label": "偶尔"},
            {"value": "lively", "label": "多一些"},
            {"value": "dry", "label": "克制"},
        ],
    },
    "punctuation": {
        "label": "标点习惯",
        "options": [
            {"value": "plain", "label": "常规"},
            {"value": "ellipsis", "label": "爱用省略号"},
            {"value": "tilde", "label": "爱用波浪号"},
            {"value": "exclaim", "label": "爱用感叹号"},
        ],
    },
    "emoji": {
        "label": "表情密度",
        "options": [
            {"value": "none", "label": "不用"},
            {"value": "low", "label": "偶尔"},
            {"value": "medium", "label": "常用"},
        ],
    },
}


def normalize_dna(raw: Any) -> dict[str, Any]:
    """把任意输入收敛成合法 DNA：非法档位退回默认，绝不抛错。

    人格是用户随手编辑的数据，脏值只会让陪聊变哑，不该让整个页面 500。
    """
    data = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_DNA)
    for key, table in (
        ("sentence", SENTENCE),
        ("particles", PARTICLES),
        ("punctuation", PUNCTUATION),
        ("emoji", EMOJI),
    ):
        value = data.get(key)
        if isinstance(value, str) and value in table:
            out[key] = value
    catchphrase = data.get("catchphrase")
    if isinstance(catchphrase, str):
        out["catchphrase"] = catchphrase.strip()[:40]
    taboo = data.get("taboo")
    if isinstance(taboo, list):
        out["taboo"] = [str(item).strip()[:20] for item in taboo if str(item).strip()][:12]
    return out


def compile_dna(raw: Any) -> list[str]:
    """把 DNA 编译成 1-2 行提示词：分节太多会让模型抓不住重点，也白烧 token。"""
    dna = normalize_dna(raw)
    style = "；".join((
        SENTENCE[dna["sentence"]].rstrip("。"),
        PARTICLES[dna["particles"]].rstrip("。"),
        PUNCTUATION[dna["punctuation"]].rstrip("。"),
        EMOJI[dna["emoji"]].rstrip("。"),
    ))
    lines = [f"说话方式：{style}。"]
    extras: list[str] = []
    if dna["catchphrase"]:
        extras.append(f"你的口头禅是「{dna['catchphrase']}」，偶尔冒一句，别每条都带")
    forbidden = list(BUILTIN_TABOO) + [word for word in dna["taboo"] if word]
    if forbidden:
        extras.append("这些词别用：" + "、".join(forbidden[:16]))
    if extras:
        lines.append("；".join(extras) + "。")
    return lines


# ── 输出侧兜底 ──
# 提示词是软约束，模型仍会漏出模板腔。这里只删「明显不属于任何人格」的句子，
# 不重写、不改写、不删正文 —— 清洗过度比不清洗更伤人格。

_OPENERS = re.compile(
    r"(?m)^[ \t]*(?:好问题|这是个好问题|很好的问题|谢谢你的分享|谢谢你的信任|"
    r"我理解你的感受|我明白你的感受)[！!，,。.…~\s]*"
)
_TAILS = re.compile(
    r"(?:希望(?:这|以上|这些)?(?:些)?(?:内容|回答)?(?:能)?(?:对你)?有(?:所)?帮助"
    r"|如果(?:你)?还有(?:其他|别的)?(?:问题|需要|想聊)[^。！？\n]{0,14}"
    r"|随时(?:可以)?(?:告诉|找|叫)我[^。！？\n]{0,8})[。.!！~]*\s*\Z"
)
_AI_SELF = re.compile(
    r"作为(?:一个|一名)?\s*(?:AI|人工智能|人工智能助手|语言模型|大模型|智能助手)"
    r"[，,]?[^。！？\n]*[。！？]?"
)
_ORDINALS = re.compile(r"(?m)^[ \t]*(?:首先|其次|再次|最后|第一|第二|第三)[，,、]\s*")


def strip_ai_smell(text: str) -> str:
    """去掉模板腔。清空或改动过度时退回原文 —— 宁可留着套话，也不能把回复删没。"""
    if not text or not text.strip():
        return text
    cleaned = _AI_SELF.sub("", text)
    cleaned = _OPENERS.sub("", cleaned)
    cleaned = _ORDINALS.sub("", cleaned)
    cleaned = _TAILS.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    # 兜底：正文塌掉三成以上（或几乎清空）说明正则吃到了内容，退回原文。
    # 阈值取 0.3 而非 0.5：一句「作为一个AI…」+ 一句结尾套话，正常就会掉一半，
    # 阈值定太高等于把有意义的清洗也回滚掉。
    if len(cleaned) < max(4, len(text.strip()) * 0.3):
        return text.strip()
    return cleaned
