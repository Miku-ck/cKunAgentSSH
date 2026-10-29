"""微信平台侧片段：解密后的 XML 解析、出站 XML 组装与分段。

连接与 HTTP 在 wechat_manager.py；消息流程仍在 inbound_pipeline.py。
"""

import xml.etree.ElementTree as ET

import pytest

from bot.wechat_channel import (
    WECHAT_TEXT_LIMIT,
    build_kefustate_xml,
    build_sendmsg_xml,
    extract_text,
    inbound_event_id,
    parse_inbound,
    split_wechat_parts,
)

DECRYPTED = """<xml>
  <userid>o-user-1</userid>
  <appid>wx-app</appid>
  <content><msg>今天北京多少度</msg></content>
  <event></event>
  <from>0</from>
  <kfstate>0</kfstate>
  <channel>0</channel>
  <assessment>0</assessment>
  <createtime>1735689600</createtime>
</xml>"""


def test_parse_inbound_reads_platform_identity_and_text():
    inbound = parse_inbound(DECRYPTED)

    assert inbound.userid == "o-user-1"
    assert inbound.appid == "wx-app"
    assert inbound.msg == "今天北京多少度"
    assert inbound.from_ == 0
    assert inbound.channel == 0
    assert inbound.kfstate == 0
    assert inbound.createtime == "1735689600"
    assert inbound.event == ""


def test_parse_inbound_rejects_broken_xml():
    with pytest.raises(Exception):
        parse_inbound("<xml><userid>oops")


def test_extract_text_keeps_plain_text_and_unwraps_json_text():
    assert extract_text("你好呀") == "你好呀"
    assert extract_text('{"text": "在的"}') == "在的"
    # 图片/语音/卡片：P0 不处理，返回空串让调用方忽略
    assert extract_text('{"image": {"media_id": "x"}}') == ""
    # 不是 JSON 就按纯文本，别把用户打的括号吃掉
    assert extract_text("{这不像是 json}") == "{这不像是 json}"


def test_sendmsg_xml_escapes_cdata_terminator():
    xml_text = build_sendmsg_xml(
        appid="wx-app", openid="o-1", text="前半]]>后半 & <tag>", channel=0
    )
    root = ET.fromstring(xml_text)

    assert root.findtext("appid") == "wx-app"
    assert root.findtext("openid") == "o-1"
    assert root.findtext("msg") == "前半]]>后半 & <tag>"
    assert root.findtext("channel") == "0"


def test_sendmsg_xml_omits_appid_for_h5_channel():
    root = ET.fromstring(build_sendmsg_xml(appid="", openid="o-1", text="hi", channel=7))

    assert root.find("appid") is None
    assert root.findtext("channel") == "7"


def test_kefustate_xml_carries_the_state():
    root = ET.fromstring(
        build_kefustate_xml(appid="wx-app", openid="o-1", state="personserving")
    )

    assert root.findtext("appid") == "wx-app"
    assert root.findtext("openid") == "o-1"
    assert root.findtext("kefustate") == "personserving"


def test_outbound_splits_bubbles_and_strips_markdown():
    parts = split_wechat_parts("**第一段**\n\n`第二段`")

    assert parts == ["第一段", "第二段"]


def test_outbound_respects_the_platform_length_limit():
    parts = split_wechat_parts("啊" * (WECHAT_TEXT_LIMIT * 2))

    assert len(parts) >= 2
    assert all(len(part) <= WECHAT_TEXT_LIMIT for part in parts)


def test_outbound_caps_at_three_bubbles():
    # 每段都要够厚，否则会命中「三截凑数并回一条」的兜底
    parts = split_wechat_parts("\n\n".join(f"第{index}段：" + "内容" * 12 for index in range(5)))

    assert len(parts) == 3


def test_event_id_is_stable_for_the_same_message_and_varies_by_content():
    first = inbound_event_id("o-1", "1735689600", "在吗")
    again = inbound_event_id("o-1", "1735689600", "在吗")
    other = inbound_event_id("o-1", "1735689600", "在嘛")

    assert first == again
    assert first != other
    assert first.isalnum()
