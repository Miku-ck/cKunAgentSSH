"""
cKunAgentic Python 后端入口
启动: python main.py --port 8010
"""
import argparse
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import init_db
from routes.chat import router as chat_router
from routes.models import router as models_router
from routes.settings import router as settings_router
from routes.skills import router as skills_router
from routes.agent import router as agent_router
from agent.consciousness_tick import get_global_tick


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """初始化数据库 + 启动后台意识 tick"""
    init_db(settings.db_path)
    print(f"[cKunAgentic] Database initialized: {settings.db_path}")

    # 意识 tick 延迟启动：仅首次 Agent 运行时触发（ensure_started），
    # 避免纯聊天用户产生空 DB 查询开销
    tick = get_global_tick()

    yield

    # 优雅停止后台 tick（若曾启动）
    await tick.stop()

    # 等待 PostProcessManager 任务完成（最多 10 秒），避免数据库写入被截断
    from post_processor import get_post_process_manager
    await get_post_process_manager().shutdown(timeout=10.0)


app = FastAPI(
    title="cKunAgentic Backend",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# CORS — 允许 Electron 渲染进程来源 + 任意主机:5173（dev server 浏览器访问，涵盖局域网 IP）
# file:// renderer 的 Origin 头为 "null"，必须显式列入否则 Chromium 拦响应致前端显示"连接失败"
# 浏览器从任意主机:5173 进入时携带不同 Origin，统一用正则匹配放行（仅 dev 环境，生产用 app://）
app.add_middleware(
    CORSMiddleware,
    # 仅放行 dev server 来源：localhost/127.0.0.1 + 局域网私有 IP（10./172.16-31./192.168.）
    # 不用 `.*` 通配，避免任意公网主机:5173 被误放行
    allow_origin_regex=(
        r"^https?://(localhost|127\.0\.0\.1|"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"):5173$|^app://\.$|^null$"
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)


@app.get("/api/health")
async def health():
    """健康检查 — BackendManager 轮询此端点"""
    return {
        "status": "ok",
        "version": app.version,
    }


# 注册路由
app.include_router(chat_router, prefix="/api")
app.include_router(models_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(skills_router, prefix="/api")
app.include_router(agent_router, prefix="/api")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="cKunAgentic Backend")
    parser.add_argument("--port", type=int, default=8010, help="监听端口")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    print(f"[cKunAgentic] Starting backend on {args.host}:{args.port}")
    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=settings.dev_mode,
        log_level="info",
    )
