"""
cKunAgentic Python 后端入口
启动: python main.py --port 7777
"""
import time

#: 进程内自报耗时的原点：本模块开始执行（≈解释器 bootstrap 完成）。必须早于其余
#: import —— 否则「导入花了多少」无从自报。ruff 的 E402 已在 ruff.toml 对 main.py
#: 放过，与该文件既有的惰性导入范式同一取舍。
_MODULE_T0 = time.perf_counter()

import argparse
import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import logconfig
import netconfig
from config import settings
import database as db
from routes.chat import router as chat_router
from routes.bot import router as bot_router
from routes.bot_channels import router as bot_channels_router
from routes.bot_stickers import router as bot_stickers_router
from routes.wechat_callback import router as wechat_callback_router
from routes.settings import router as settings_router
from routes.skills import router as skills_router
from routes.agent import router as agent_router
from routes.terminal import router as terminal_router
from routes.audit import router as audit_router
from routes.schedules import router as schedules_router
from routes.workflow import router as workflow_router
from routes.rollbacks import router as rollbacks_router
from routes.runbooks import router as runbooks_router
from agent.audit import AuditWriteError
from agent.bridge_relay import BridgeRelay, bridge_ws_endpoint, set_bridge_relay
import win_asyncio_teardown

# ── 启动耗时打点 ─────────────────────────────────────────────────────────────
# 两个基准，回答两个不同的问题：
#   · _MODULE_T0（见文件首行）= 本模块开始执行，≈ 解释器 bootstrap 完成，与 Electron
#     主进程 `backend: spawned pid=` 同一量级 —— 所有明细行以它为原点；
#   · _STARTUP_T0 = 依赖 import 完成 —— 「import 之后 → 健康就绪」这段的基线，也
#     正是本文件真正能动的那一段（init_db / 配置回填 / 技能同步 / 模型探测 /
#     渠道恢复）。导入成本本身用 `python -X importtime -c "import main"` 观测，
#     导入图不倒退由 tests/test_startup_critical_path.py 守卫。
_STARTUP_T0 = time.perf_counter()

logger = logging.getLogger(__name__)

_PERF_VERBOSE = os.environ.get("CKUN_PERF", "") == "1"

#: (label, 相对 _MODULE_T0 的毫秒)。汇总行一次性渲染 —— 并发节点各有各的完成
#: 时刻，让它们各自打一行既刷屏也拼不出全貌。
_marks: list[tuple[str, float]] = []


def _perf_mark(label: str) -> None:
    """记录一个启动节点；CKUN_PERF=1 时附带输出明细行。

    明细必须是 WARNING：lifespan 里的 logconfig.load_from_db 会把 root 级别回填成
    用户配置（默认 warning），INFO 明细会被自己吃掉（实测 batch1/batch2/就绪三条全丢）。
    常驻信息只留 _log_startup_summary 那一行。
    """
    at = (time.perf_counter() - _MODULE_T0) * 1000
    _marks.append((label, at))
    if _PERF_VERBOSE:
        logger.warning("[perf] %s @%.0fms", label, at)


def _log_startup_summary() -> None:
    """就绪时输出一行常驻汇总（WARNING：此刻 root 级别已被用户配置接管）。

    形制与 Electron 侧 electron/utils/perf-mark.ts 的 `[perf] <name> @<N>ms` 对齐；
    imports= 与主进程 `backend: spawned pid=` 同一物理起点量级，所以「后端自报
    total」与「主进程 backend: health ready」可以直接对表。

    原实现用 logger.info 打就绪行，而它跑在 logconfig.load_from_db 把 root 降到
    warning 之后 —— 默认配置下那行永远是死日志，后端对自己的就绪耗时零输出。
    """
    at = dict(_marks)
    now = (time.perf_counter() - _MODULE_T0) * 1000

    def delta(start: float | None, end: float | None) -> float | None:
        if start is None or end is None:
            return None
        return end - start

    def ms(value: float | None) -> str:
        return "?" if value is None else f"{value:.0f}ms"

    imports_done = (_STARTUP_T0 - _MODULE_T0) * 1000
    entered = at.get("lifespan entered")
    logger.warning(
        "[perf] startup imports=%s assemble=%s boot=%s lifespan=%s total=%s"
        " | init_db=%s round1=%s background=%s",
        ms(imports_done),
        ms(delta(imports_done, at.get("app assembled"))),
        ms(delta(at.get("serve start"), entered)),
        ms(delta(entered, at.get("ready"))),
        ms(now),
        ms(delta(entered, at.get("init_db"))),
        ms(delta(at.get("init_db"), at.get("round1"))),
        ms(delta(at.get("round1"), at.get("background started"))),
    )


