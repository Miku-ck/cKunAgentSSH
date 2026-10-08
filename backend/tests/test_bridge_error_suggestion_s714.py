# -*- coding: utf-8 -*-
"""S7-14 回归：同一条 BridgeError 不该有四条文案。

原实现 `super().__init__(f"[{code}] {detail}" if detail else f"[{code}] {suggestion}")`
—— detail 非空就把 suggestion 顶掉。而定时任务里那三条 raise 都带 detail（未注入 token、
connect_ssh 空 sessionId、反向推断的原始异常），恰恰是最需要「请查看主进程日志」这类
指引的三条；只有 probe 那一条不带 detail，于是指引时有时无。
"""
import pytest

from agent.electron_bridge import (
    BridgeError,
    BridgeErrorCode,
    _BRIDGE_ERROR_SUGGESTIONS,
    classify_bridge_error,
)

ALL_CODES = list(BridgeErrorCode)


@pytest.mark.parametrize("code", ALL_CODES)
def test_detail_does_not_evict_the_suggestion(code):
    err = BridgeError(code, "connect_ssh 返回空 sessionId")
    text = str(err)
    assert code.value in text
    assert "connect_ssh 返回空 sessionId" in text
    assert _BRIDGE_ERROR_SUGGESTIONS[code] in text


@pytest.mark.parametrize("code", ALL_CODES)
def test_no_detail_still_carries_the_suggestion(code):
    assert _BRIDGE_ERROR_SUGGESTIONS[code] in str(BridgeError(code))


def test_classification_ignores_the_appended_prose():
    """把 suggestion 拼进 message 的新风险：文本嗅探会被指引措辞带偏。

    NOT_CONFIGURED 的建议里就含「主进程」，HTTP_ERROR 的建议含「主进程日志」——
    反向推断只准读 code，不许读文本，这条必须钉住。
    """
    err = BridgeError(BridgeErrorCode.HTTP_ERROR, "401 unauthorized")
    assert classify_bridge_error(err) is BridgeErrorCode.HTTP_ERROR
    # 非 BridgeError 才走文本嗅探（那一支本来的职责）
    assert classify_bridge_error(RuntimeError("401 unauthorized")) is BridgeErrorCode.HTTP_ERROR
