"""微信对话开放平台的报文加解密（AES-256-CBC + PKCS#7/32）。

密文格式：base64( AES[random(16B) + msg_len(4B) + msg + appid] )。
密钥：EncodingAESKey 43 位 → base64decode(key + "=") → 32 字节；IV = key[:16]。
测试里的「独立实现」直接用 cryptography 拼装/拆解，不复用被测类的分组逻辑。
"""

import base64

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bot.wechat_crypto import (
    WeChatCrypto,
    WeChatCryptoError,
    decode_aes_key,
)

APPID = "wx1234567890abcdef"
RAW_KEY = bytes(range(32))
AES_KEY = base64.b64encode(RAW_KEY).decode().rstrip("=")
assert len(AES_KEY) == 43


def _raw_encrypt(plaintext: str, *, appid: str = APPID, prefix: bytes = b"\x07" * 16) -> str:
    msg = plaintext.encode("utf-8")
    payload = prefix + len(msg).to_bytes(4, "big") + msg + appid.encode("utf-8")
    pad = 32 - len(payload) % 32
    payload += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(RAW_KEY), modes.CBC(RAW_KEY[:16])).encryptor()
    return base64.b64encode(encryptor.update(payload) + encryptor.finalize()).decode()


def _raw_decrypt(blob_b64: str) -> bytes:
    decryptor = Cipher(algorithms.AES(RAW_KEY), modes.CBC(RAW_KEY[:16])).decryptor()
    padded = decryptor.update(base64.b64decode(blob_b64)) + decryptor.finalize()
    pad = padded[-1]
    assert 1 <= pad <= 32 and padded[-pad:] == bytes([pad]) * pad
    return padded[:-pad]


def test_decodes_an_independently_built_payload():
    crypto = WeChatCrypto(APPID, AES_KEY)
    ciphertext = _raw_encrypt("你好，帮我看看这个")
    assert crypto.decrypt(ciphertext) == "你好，帮我看看这个"


def test_encrypt_uses_the_documented_layout():
    crypto = WeChatCrypto(APPID, AES_KEY)
    message = "第一段\n\n第二段"
    plain = _raw_decrypt(crypto.encrypt(message))

    assert len(plain) >= 20
    length = int.from_bytes(plain[16:20], "big")
    assert plain[20:20 + length].decode("utf-8") == message
    assert plain[20 + length:] == APPID.encode("utf-8")


def test_round_trip_preserves_unicode_and_quotes():
    crypto = WeChatCrypto(APPID, AES_KEY)
    message = '他说：“今天不下雨。” \U0001f60a ]]> & <tag>'
    assert crypto.decrypt(crypto.encrypt(message)) == message


def test_appid_mismatch_is_rejected():
    crypto = WeChatCrypto("wx-another-app", AES_KEY)
    ciphertext = _raw_encrypt("hi", appid=APPID)

    with pytest.raises(WeChatCryptoError) as info:
        crypto.decrypt(ciphertext)
    # 防填充预言机：填充非法与 appid 不匹配共用同一句文案，断言只认这句
    assert "密文校验失败" in str(info.value)
    assert str(info.value) == "密文校验失败：EncodingAESKey 或回调机器人不匹配"


def test_bad_base64_and_bad_padding_are_rejected():
    crypto = WeChatCrypto(APPID, AES_KEY)

    with pytest.raises(WeChatCryptoError):
        crypto.decrypt("不是base64***")

    with pytest.raises(WeChatCryptoError):
        crypto.decrypt(base64.b64encode(b"too-short").decode())


def test_key_must_be_43_base64_chars():
    with pytest.raises(WeChatCryptoError) as too_short:
        decode_aes_key("abc")
    assert "43" in str(too_short.value)

    with pytest.raises(WeChatCryptoError):
        decode_aes_key("!" * 43)


def test_key_with_padding_already_present_is_accepted():
    # 用户在后台复制时可能连尾部的 '=' 一起粘进来。
    assert decode_aes_key(AES_KEY + "=") == RAW_KEY