# ── S-B1①：入口最早安装 root handler ─────────────────────────────────────────
# 必须先于下方模块级 token 警告与 __main__ 里的启动横幅执行：此前前者经
# lastResort 裸打（无时间戳无级别）、后者是 root 默认 WARNING 丢 INFO 的死日志。
# 安装后二者按统一 `%(asctime)s %(levelname)-7s %(name)s` 格式落地，且出口
# 脱敏（SecretRedactionFilter）随 handler 同步生效 —— 早期日志没有脱敏真空期。
# 启动窗口用 info 级（横幅与初始化进度必须落地）；用户配置级别由 lifespan
# 的 logconfig.load_from_db 回填接管，运行期默认仍是 warning。
logconfig.install_early()

# ── API Token 认证 ────────────────────────────────────────────────────────────
# 契约：Electron spawn 时注入环境变量 CKUN_API_TOKEN；
# 客户端请求需携带 HTTP 头 X-API-Token。
# 开发模式（未注入）下生成临时 token 并打印前 8 位（不打印完整 token）。
API_TOKEN = os.environ.get("CKUN_API_TOKEN", "")
if not API_TOKEN:
    API_TOKEN = secrets.token_hex(32)
    logger.warning(
        "未收到 CKUN_API_TOKEN，已生成临时 token（开发模式），token 前 8 位: %s...",
        API_TOKEN[:8],
    )


async def _restore_channels_quietly() -> None:
    """后台恢复机器人渠道（QQ 登录等纯网络动作）。

    该动作不进健康就绪关键路径（实测 172ms，网络差时更久），失败也不影响
    Agent/终端主链路，只警告。
    """
    from bot.channel_runtime import get_channel_runtime

    try:
        await get_channel_runtime().restore()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("恢复 cKunBot 机器人渠道失败: %s", exc)


# ── 启动初始化步骤 ───────────────────────────────────────────────────────────
# 全部抽成模块级函数：lifespan 只负责分派、打点与关停；测试可 monkeypatch 单步
# （tests/test_startup_timing.py），不必为了验时序去搭真库真渠道。
# 模块导入一律下沉到各自函数内部：builtin_skills / post_processor / netconfig 首次
# 导入因此发生在 worker 线程里，与 init_db 及其他任务重叠，而不是在 lifespan 开头
# 排队付出（实测 post_processor 2.3ms、builtin_skills ~2ms 白花在关键路径上）。


def _import_permission_modules():
    """预导入审批策略模块图（纯 CPU，实测 ~22ms），返回三个设置函数。

    它不依赖数据库，所以在 init_db 之前就先丢进线程池：sqlite 的 I/O 会释放 GIL，
    这段纯 Python 导入能与之重叠，把 22ms 从关键路径上藏掉。返回值必须由
    _apply_permission_policy 在任何 PermissionGate 单例创建前写入模块值。
    """
    from agent.harness.reins.auto_approve import set_db_enabled
    from agent.harness.reins.core import set_approval_fallback_seconds
    from agent.harness.reins.gate import set_allowlist_ttl_hours
    return set_allowlist_ttl_hours, set_db_enabled, set_approval_fallback_seconds


