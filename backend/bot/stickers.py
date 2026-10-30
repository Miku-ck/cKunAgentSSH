"""内置表情包：标记解析、合法键校验、素材路径。

模型只输出一个文本标记 `[表情:开心]`，由这里兑现成一张真图：
本地气泡渲染图片、QQ 走富媒体、微信有公网地址时发图片（否则回落颜文字）。
标记必须独占一条 —— 表情包是「另发一张图」，不是句子里的一个字符。
"""

import re
from dataclasses import dataclass
from pathlib import Path

STICKER_DIR = Path(__file__).resolve().parent / "stickers"

# 键同时是给模型看的名字：中文键比 happy/shy 更贴近模型对「情绪词」的语感。
# when 直接进提示词，写「什么时候用」而不是形容词。
@dataclass(frozen=True)
class Sticker:
    key: str
    file: str
    kaomoji: str
    when: str


@dataclass(frozen=True)
class Pack:
    """一套表情包：id 给前端做 tab 键，name 给人看。"""

    id: str
    name: str
    stickers: tuple[Sticker, ...]


# 第一套：无字版，表情本身即台词。
_WHALE: tuple[Sticker, ...] = (
    Sticker("开心", "happy.png", "(≧▽≦)", "被逗笑、事情成了"),
    Sticker("害羞", "shy.png", "(/ω＼)", "被夸、有点不好意思"),
    Sticker("无语", "speechless.png", "(￣▽￣)…", "对方说了离谱或接不住的话"),
    Sticker("加油", "cheer.png", "٩(๑•̀ω•́๑)۶", "给对方打气、鼓励"),
    Sticker("委屈", "sulk.png", "(｡•́︿•̀｡)", "被冷落、撒娇式不满"),
    Sticker("困了", "sleepy.png", "(－_－) zzZ", "深夜、道晚安"),
    Sticker("生气", "angry.png", "(｀へ´)", "轻微不满（正经冲突别用）"),
    Sticker("喜欢", "love.png", "(♥ω♥*)", "表达喜欢、亲近"),
)

# 第二套：同角色的梗短句版。键就是图上的字，所以新键不能和第一套撞车。
_WHALE_TEXT: tuple[Sticker, ...] = (
    Sticker("好耶", "text-happy.png", "ヽ(°▽°)ノ", "事情成了、收到好消息"),
    Sticker("诶嘿", "text-shy.png", "(〃▽〃)", "被夸、想糊弄过去"),
    Sticker("服了", "text-speechless.png", "(￣▽￣)…", "对方说了离谱或接不住的话"),
    Sticker("冲鸭", "text-cheer.png", "٩(๑•̀ω•́๑)۶", "给对方打气、鼓励"),
    Sticker("呜呜", "text-sulk.png", "(｡•́︿•̀｡)", "被冷落、撒娇式不满"),
    Sticker("困困", "text-sleepy.png", "(－_－) zzZ", "深夜、道晚安"),
    Sticker("哼", "text-angry.png", "(｀へ´)", "轻微不满（正经冲突别用）"),
    Sticker("贴贴", "text-love.png", "(♥ω♥*)", "表达喜欢、亲近"),
)

PACKS: tuple[Pack, ...] = (
    Pack("whale", "鲸鱼娘", _WHALE),
    Pack("whale-text", "鲸鱼娘·文字", _WHALE_TEXT),
)

STICKERS: tuple[Sticker, ...] = tuple(item for pack in PACKS for item in pack.stickers)

_BY_KEY = {item.key: item for item in STICKERS}

# 只认整条标记，不做行内替换：行内出现的一律当噪声删掉。
STICKER_RE = re.compile(r"\[表情[:：]\s*([^\[\]\s]{1,8})\s*\]")


def get(key: str | None) -> Sticker | None:
    return _BY_KEY.get(key or "")


def bubble_key(text: str) -> str | None:
    """整条只有一个合法标记时返回键；带别的字或未知键都返回 None。"""
    match = STICKER_RE.fullmatch((text or "").strip())
    if not match:
        return None
    key = match.group(1)
    return key if key in _BY_KEY else None


def strip_tokens(text: str) -> str:
    return STICKER_RE.sub("", text or "").strip()


def normalize(text: str, *, enabled: bool = True) -> str:
    """落库前的确定性收口：未知键、行内标记、多张表情包都在这儿处理。

    未知键直接丢（宁可少一张图，也不把 [表情:xxx] 原样发出去）；
    一轮最多留第一张；被清空的条不再占位。
    """
    chunks = [chunk.strip() for chunk in re.split(r"\n[ \t]*\n", text or "") if chunk.strip()]
    out: list[str] = []
    used = False
    for chunk in chunks:
        key = bubble_key(chunk)
        if key:
            if enabled and not used:
                out.append(f"[表情:{key}]")
                used = True
            continue
        cleaned = strip_tokens(chunk)
        if cleaned:
            out.append(cleaned)
    return "\n\n".join(out)


def path_for(key: str | None) -> Path | None:
    sticker = get(key)
    if not sticker:
        return None
    path = STICKER_DIR / sticker.file
    return path if path.is_file() else None


def kaomoji_for(key: str | None) -> str:
    sticker = get(key)
    return sticker.kaomoji if sticker else ""


_DENSITY_RULE = {
    "low": "情绪到位时偶尔发一张（大约三四轮一次），别每轮都发。",
    "medium": "可以多用表情包，但一轮最多一张。",
}


def prompt_block(density: str | None) -> str:
    """表情包的使用规则 + 可选项。density 为 none 时不注入。"""
    if density not in _DENSITY_RULE or not STICKERS:
        return ""
    # 两套风格要分开列：模型得知道「无字」和「梗短句」是两种语气，不能混着理解。
    catalog = "；".join(
        f"{pack.name}：" + "、".join(f"{item.key}（{item.when}）" for item in pack.stickers)
        for pack in PACKS
    )
    return (
        "表情包：需要表达情绪时，单独发一条标记，例如 [表情:开心]，对方会收到一张真图。"
        "标记必须独占一条（前后用空行隔开），这条里不写别的字；"
        f"{_DENSITY_RULE[density]}连续两轮别都发，拿不准就不发。可用：{catalog}。"
    )
