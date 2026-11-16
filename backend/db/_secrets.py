"""凭据信封（Windows DPAPI / 非 Windows keyring）— 从 database.py 同步的领域模块（P0 阶段 3）。

LLM api_key（models_config.api_key）与网络工具密钥（app_settings: web.tavily_api_key）
不再明文落库：

- Windows：经 DPAPI（CryptProtectData，当前用户作用域）加密后以 `dpapi:v1:<base64>`
  信封前缀存储 —— 库文件被拖走时拿到的是只有同一 Windows 用户才能解开的密文。
- 非 Windows：明文存系统凭据库（keyring，可选依赖），DB 行只存 `keyring:v1:<句柄>`
  句柄 —— 库文件里从头到尾不出现密钥明文。keyring 不可用（未安装 / 系统无凭据
  后端）时写入侧**显式抛错（fail-closed）**：静默降级成明文落盘会把「这条没加密」
  藏进库，保存接口应让这次保存失败并指引安装 keyring 后端。

读取侧（unprotect_secret）见到任一前缀就解密还原；无前缀按旧明文原样返回
（历史行兼容，该值下次经写入侧保存时自动升级为信封）。

已知限制：keyring 方案换密钥后，系统凭据库里的旧条目成为孤儿（句柄随机、
不再被引用，泄露面可忽略），不做主动清理。

原子性口径：本信封只作用于 SQLite 行值，落库走单条 INSERT/UPDATE 事务 ——
不引入任何设置文件写盘，CONTEXT.md「半写损坏」的 tmp+rename 由 DB 事务天然保证。
"""
import base64
import logging
import sys
import uuid

logger = logging.getLogger(__name__)

_SECRET_ENVELOPE_PREFIX = "dpapi:v1:"
_KEYRING_ENVELOPE_PREFIX = "keyring:v1:"
# keyring 条目命名：service 固定常量；username 带自描述版本前缀 + 随机句柄。
_KEYRING_SERVICE = "cKunAgentSSH.secrets"
_KEYRING_USERNAME_PREFIX = "kr1:"
# 加密平台开关（测试可改写以模拟非 Windows 路径）；仅 win32 启用 DPAPI。
_SECRET_PROTECTION_PLATFORM = "win32"

__all__ = [
    "protect_secret",
    "unprotect_secret",
    "SecretStorageError",
]


class SecretStorageError(OSError):
    """系统凭据库不可用或写入失败。

    继承 OSError：调用方（模型保存 / Tavily 保存）现有的 OSError 失败路径
    无需改动即可接住。语义是 fail-closed —— 本次保存显式失败，绝不静默
    降级成明文落库。
    """


# ── keyring 间接层（收进三个函数，测试 monkeypatch 这里即可模拟）──────────────

def _keyring_import():
    """惰性导入 keyring；缺失时抛 ImportError，由调用方转为 SecretStorageError。"""
    import keyring  # noqa: F401 — 仅探测可用性
    from keyring import errors as _keyring_errors
    return keyring, _keyring_errors


def _keyring_set(username: str, value: str) -> None:
    """明文写入系统凭据库；不可用/失败一律转 SecretStorageError（fail-closed）。"""
    try:
        keyring, errors = _keyring_import()
    except ImportError as e:
        raise SecretStorageError(
            "keyring 库未安装，已拒绝保存密钥；请安装 keyring 并配置系统凭据后端"
            "（如 Linux 的 SecretService / macOS 钥匙串）") from e
    try:
        keyring.set_password(_KEYRING_SERVICE, username, value)
    except errors.NoKeyringError as e:
        raise SecretStorageError(
            "系统凭据库不可用（未安装 keyring 后端），已拒绝保存密钥；"
            "请安装并配置系统凭据后端（如 Linux 的 SecretService / macOS 钥匙串）") from e
    except SecretStorageError:
        raise
    except Exception as e:
        raise SecretStorageError(f"系统凭据库写入失败: {e}") from e


def _keyring_get(username: str) -> str | None:
    """读取系统凭据库条目；不可用/失败返回 None（读取侧按「未配置」处理）。"""
    try:
        keyring, _ = _keyring_import()
        return keyring.get_password(_KEYRING_SERVICE, username)
    except Exception as e:
        logger.warning("keyring 凭据读取失败，按未配置处理: %s", e)
        return None