def _apply_permission_policy(setters) -> None:
    """E3 + 审批策略两新字段回填（DB 读 + 写模块值）。"""
    set_allowlist_ttl_hours, set_db_enabled, set_approval_fallback_seconds = setters
    # allowlist 保留时长必须在任何 PermissionGate 单例创建前写入模块值，否则首次
    # 加载 allowlist 时按 TTL=0 判定，过期条目要等到下次重启才不会被撤销。
    # 另两字段同段回填：auto_approve 的 DB 开关（env kill switch 在调用处仍优先）；
    # 兜底时长只在 DB 有显式行时才推 —— 从未在设置面板改过的用户，
    # CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S 的 env 覆盖保持有效。
    policy = db.get_permission_policy()
    set_allowlist_ttl_hours(policy["allowlist_ttl_hours"])
    set_db_enabled(policy["auto_approve_enabled"])
    # 「用户写过」要用带区间的判定，不是「这一行存在」：手工改成 "abc" 或越界
    # 值的脏行，get_permission_policy 会回默认 600 读数，若仍按"写过"推给运行时，
    # 就把运维的 CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S 永久锁掉了 —— 与
    # netconfig 的 retry/timeout env 兜底同一教训（database.get_http_policy 的
    # *_set 就是为此而存在）。
    fallback_set = db.is_int_app_setting_set(
        "permission.approval_fallback_s", 600, low=0, high=86400)[1]
    if fallback_set:
        set_approval_fallback_seconds(policy["approval_fallback_s"])


async def _load_net_policy(net_ready: asyncio.Event) -> None:
    """出站 HTTP 策略回填；无论成败都放行 _seed_models（读不到走直连默认）。"""
    try:
        await asyncio.to_thread(netconfig.load_policy_from_db)
    finally:
        net_ready.set()


async def _seed_models(net_ready: asyncio.Event) -> None:
    """首启模型探测：只等 netconfig（探测要走出站策略），不等整轮。"""
    await net_ready.wait()
    await db.seed_models_async(settings.db_path)


def _sync_skills() -> None:
    import builtin_skills
    builtin_skills.sync_packaged_skills()


def _load_post_config() -> None:
    from post_processor import (
        init_post_process_manager,
        load_post_process_config_from_db,
    )
    init_post_process_manager(load_post_process_config_from_db())


def _reset_stale() -> None:
    n = db.reset_stale_in_progress_conversations()
    if n:
        logger.info("[cKunAgentic] Reset %s stale in_progress conversation(s)", n)


#: 与 lifespan 里 gather 的顺序一一对应，只用于把异常日志说到具体哪一步。
_ROUND1_STEPS = (
    "日志级别回填", "出站 HTTP 策略", "内置技能同步", "后处理配置",
    "审批策略回填", "崩溃会话复位", "首启模型探测",
)


class _Background:
    """就绪后常驻的协作组件句柄：start/stop 共用同一份，避免两处各记一遍。"""

    __slots__ = ("proactive", "restore_task", "schedule_engine", "tick")

    def __init__(self, tick, schedule_engine, proactive, restore_task):
        self.tick = tick
        self.schedule_engine = schedule_engine
        self.proactive = proactive
        self.restore_task = restore_task


def _start_background_tasks() -> _Background:
    """启动就绪后才需要的后台组件（均不在健康就绪的关键路径上）。"""
    # 意识 tick 延迟启动：仅首次 Agent 运行时触发（ensure_started），
    # 避免纯聊天用户产生空 DB 查询开销。import 推迟到此处——该路径在健康
    # 就绪之后才运行，不应在 `import main` 阶段被装配进模块级导入树。
    from agent.consciousness_tick import get_global_tick
    tick = get_global_tick()

    # 定时任务调度引擎：懒启动，仅在存在定时任务时 begin（ensure_started）
    from agent.schedule_engine import get_schedule_engine
    schedule_engine = get_schedule_engine()
    schedule_engine.ensure_started()

    # 陪聊主动性调度（P3）：60s 轮询，无人格开启时 tick 是一次空查询，
    # 开关在人格上（proactive_enabled，默认关），这里常驻无副作用。
    from bot.proactive import get_scheduler
    proactive = get_scheduler()
    proactive.ensure_started()

    # Bridge WS relay（阶段 1）：Electron 主进程反向连接 /ws/bridge 的命令通道。
    # 创建即注册到模块全局（bridge_ws_endpoint 经 get_bridge_relay() 获取）。
    set_bridge_relay(BridgeRelay())

    # 机器人渠道恢复（QQ 登录等纯网络动作）移出健康就绪关键路径：实测 172ms，
    # 网络差时更久，且失败不影响 Agent/终端主链路。后台任务 + 退出前取消等待，
    # 保证 stop_all 不会与仍在进行的 restore 交叉。
    restore_task = asyncio.create_task(_restore_channels_quietly())
    return _Background(tick, schedule_engine, proactive, restore_task)


