"""公网回调的报文上限必须是「先算字节再收」，不是「收完再算」。

routes/wechat_callback.py 是全仓唯一一个不挂 /api、因而拿不到 X-API-Token 的 POST
入口（平台回调带不了 token，鉴权靠 AES 密钥 + appid，而解密发生在读完报文之后）。
旧写法先 `await request.body()` 把整个流缓冲进内存，再比 64KB —— 上限只在事后成立；
而 Content-Length 是可选头，chunked 上传不带它，前置检查直接跳过。所以一个不含
Content-Length 的大报文能先把进程撑住，才收到 413。

这里跑真行为（不需要 HTTP 传输层：上限判据就在 _read_capped_body 里），并用
「消费到第几块」这个可观测量证明它是中途断开，而不是读完再拒绝。
"""
import pytest
from fastapi import HTTPException

from routes.wechat_callback import MAX_BODY_BYTES, _read_capped_body


class _FakeRequest:
    """只实现 _read_capped_body 用到的那两个面：headers.get 与 async stream()。"""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self._consumed = 0
        self.headers = dict(headers or {})

    async def stream(self):
        for chunk in self._chunks:
            self._consumed += 1
            yield chunk


@pytest.mark.asyncio
async def test_small_body_passes_through_unchanged():
    req = _FakeRequest([b'{"a":', b'1}'])
    assert await _read_capped_body(req) == b'{"a":1}'
    assert req._consumed == 2


@pytest.mark.asyncio
async def test_chunked_overflow_is_cut_off_mid_stream():
    """无 Content-Length 时无限分块：必须在越界那一块就断开，而不是读完全部。"""
    chunk = b"x" * (MAX_BODY_BYTES // 2 + 1)
    req = _FakeRequest([chunk, chunk, chunk, chunk, chunk])  # 5 块 = 上限的 2.5 倍
    with pytest.raises(HTTPException) as exc:
        await _read_capped_body(req)
    assert exc.value.status_code == 413
    # 越界发生在第 2 块：剩下的 3 块根本不该被读进内存
    assert req._consumed == 2, "上限只在事后成立 = 报文已经整个进过内存了"


@pytest.mark.asyncio
async def test_oversized_content_length_rejects_without_reading_a_byte():
    req = _FakeRequest([b"x" * 10], headers={"content-length": str(MAX_BODY_BYTES + 1)})
    with pytest.raises(HTTPException) as exc:
        await _read_capped_body(req)
    assert exc.value.status_code == 413
    assert req._consumed == 0


@pytest.mark.asyncio
async def test_exactly_at_limit_is_accepted():
    """上限是「超过」才拒：正好 64KB 的合法报文不能被自己挡掉。"""
    req = _FakeRequest([b"x" * MAX_BODY_BYTES])
    assert len(await _read_capped_body(req)) == MAX_BODY_BYTES
