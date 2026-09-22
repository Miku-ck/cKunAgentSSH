"""
配置管理 — 从环境变量 / .env 读取
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """最小 .env 加载（无第三方依赖）。

    查找顺序：backend/.env（与本文件同目录）→ cwd/.env，取第一个存在的。
    仅支持简单 KEY=VALUE 行（忽略空行/注释）；已存在的进程环境变量优先，
    .env 不覆盖。解析失败静默——.env 是便利设施，不能阻塞启动。
    """
    candidates = [Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception:
            logger.warning("读取 .env 失败（已忽略）: %s", path)
        break  # 按优先级取第一个命中的文件


_load_dotenv()


# 应用级共享路径常量（此前 reins.py 两处字面量复制，收敛单一源）
ALLOWLIST_PATH = os.path.expanduser("~/.ckunagentic/allowlist.txt")


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

    # AI 模型配置（仅用于首启 _seed_models 自动探测，运行时模型配置存数据库）
    # 默认空 = 不种子任何模型（引导用户在设置 UI 配置）。
    # 开发者/演示环境显式设置 VLLM_BASE_URL（或写 backend/.env）即可保留
    # 首启自动探测 /models 并种子的开箱行为。
    vllm_base_url: str = field(default_factory=lambda: os.getenv("VLLM_BASE_URL", ""))
    vllm_api_key: str = field(default_factory=lambda: os.getenv("VLLM_API_KEY", "EMPTY"))

    # Thinking 模型配置（可选；设置后首启同样探测并种子为思考模型）
    vllm_thinking_url: str = field(default_factory=lambda: os.getenv("VLLM_THINKING_URL", ""))

    # 数据库 — 绝对路径，跨 cwd / 跨重装稳定
    db_path: str = field(default_factory=_default_db_path)

    # 后端版本 — /api/health 与诊断面板（G10）共用同一个源，别再各处抄 "0.1.0" 字面量
    version: str = field(default_factory=lambda: os.getenv("CKUN_APP_VERSION", "0.1.0"))


settings = Settings()