async def _stop_background_tasks(bg: _Background) -> None:
    """关停顺序：先掐渠道恢复（防与 stop_all 交叉）→ 停渠道 → 并发停三个
    后台循环 → 注销 relay → 等后处理把在途任务写完。"""
    if not bg.restore_task.done():
        bg.restore_task.cancel()
        with suppress(asyncio.CancelledError):
            await bg.restore_task

    from bot.channel_runtime import get_channel_runtime
    await get_channel_runtime().stop_all()

    # 三个循环互不依赖：并发停止；单个失败不拖住其余（失败即泄漏，不许静默）
    for result in await asyncio.gather(
        bg.tick.stop(), bg.schedule_engine.stop(), bg.proactive.stop(),
        return_exceptions=True,
    ):
        if isinstance(result, Exception):
            # 后台循环关停失败是真实错误（资源泄漏可能），需 error 级 + 完整 trace，
            # 不能用 warning/%s 吞掉堆栈——否则泄漏现场无法复盘。
            logger.error("[cKunAgentic] 后台循环停止异常", exc_info=result)

    # 注销 Bridge WS relay（优雅关闭时新连接将收到 1013）
    set_bridge_relay(None)

    # 等待 PostProcessManager 任务完成（最多 10 秒），避免数据库写入被截断
    from post_processor import get_post_process_manager
    await get_post_process_manager().shutdown(timeout=10.0)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """初始化数据库 + 启动后台组件（耗时节点见 _log_startup_summary）"""
    _perf_mark("lifespan entered")

    # 审批策略模块图不依赖 DB：先丢线程池，与 init_db 的 sqlite I/O 重叠
    permission_modules = asyncio.create_task(asyncio.to_thread(_import_permission_modules))

    db.init_db(settings.db_path)
    _perf_mark("init_db")
    # 心跳型稳态信息：DB 初始化极少失败，路径+耗时已由 _log_startup_summary
    # 统一输出；此处留 debug 便于需要时复现时序，不占 info 通道。
    logger.debug("[cKunAgentic] Database initialized: %s", settings.db_path)

    net_ready = asyncio.Event()

    async def _apply_policy() -> None:
        await asyncio.to_thread(_apply_permission_policy, await permission_modules)

    # 七个步骤互不依赖（首启模型探测只等 netconfig，用事件而非整轮），一次分派：
    # 原实现把「模型探测 / 会话复位 / 后处理 / 审批策略」排在「log/net 配置 + 技能
    # 同步」之后白等一轮（实测 9ms），而其中审批策略那 21ms 的模块导入根本不依赖
    # 上一批，却压在最后一轮才付。
    results = await asyncio.gather(
        asyncio.to_thread(logconfig.load_from_db),
        _load_net_policy(net_ready),
        asyncio.to_thread(_sync_skills),
        asyncio.to_thread(_load_post_config),
        _apply_policy(),
        asyncio.to_thread(_reset_stale),
        _seed_models(net_ready),
        return_exceptions=True,
    )
    for step, result in zip(_ROUND1_STEPS, results):
        if isinstance(result, Exception):
            # 任一启动步骤抛出都是真实错误（后续依赖它的功能会静默降级），
            # error + exc_info 保留堆栈；warning/%s 会把故障吃掉只剩一行文本。
            logger.error("[cKunAgentic] 启动初始化（%s）异常", step, exc_info=result)
    _perf_mark("round1")

    background = _start_background_tasks()
    _perf_mark("background started")
    _perf_mark("ready")
    _log_startup_summary()

    # try/finally 而不是裸 yield 后收尾：应用体抛异常时生成器在 yield 处被 throw，
    # 裸收尾不会执行 —— 后台循环与渠道就成了孤儿。
    try:
        yield
    finally:
        await _stop_background_tasks(background)


