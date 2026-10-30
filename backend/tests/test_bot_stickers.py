"""表情包标记：解析、落库收口、提示词与静态素材路由。

标记是跨层契约（模型 → 文本 → 本地/QQ/微信），所以这里测的是
「哪条能变成图、哪条必须被清掉」，不是图片本身。
"""

import fastapi
from fastapi.testclient import TestClient

from bot import stickers
from bot.voice import DEFAULT_DNA, DNA_OPTIONS, compile_dna, normalize_dna, split_reply_bubbles


def test_manifest_files_exist_and_keys_are_unique():
    keys = [item.key for item in stickers.STICKERS]
    assert len(keys) == len(set(keys))
    for item in stickers.STICKERS:
        path = stickers.STICKER_DIR / item.file
        assert path.is_file(), item.file
        assert path.read_bytes().startswith(b"\x89PNG"), item.file


def test_packs_are_nonempty_with_unique_ids():
    assert [pack.id for pack in stickers.PACKS] == ["whale", "whale-text"]
    assert len({pack.id for pack in stickers.PACKS}) == len(stickers.PACKS)
    assert all(pack.stickers for pack in stickers.PACKS)


def test_bubble_key_only_accepts_a_whole_known_marker():
    assert stickers.bubble_key("[表情:开心]") == "开心"
    assert stickers.bubble_key("  [表情：害羞]  ") == "害羞"
    # 带别的字 = 行内噪声，不是「另发一张图」
    assert stickers.bubble_key("笑死[表情:开心]") is None
    assert stickers.bubble_key("[表情:不明]") is None
    assert stickers.bubble_key("[表情:开心]\n还有一句") is None
    assert stickers.bubble_key("") is None


def test_strip_tokens_removes_inline_and_keeps_body():
    assert stickers.strip_tokens("笑死[表情:开心]") == "笑死"
    assert stickers.strip_tokens("[表情:开心]\n正文") == "\n正文".strip()


def test_normalize_drops_unknown_inline_and_extra_stickers():
    text = "第一句\n\n[表情:开心]\n\n[表情:害羞]\n\n第二句[表情:不明]"
    assert stickers.normalize(text) == "第一句\n\n[表情:开心]\n\n第二句"


def test_normalize_respects_disabled_stickers():
    text = "第一句\n\n[表情:开心]\n\n第二句"
    assert stickers.normalize(text, enabled=False) == "第一句\n\n第二句"


def test_normalize_returns_empty_when_nothing_displayable():
    assert stickers.normalize("[表情:开心]", enabled=False) == ""
    assert stickers.normalize("[表情:不明]") == ""


def test_path_and_kaomoji_lookup():
    assert stickers.path_for("困了") is not None
    assert stickers.path_for("不明") is None
    assert stickers.kaomoji_for("困了").startswith("(")
    assert stickers.kaomoji_for("不明") == ""


def test_prompt_block_lists_catalog_and_density():
    block = stickers.prompt_block("low")
    assert "[表情:开心]" in block
    assert "开心（被逗笑、事情成了）" in block
    assert "三四轮一次" in block
    assert stickers.prompt_block("medium") != block
    assert stickers.prompt_block("none") == ""


def test_split_reply_bubbles_never_merges_a_sticker_into_text():
    # 字少也不能并：并了就变成句子里的标记
    assert split_reply_bubbles("哈\n\n[表情:开心]\n\n真的假的") == [
        "哈",
        "[表情:开心]",
        "真的假的",
    ]
    # 超出条数时表情包不塞进收尾那条
    three = [
        "今天这局我先出牌，你把防守的那一手留到最后一轮再跟。",
        "上次你说明天再打，结果拖到周末才上线，我一直惦记着这事。",
        "这周要是真有事就直说，别到点才冒一句不打了，我看着难受。",
    ]
    parts = split_reply_bubbles("\n\n".join(three) + "\n\n[表情:开心]")
    assert len(parts) == 3
    assert parts[-1] == "\n\n".join(three[2:])


def test_dna_has_kaomoji_and_sticker_axes_with_defaults():
    assert DEFAULT_DNA["kaomoji"] == "low"
    assert DEFAULT_DNA["sticker"] == "low"
    assert [item["value"] for item in DNA_OPTIONS["kaomoji"]["options"]] == [
        "none",
        "low",
        "medium",
    ]
    assert DNA_OPTIONS["sticker"]["label"] == "表情包"
    assert normalize_dna({})["sticker"] == "low"
    assert normalize_dna({"sticker": "none"})["sticker"] == "none"
    assert normalize_dna({"kaomoji": "always"})["kaomoji"] == "low"


def test_compile_dna_speaks_both_new_axes():
    joined = "\n".join(compile_dna({"kaomoji": "medium", "sticker": "medium"}))
    assert "颜文字" in joined
    assert "表情包" in joined
    quiet = "\n".join(compile_dna({"kaomoji": "none", "sticker": "none"}))
    assert "不用颜文字" in quiet
    assert "不发表情包" in quiet


def test_sticker_route_serves_png_and_404s_unknown_keys():
    from routes.bot_stickers import router

    app = fastapi.FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        ok = client.get("/assets/stickers/开心")
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "image/png"
        assert ok.content.startswith(b"\x89PNG")

        # 未知键与路径穿越都必须在清单校验处被拦下
        assert client.get("/assets/stickers/不明").status_code == 404
        assert client.get("/assets/stickers/..%2F..%2Fdatabase.py").status_code == 404
