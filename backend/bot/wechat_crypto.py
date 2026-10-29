"""微信对话开放平台的报文加解密（AES-256-CBC + PKCS#7/32）。

密文 = base64( AES[random(16B) + msg_len(4B, 大端) + msg + appid] )。
EncodingAESKey 是 43 位 base64，补一个 "=" 解出 32 字节；IV 取前 16 字节。
平台文档明确入站不做签名校验，所以「能解开 + appid 对得上」就是鉴权。
"""

import base64
import binascii
import os

AES_KEY_CHARS = 43
KEY_BYTES = 32
BLOCK_SIZE = 32


class WeChatCryptoError(Exception):
    """密钥/密文不合法。调用方一律转成用户能看懂的配置错误。"""


def decode_aes_key(encoding_aes_key: str) -> bytes:
    key = (encoding_aes_key or "").strip()
    # 后台复制时可能连尾部的 "=" 一起粘进来。
    if len(key) == AES_KEY_CHARS + 1 and key.endswith("="):
        key = key[:-1]
    if len(key) != AES_KEY_CHARS:
        raise WeChatCryptoError(
            f"EncodingAESKey 必须是 {AES_KEY_CHARS} 位（当前 {len(key)} 位），请到开放API 页重新复制"
        )
    try:
        raw = base64.b64decode(key + "=", validate=True)
    except (binascii.Error, ValueError):
        try:
            # base64.urlsafe_b64decode 会忽略非法字符，长度检查兜住乱码。
            raw = base64.urlsafe_b64decode(key + "=")
        except (binascii.Error, ValueError) as exc:
            raise WeChatCryptoError("EncodingAESKey 含有非法字符，请重新复制") from exc
    if len(raw) != KEY_BYTES:
        raise WeChatCryptoError("EncodingAESKey 解出的密钥长度不正确，请重新复制")
    return raw


def _aes():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # 裸 ModuleNotFoundError 在界面上没法行动
        raise WeChatCryptoError(
            "后端缺少微信渠道依赖 cryptography，请在运行后端的 Python 中执行 "
            "pip install cryptography"
        ) from exc
    return Cipher, algorithms, modes


class WeChatCrypto:
    def __init__(self, app_id: str, encoding_aes_key: str):
        self.app_id = (app_id or "").strip()
        if not self.app_id:
            raise WeChatCryptoError("APPID 不能为空")
        self._key = decode_aes_key(encoding_aes_key)
        self._iv = self._key[:16]
        self._Cipher, self._algorithms, self._modes = _aes()

    def _cipher(self):
        return self._Cipher(
            self._algorithms.AES(self._key),
            self._modes.CBC(self._iv),
        )

    def encrypt(self, plaintext: str) -> str:
        msg = (plaintext or "").encode("utf-8")
        payload = os.urandom(16) + len(msg).to_bytes(4, "big") + msg + self.app_id.encode(
            "utf-8"
        )
        pad = BLOCK_SIZE - len(payload) % BLOCK_SIZE
        payload += bytes([pad]) * pad
        encryptor = self._cipher().encryptor()
        encrypted = encryptor.update(payload) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        raw = (encrypted or "").strip()
        if not raw:
            raise WeChatCryptoError("密文为空")
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WeChatCryptoError("密文不是合法的 base64") from exc
        if not blob or len(blob) % 16:
            raise WeChatCryptoError("密文长度不是 AES 分组长度")
        decryptor = self._cipher().decryptor()
        try:
            padded = decryptor.update(blob) + decryptor.finalize()
        except ValueError as exc:
            raise WeChatCryptoError("密文解密失败（EncodingAESKey 与后台不一致）") from exc
        if not padded:
            raise WeChatCryptoError("密文解密结果为空")

        pad = padded[-1]
        if pad < 1 or pad > BLOCK_SIZE or padded[-pad:] != bytes([pad]) * pad:
            # 与 appid 不匹配共用同一句文案：AES-CBC 无 MAC，区分「填充非法」和
            # 「appid 不对」等于给公开回调端点开填充预言机，错误细节只留日志。
            raise WeChatCryptoError("密文校验失败：EncodingAESKey 或回调机器人不匹配")
        plain = padded[:-pad]
        if len(plain) < 20:
            raise WeChatCryptoError("密文内容过短")
        length = int.from_bytes(plain[16:20], "big")
        if 20 + length > len(plain):
            raise WeChatCryptoError("密文校验失败：EncodingAESKey 或回调机器人不匹配")

        appid = plain[20 + length:].decode("utf-8", "replace")
        if appid != self.app_id:
            raise WeChatCryptoError("密文校验失败：EncodingAESKey 或回调机器人不匹配")
        try:
            return plain[20:20 + length].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeChatCryptoError("密文内容不是合法 UTF-8") from exc