app = FastAPI(
    title="cKunAgentic Backend",
    version=settings.version,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# CORS — 显式来源白名单。调用方来源集合只有两种形态：
#   dev 态：vite dev server（electron/main.ts loadURL(ELECTRON_RENDERER_URL)），
#           Origin = http://localhost:5555 / http://127.0.0.1:5555，浏览器经局域网
#           IP 访问时为 http://<私有IP>:5555；
#   打包态：loadFile(...) 的 file:// renderer，其跨源请求 Origin 头为字面量 "null"，
#           必须显式列入否则 Chromium 拦响应致前端显示"连接失败"。
# （旧正则里的 ^app://\.$ 是未落地的自定义协议计划残留 —— 全仓没有任何
#   protocol.register/handle 注册 app://，去掉以免未来真出现 app:// 伪装来源时被放行。）
app.add_middleware(
    CORSMiddleware,
    # 仅放行 dev server 来源：localhost/127.0.0.1 + 局域网私有 IP（10./172.16-31./192.168.）
    # 不用 `.*` 通配，避免任意公网主机:5555 被误放行
    allow_origin_regex=(
        r"^https?://(localhost|127\.0\.0\.1|"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"):5555$|^null$"
    ),
    # 凭据恒不放开：本后端没有任何 Cookie 会话（鉴权走每次请求显式带的
    # X-API-Token 头，见下方 api_token_auth），前端 fetch 也从不带 credentials；
    # 放开等于给沙箱 iframe 的 Origin: null 来源（同样命中上面的 ^null$）回显
    # Access-Control-Allow-Credentials，纯增攻击面。
    allow_credentials=False,
    # PUT 必须在列：QQ 接入的 PUT /bot/qq/config 与各 /settings PUT 都靠它。曾漏配
    # PUT → 预检被 Starlette 判为 Disallowed CORS method 回 400，浏览器拦截实请求，
    # fetch 抛 Failed to fetch → 前端兜底成误导性的「网络连接失败，请检查服务是否运行」。
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # 审查外热修：渲染进程直连请求都带 X-API-Token 头（client.ts withApiToken），
    # 必须列入允许头 — Starlette ≥0.41 的 CORSMiddleware 对预检中出现未允许头的
    # OPTIONS 直接回 400 "Disallowed CORS headers"（旧版会放行），导致浏览器
    # 预检失败、所有跨源请求被拦，日志里 OPTIONS 400 刷屏即此因
    allow_headers=["Content-Type", "Authorization", "Accept", "X-API-Token"],
)


# ── API Token 认证中间件 ─────────────────────────────────────────────────────
# 受保护范围：所有 /api/* 路由（含 SSE：POST /api/agent/run）
# 例外：GET /api/health 不认证；OPTIONS（CORS 预检）放行
# 校验：X-API-Token 头与 API_TOKEN 恒定时间比较（hmac.compare_digest）
# 注意：本中间件经 @app.middleware 注册后被置于中间件栈最外层（先于 CORS 执行），
#       未授权请求直接返回 401，不经 CORS 处理——对未授权请求这是期望行为。
@app.middleware("http")
async def api_token_auth(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    # 唯一免鉴权端点：GET /api/health（/health 从未注册过，别在这里加第二条）
    if request.method == "GET" and path == "/api/health":
        return await call_next(request)
    token = request.headers.get("X-API-Token", "")
    # 审查 MINOR 修复：compare_digest 的 str 版本要求纯 ASCII，畸形/非 ASCII 头
    # 原实现抛 TypeError → 500；两侧编码为 bytes 后恒定时间比较恒安全
    if not hmac.compare_digest(token.encode("utf-8"), API_TOKEN.encode("utf-8")):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.get("/api/health")
async def health():
    """健康检查 — BackendManager 轮询此端点（仅判 200）。

    唯一免 token 端点，不回显版本号：本机其他进程探测到开放端口后可借此
    指纹识别应用；版本细节由需认证的 /settings/diagnostics 提供。"""
    return {"status": "ok"}


# 审计链不可写 → 关键决策（授权生效）必须失败。原实现里 append_record 静默吞掉
# 写失败，授权照样落地：事后取证看到一条没有「谁批准了什么」的干净链。
# 统一转 503 而不是 500 —— 这是依赖面（磁盘/锁）不可用，不是请求写错了。
@app.exception_handler(AuditWriteError)
async def _audit_write_error_handler(request: Request, exc: AuditWriteError):
    logger.error("关键审计写入失败，已拒绝该动作: %s %s err=%s",
                 request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "审计链不可写，该操作已拒绝生效（请检查磁盘与数据库锁）"},
    )


# 注册路由
app.include_router(chat_router, prefix="/api")
app.include_router(bot_router, prefix="/api")
app.include_router(bot_channels_router, prefix="/api")
# 表情包素材：不挂 /api —— <img> 与微信平台都带不了 X-API-Token；只暴露内置 png
app.include_router(bot_stickers_router)
# 微信平台回调：不挂 /api —— 平台不会带 X-API-Token，鉴权由 AES 密钥 + appid 承担
app.include_router(wechat_callback_router)
app.include_router(settings_router, prefix="/api")
app.include_router(skills_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(terminal_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")
app.include_router(workflow_router, prefix="/api")
app.include_router(rollbacks_router, prefix="/api")
app.include_router(runbooks_router, prefix="/api")

# Bridge WS 端点：刻意不挂 /api 前缀 — X-API-Token 中间件不覆盖，
# 它有自己的 hello token 鉴权（与主进程注入后端的 ELECTRON_BRIDGE_TOKEN 比对）
app.add_api_websocket_route("/ws/bridge", bridge_ws_endpoint)

# 装配到此结束（建 app / 中间件 / 异常处理器 / 全部路由）：与 imports、uvicorn 引导
# 共同构成 _log_startup_summary 里的三段前缀。
_perf_mark("app assembled")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="cKunAgentic Backend")
    parser.add_argument("--port", type=int, default=7777, help="监听端口")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    logger.debug("[cKunAgentic] Starting backend on %s:%s", args.host, args.port)
    # 使用 asyncio.run(Server.serve()) 代替 uvicorn.run()，
    # 避免 PowerShell 后台运行时 WatchFiles reloader 启动缓慢导致健康检查超时

    # 必须早于任何 transport 创建（win32 之外自动跳过）。新增其他入口（如
    # `uvicorn main:app`）时同样要在 asyncio.run 之前调用，否则 10054 噪音与
    # 「孤儿后端」会回来（见模块 docstring）。
    win_asyncio_teardown.install()
    config = uvicorn.Config(
        # 传 app 对象而不是 "main:app" 字符串：本文件是以 __main__ 运行的，字符串入口
        # 会让 uvicorn 再 import 一次 main 模块（实测 +36ms，并多建一份 FastAPI app、
        # 多装一次 root log handler、多生成一个临时 token），而实际服务的却是第二份。
        app,
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
        # S-B1②：关访问日志 —— BackendManager 以 150-500ms 轮询 /api/health，
        # 每次轮询一条 access 行把启动日志刷成健康检查流水账（与 Electron 侧
        # [Python] 行缓冲 relay 配套的统一口径）。业务可观测性走 root handler，
        # uvicorn.error（启动/停止事件）仍保留在 info。
        access_log=False,
        # Bridge WS 单帧上限（显式化默认值）：远程命令输出可能较大
        ws_max_size=16 * 1024 * 1024,
    )
    server = uvicorn.Server(config)
    # serve start → lifespan entered 就是 uvicorn 引导段：config.load 导入
    # loop/protocol/lifespan 实现（实测 config.load 单独 ~28ms）+ 事件循环创建。
    _perf_mark("serve start")
    asyncio.run(server.serve())