def _keyring_delete(username: str) -> None:
    """尽力删除系统凭据库条目（清除语义的辅助，失败仅留痕不阻断）。"""
    try:
        keyring, _ = _keyring_import()
        keyring.delete_password(_KEYRING_SERVICE, username)
    except Exception as e:
        logger.warning("keyring 条目删除失败（句柄 %s…，不影响清除语义）: %s", username[:8], e)


# ── Windows DPAPI（零第三方依赖，行为与改造前逐字一致）────────────────────────

def _dpapi_transform(data: bytes, protect: bool) -> bytes:
    """调 crypt32 的 DPAPI 做当前用户级加解密（仅 Windows）。

    CRYPTPROTECT_UI_FORBIDDEN(0x1)：后端是无人值守的服务进程，绝不允许
    DPAPI 弹 PIN/确认 UI 卡住请求。两个 ctypes 陷阱（实测 0xc0000374 堆损坏）：
    - pbData 字段必须是 c_void_p 而不是 c_char_p —— c_char_p 字段读出来的是
      「按 NUL 截断的副本」，binary 密文被截断不说，拿它调 LocalFree 等于
      释放一块非堆指针，堆当场损坏；
    - LocalFree 必须声明 argtypes=[c_void_p] —— 不声明时 Python int（64 位
      地址）按 c_int 传参，地址被截断成 32 位，同样释放到错误的指针上。
    """
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.c_void_p),
        ]

    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = crypt32.CryptProtectData.argtypes
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    blob_in = _DATA_BLOB(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError(
            f"DPAPI {'加密' if protect else '解密'}失败（WinError {ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_secret(value: str) -> str:
    """机密写入侧：明文 → 信封（win32=DPAPI 密文信封；非 win32=keyring 句柄信封）。

    空值原样返回（空串 = 用户清除密钥的语义，不包信封）；已是任一信封的值不
    二次包裹（幂等）。加密路径失败向上抛 OSError（win32）或 SecretStorageError
    （非 win32）：静默降级成明文落盘会把「这次没加密」藏进库，调用方（模型
    保存/Tavily 保存）应当让这次保存显式失败。
    """
    value = value or ""
    if (not value or value.startswith(_SECRET_ENVELOPE_PREFIX)
            or value.startswith(_KEYRING_ENVELOPE_PREFIX)):
        return value
    if sys.platform == _SECRET_PROTECTION_PLATFORM:
        blob = _dpapi_transform(value.encode("utf-8"), protect=True)
        return _SECRET_ENVELOPE_PREFIX + base64.b64encode(blob).decode("ascii")
    # 非 Windows：DB 行只存随机句柄，明文进系统凭据库（keyring 不可用即抛）
    handle = uuid.uuid4().hex
    _keyring_set(_KEYRING_USERNAME_PREFIX + handle, value)
    return _KEYRING_ENVELOPE_PREFIX + handle


def unprotect_secret(value: str) -> str:
    """机密读取侧：见信封前缀就解密还原明文；无前缀按旧明文原样返回。

    解密/读取失败（典型：库文件被拷到别的 Windows 用户/机器、或 keyring
    不可用/条目缺失）不抛也不回密文，返回空串并留 warning —— 把密文当 key
    发给上游只会得到费解的 401，报「未配置」则引导用户重填一次密钥（下次
    保存即按当前环境重新加封）。
    """
    if not value:
        return value
    if value.startswith(_KEYRING_ENVELOPE_PREFIX):
        username = _KEYRING_USERNAME_PREFIX + value[len(_KEYRING_ENVELOPE_PREFIX):]
        got = _keyring_get(username)
        if got is None:
            logger.warning(
                "keyring 凭据句柄读不出明文，按未配置处理（keyring 不可用或条目缺失）")
            return ""
        return got
    if not value.startswith(_SECRET_ENVELOPE_PREFIX):
        return value
    try:
        blob = base64.b64decode(value[len(_SECRET_ENVELOPE_PREFIX):], validate=True)
        return _dpapi_transform(blob, protect=False).decode("utf-8")
    except (ValueError, OSError) as e:
        # binascii.Error（base64 畸形，ValueError 子类）、UnicodeDecodeError、
        # DPAPI 的 OSError —— 除此之外的异常不属于"密文读不出"，照抛
        logger.warning("凭据信封解密失败，按未配置处理（库可能来自其他 Windows 用户/机器）: %s", e)
        return ""
