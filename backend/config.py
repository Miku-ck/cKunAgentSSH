"""
配置管理 — 从环境变量 / .env 读取
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def _default_db_path() -> str:
    """数据库默认路径：优先用 env，否则落在用户目录，避免相对 cwd 脆弱导致跨启动丢库"""
    env = os.getenv("DB_PATH")
    if env:
        return env
    # Electron 主进程会通过 env 显式传 DB_PATH（userData/db/ckunagentic.db）。
    # 兜底：未走 Electron（命令行直跑）时落到 ~/.ckunagentic/ckunagentic.db，跨 cwd 稳定。
    return str(Path.home() / ".ckunagentic" / "ckunagentic.db")


@dataclass
class Settings:
    """应用配置"""

    # 运行模式（默认 false：生产安全，避免忘记设置 DEV_MODE 导致调试接口暴露）
    dev_mode: bool = field(default_factory=lambda: os.getenv("DEV_MODE", "false").lower() == "true")

    # AI 模型配置（仅用于 _seed_models 初始种子，运行时模型配置存数据库）
    vllm_base_url: str = field(default_factory=lambda: os.getenv("VLLM_BASE_URL", "http://192.168.150.40:9030/v1"))
    vllm_api_key: str = field(default_factory=lambda: os.getenv("VLLM_API_KEY", "EMPTY"))

    # Thinking 模型配置
    vllm_thinking_url: str = field(default_factory=lambda: os.getenv("VLLM_THINKING_URL", "http://192.168.150.40:9031/v1"))

    # 数据库 — 绝对路径，跨 cwd / 跨重装稳定
    db_path: str = field(default_factory=_default_db_path)


settings = Settings()
