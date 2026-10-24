"""
模型与设置路由 — 模型配置 CRUD + 连接测试
"""
import asyncio
import logging
import os
import sys
from typing import Optional, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import database as db
import netconfig
from prompts import SSH_MODE


logger = logging.getLogger("cKunAgentic.settings")

router = APIRouter(tags=["settings"])


# ── Request Models ──

class CreateModelRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1)
    api_key: str = ""
    model_id: str = Field("", max_length=200)
    provider: str = "custom"
    supports_thinking: bool = False
    supports_image: bool = False
    supports_tool_calling: bool = False
    temperature: float = Field(0.7, ge=0, le=2)
    # max_tokens = 上下文窗口（与 models_config 列默认及 create_model_config 一致）；
    # 输出预算由 max_output_tokens 单独控制，不要在这里压低窗口。
    max_tokens: int = Field(16384, ge=1, le=1000000)
    max_output_tokens: int = Field(4096, ge=1, le=1000000)
    # 推理/思考内容的独立 token 预算（Qwen3 等模型支持 enable_thinking 时使用）
    reasoning_tokens: int = Field(8192, ge=0, le=1000000, description="推理内容最大 token 数，0 表示使用 max_output_tokens")
    top_p: float = Field(0.9, ge=0, le=1)
    is_default_ssh: bool = False


class UpdateModelRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    base_url: Optional[str] = Field(None, min_length=1)
    api_key: Optional[str] = None
    model_id: Optional[str] = Field(None, max_length=200)
    provider: Optional[str] = None
    supports_thinking: Optional[bool] = None
    supports_image: Optional[bool] = None
    supports_tool_calling: Optional[bool] = None
    temperature: Optional[float] = Field(None, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, ge=1, le=1000000)
    max_output_tokens: Optional[int] = Field(None, ge=1, le=1000000)
    reasoning_tokens: Optional[int] = Field(None, ge=0, le=1000000, description="推理内容最大 token 数")
    top_p: Optional[float] = Field(None, ge=0, le=1)
    sort_order: Optional[int] = None
    is_default_ssh: Optional[bool] = None


# ── 模型配置 CRUD ──

# 默认档位：ssh = SSH_MODE（对话/终端/定时任务用的档），global = is_default 全局档。
# 全局档不是历史包袱：post_processor、terminal/agent 的兜底链在"SSH 档没设"时读它，
# 但设置页此前只能写 SSH 档 —— 用户看得到回落却调不到它。现在两档都可设。
DefaultMode = Literal["ssh", "global"]


# 注意：/settings/models/default 必须注册在 /settings/models/{config_id} 之前，
# 否则 FastAPI 按注册顺序匹配时 "default" 会被 {config_id} 路径先捕获导致功能失效。
@router.patch("/settings/models/default")
def set_default_model(
    model_id: str = Query(..., description="要设为默认的模型：行主键 id 或 LLM 模型名皆可"),
    mode: DefaultMode = Query(SSH_MODE, description="默认档位：ssh（默认）或 global"),
):
    """设置默认模型。不传 mode = SSH 档（与 round 18 的前端控件同口径）。"""
    # mode 直传：_DEFAULT_COLUMNS 本身就认 "global"/"ssh" 两个取值，
    # 原来在这里把 "global" 折成 None 再进表是多一跳的翻译，两张映射表得同步维护。
    try:
        db.set_default_model(model_id, mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    model = db.get_model_config(model_id)
    return {"default": (model or {}).get("id") or model_id, "mode": mode}


@router.get("/settings/models/default")
def get_default_model():
    """获取默认模型 —— 档位顺序必须与运行时的兜底链一致：ssh → global，
    再谈"一个都没标"的兜底。原实现只有 ssh 一级就直接落到 models[0]，于是
    只标了全局档（⊕）时这条端点报的是列表第一行，而 routes/agent.py、terminal、
    schedule_engine 真正会用全局档那一行 —— 前端据它预选模型，选出来的和
    「★ 是亮在谁身上」不一致，用户在对话页看到的默认模型是猜的。
    """
    model = (db.get_default_model(mode=SSH_MODE)
             or db.get_default_model(mode=None))
    if not model:
        # 回退：返回第一个模型
        models = db.list_models_config()
        if models:
            return models[0]
        raise HTTPException(status_code=404, detail="No models configured")
    return model


@router.get("/settings/models")
def list_models_config():
    """获取所有模型配置"""
    return db.list_models_config()


@router.post("/settings/models")
def create_model_config(req: CreateModelRequest):
    """创建模型配置"""
    try:
        result = db.create_model_config(
            name=req.name,
            base_url=req.base_url,
            api_key=req.api_key,
            model_id=req.model_id,
            provider=req.provider,
            supports_thinking=req.supports_thinking,
            supports_image=req.supports_image,
            supports_tool_calling=req.supports_tool_calling,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            max_output_tokens=req.max_output_tokens,
            top_p=req.top_p,
            is_default_ssh=req.is_default_ssh,
            reasoning_tokens=req.reasoning_tokens,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        # S10-19：服务端故障不是输入错误。原实现把**任何**异常折成 400 + str(e)，
        # 于是 "database is locked" / "disk I/O error" / 缺列这类运维问题在面板上
        # 显示成「你填的东西不对」—— 用户会反复改表单，而真正的原因在库里。
        # 入参校验由 CreateModelRequest 的 Field 约束负责（到不了这里就 422 了），
        # 所以这条 except 收到的几乎全是服务端侧异常。
        logger.exception("模型配置创建失败")
        raise HTTPException(status_code=500, detail=f"模型配置保存失败：{e}")


@router.patch("/settings/models/{config_id}")
def update_model_config(config_id: str, req: UpdateModelRequest):
    """更新模型配置"""
    # 路径参数用 config_id 避免与 UpdateModelRequest.model_id 字段同名冲突
    data = req.model_dump(exclude_unset=True)
    result = db.update_model_config(config_id, **data)
    if not result:
        raise HTTPException(status_code=404, detail="Model not found")
    # 模型配置变更后清除 embedding 客户端缓存（base_url/api_key 可能已变）
    from agent.memory.embedding import clear_cache as clear_embedding_cache
    clear_embedding_cache()
    return result


@router.delete("/settings/models/{config_id}")
def delete_model_config(config_id: str):
    """删除模型配置"""
    try:
        deleted = db.delete_model_config(config_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Model not found")
        from agent.memory.embedding import clear_cache as clear_embedding_cache
        clear_embedding_cache()
        return {"deleted": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── 连接测试 ──

@router.post("/settings/models/{config_id}/test")
async def test_model_connection(config_id: str):
    """测试模型端点连接（HTTP 健康检查），返回 JSON

    响应始终 HTTP 200；body.status=ok 表示端点可达且目标模型在服务端列表中，
    body.status=error 表示端点不可达、HTTP 错误或目标模型不在列表（业务层失败）。
    """
    import httpx
    import time as _time

    # 审查 M-13：db 同步调用放线程池（本路由因 httpx 异步探测保持 async）
    model = await asyncio.to_thread(db.get_model_config, config_id, mask_key=False)
    if not model:
        logger.warning("[TEST] config not found: %s", config_id)
        raise HTTPException(status_code=404, detail="Model not found")

    base_url = model["base_url"].rstrip("/")
    api_key = model.get("api_key", "") or "EMPTY"
    # model_id 为空时回退到内部数据库 id，并在响应里注明「未配置 model_id」便于前端提示
    model_id_field = model.get("model_id", "") or ""
    used_fallback_id = False
    if model_id_field:
        model_name = model_id_field
    else:
        model_name = model["id"]
        used_fallback_id = True

    # 拼接 /models 端点（base_url 可能是 .../v1 或 .../v1/）
    models_url = f"{base_url}/models" if not base_url.endswith("/models") else base_url
    headers = {"Authorization": f"Bearer {api_key}"}

    logger.info(
        "[TEST] config_id=%s base_url=%s model_name=%s (fallback_id=%s) api_key_len=%d",
        config_id, netconfig.redact_urls_in_text(models_url), model_name,
        used_fallback_id,
        len(api_key) if api_key != "EMPTY" else 0,
    )

    start = _time.monotonic()
    try:
        async with netconfig.new_client(default_timeout=10.0) as client:
            resp = await client.get(models_url, headers=headers)
            logger.info(
                "[TEST] upstream HTTP %s, latency=%.0fms",
                resp.status_code, (_time.monotonic() - start) * 1000,
            )
            resp.raise_for_status()
            # 检查目标模型是否在返回列表中
            data = resp.json()
            available = [m.get("id", "") for m in data.get("data", [])]
            if model_name not in available:
                # 仅告知目标模型不在列表，不回传完整 available（避免泄露同 tenant 其他模型 id）
                if used_fallback_id:
                    detail = (
                        f"模型 {model_name} 不在服务端可用列表中（提示：该配置未填写「模型 ID」字段，"
                        "已用内部 id 匹配；请在设置中补填 model_id 后重试）"
                    )
                else:
                    detail = f"模型 {model_name} 不在服务端可用列表中"
                logger.warning(
                    "[TEST] model not in upstream list: %s (available_count=%d)",
                    model_name, len(available),
                )
                return {"status": "error", "detail": detail, "model_name": model_name}
        latency_ms = int((_time.monotonic() - start) * 1000)
        logger.info("[TEST] ok latency=%dms", latency_ms)
        return {"status": "ok", "latency_ms": latency_ms, "model_name": model_name}
    except httpx.ConnectTimeout:
        # S10-21：说准是哪一相到点。「连不上」和「连上了没回应」是两件不同的事，
        # 混成一句「连接超时 (Ns)」会让用户去查错的方向（N 此前还是用户设定的标量，
        # 而 connect 相被 netconfig 夹到 min(10,t)，两者都不等于报出的数）。
        connect_limit, _ = netconfig.effective_timeout_bounds(10.0)
        logger.warning("[TEST] connect timeout (<=%ss) url=%s", connect_limit,
                       netconfig.redact_urls_in_text(models_url))
        return {
            "status": "error",
            "detail": f"无法建立连接（连接上限 {int(connect_limit)}s）",
            "model_name": model_name,
            "timeout_seconds": int(connect_limit),
        }
    except httpx.TimeoutException:
        _, read_limit = netconfig.effective_timeout_bounds(10.0)
        logger.warning("[TEST] read timeout (<=%ss) url=%s", read_limit,
                       netconfig.redact_urls_in_text(models_url))
        return {
            "status": "error",
            "detail": f"已连上但服务端未在时限内响应（读取上限 {int(read_limit)}s）",
            "model_name": model_name,
            "timeout_seconds": int(read_limit),
        }
    except httpx.HTTPStatusError as e:
        # S10-03：detail 会经渲染层 toast 出来，而上游错误体和 httpx 异常消息都常把
        # 请求 URL 原样回显 —— base_url 带凭据（企业网关形态）时口令就这样离开后端。
        body = netconfig.redact_urls_in_text(e.response.text[:200])
        logger.warning("[TEST] upstream HTTP %d: %s", e.response.status_code, body)
        return {
            "status": "error",
            "detail": f"HTTP {e.response.status_code}: {body}",
            "model_name": model_name,
        }
    except Exception as e:
        detail = netconfig.redact_urls_in_text(str(e)) or type(e).__name__
        logger.warning("[TEST] unexpected: %s", detail)
        return {"status": "error", "detail": detail, "model_name": model_name}


# ── 记忆设置 ──


class MemorySettingsRequest(BaseModel):
    """记忆设置写入体。全字段 Optional =「不传 = 不改」（与 /settings/http 同款契约）：
    带默认值的请求模型会让只发一项的调用把其余五项静默抹回默认。
    """
    enabled: Optional[bool] = None
    max_items: Optional[int] = Field(None, ge=10, le=500)
    # 两条注入通道分账的 token 预算：画像块（常驻）与检索注入（每轮按相关性）
    inject_budget_tokens: Optional[int] = Field(None, ge=200, le=8000)
    blocks_budget_tokens: Optional[int] = Field(None, ge=200, le=8000)
    # 预检索等待上限：embedding API 慢时用户在等 SSE 首字节，上限越短越早放弃
    search_timeout_s: Optional[float] = Field(None, ge=0.5, le=30.0)
    # 关掉 = 后台整理只做阈值去重，不调模型；记忆仍会写入，只是不再消解矛盾
    reconcile_llm_enabled: Optional[bool] = None


@router.get("/settings/memory")
def get_memory_settings():
    """获取记忆设置"""
    return db.get_memory_settings()


@router.put("/settings/memory")
def update_memory_settings(req: MemorySettingsRequest):
    """更新记忆设置：未出现的键保持原值，整批单事务落库。"""
    data = req.model_dump(exclude_none=True)  # 未出现的键 = 不改
    updates: dict[str, str] = {}
    for field in ("enabled", "reconcile_llm_enabled"):
        if field in data:
            updates[f"memory.{field}"] = "true" if data[field] else "false"
    for field in ("max_items", "inject_budget_tokens", "blocks_budget_tokens",
                  "search_timeout_s"):
        if field in data:
            updates[f"memory.{field}"] = str(data[field])
    if updates:
        # 与审批策略/HTTP 策略同款：逐条 set_app_setting 各自提交，中途一条失败
        # 会留下「库里一半新一半旧」，而 GET 又把另一半当已生效报给面板。
        try:
            db.update_app_settings_bulk(updates)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"记忆设置写入失败，已整体回滚: {e}")
    return db.get_memory_settings()


# ── 审批授权策略（E3：allowlist 保留时长）──


class PermissionPolicyRequest(BaseModel):
    # 全字段 Optional =「不传 = 不改」（与 /settings/http 同款契约）：带默认值的
    # 请求模型会让只发 TTL 的旧调用把自动审批开关静默翻开。
    # 上限一年：再大的值与"永久"在使用上没区别，却会让脏值把到期时间推到不可读的年份
    allowlist_ttl_hours: Optional[int] = Field(
        None, ge=0, le=8760, description="永久授权保留时长（小时）；0 = 永不过期")
    auto_approve_enabled: Optional[bool] = Field(
        None, description="自动审批设置侧开关；env kill switch 优先级更高")
    # 0 = 永久等待（审批卡不过期）；上限一天，再长与永久没使用差别
    approval_fallback_s: Optional[int] = Field(
        None, ge=0, le=86400, description="审批兜底等待秒数；0 = 永久等待")


@router.get("/settings/permission-policy")
def get_permission_policy():
    """获取审批授权策略。默认 TTL=0（永久）、自动审批开、兜底 600s。"""
    return db.get_permission_policy()


@router.put("/settings/permission-policy")
async def update_permission_policy(req: PermissionPolicyRequest):
    """更新审批授权策略，并即时推给进程内运行时（gate TTL / auto_approve 开关 /
    core 兜底时长）。

    只写库不够：三者都是进程内状态，不推这一步，改完设置要重启后端才生效 ——
    而用户刚把时长调小到 24h，恰恰是希望立刻把不再想要的预授权收回来。

    S2-03 修复：本路由必须是 `async def`。原实现是同步 `def`，FastAPI 把它丢进
    anyio 线程池执行，于是三个运行时 mutator 在**非事件循环线程**上改全局状态 ——
    其中 `set_allowlist_ttl_hours` 还会直接动进程级 PermissionGate 单例并调用
    `prune_expired_allowlist()`（删键 + 落 allowlist 文件），而同一时刻事件循环上
    的 `check_permission`/grant 正在读同一批状态。同族的 grant/revoke/release-run/
    deny-command 四条端点正是为消掉这一类竞态改成 async def 的（口径见
    routes/agent.py 的 grant 路由 docstring：「回到循环后互斥天然成立」），
    这条漏了一处 —— 而它是唯一会**删授权**的那条腿。
    落库与末尾的策略读回走 asyncio.to_thread，与 agent.py 把审计写移出循环同款。
    """
    # 审查R2修复：多键逐条 set_app_setting 各自提交，中途一条失败会让库里
    # 「一半新一半旧」，而内存策略仍按全量新值应用，两边各说各话。改为
    # update_app_settings_bulk 单事务落库：整体成功后才推内存运行时，
    # 失败整体回滚并 500（内存策略保持旧值，与库一致）。
    updates: dict[str, str] = {}
    if req.allowlist_ttl_hours is not None:
        updates["permission.allowlist_ttl_hours"] = str(req.allowlist_ttl_hours)
    if req.auto_approve_enabled is not None:
        updates["permission.auto_approve_enabled"] = (
            "true" if req.auto_approve_enabled else "false")
    if req.approval_fallback_s is not None:
        updates["permission.approval_fallback_s"] = str(req.approval_fallback_s)
    try:
        await asyncio.to_thread(db.update_app_settings_bulk, updates)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"审批策略写入失败，已整体回滚，运行时未变更: {e}")
    # 以下三个调用现在都跑在事件循环上：与 gate 的消费方串行，不再跨线程改状态
    if req.allowlist_ttl_hours is not None:
        from agent.harness.reins.gate import set_allowlist_ttl_hours
        set_allowlist_ttl_hours(req.allowlist_ttl_hours)
    if req.auto_approve_enabled is not None:
        from agent.harness.reins.auto_approve import set_db_enabled
        set_db_enabled(req.auto_approve_enabled)
    if req.approval_fallback_s is not None:
        from agent.harness.reins.core import set_approval_fallback_seconds
        set_approval_fallback_seconds(req.approval_fallback_s)
    return await asyncio.to_thread(db.get_permission_policy)


# ── 会话保留清理（用户数据，默认关闭）──

class ConversationRetentionRequest(BaseModel):
    # 全字段 Optional =「不传 = 不改」：keep_count 默认值若为 1000，只发 enabled 的
    # 调用会把用户精心调过的保留条数静默改回去 —— 而这条开关打开后是会删会话的。
    enabled: Optional[bool] = None
    # 0 是「不限制 = 一条都不删」的哨兵（database.prune_conversations 的 <=0 早退），
    # 不是「清空全部」。面板文案此前反着写，用户会以为填 0 就能清库，实际开了开关
    # 却永远不清理。要清空请显式删会话，不给一个数字输入框批量删用户数据的权力。
    keep_count: Optional[int] = Field(None, ge=0, le=100000,
                                      description="保留最近多少条已结束会话；0 = 不限制（不清理）")


@router.get("/settings/conv-retention")
def get_conv_retention():
    """获取会话保留设置（默认关闭）。开启后按 keep_count 清理最旧的已结束会话。"""
    return db.get_conv_retention()


@router.put("/settings/conv-retention")
def update_conv_retention(req: ConversationRetentionRequest):
    """更新会话保留设置。enabled=false（默认）时不做任何自动清理。"""
    data = req.model_dump(exclude_none=True)  # 未出现的键 = 不改
    updates: dict[str, str] = {}
    if "enabled" in data:
        updates["conv_retention.enabled"] = "true" if data["enabled"] else "false"
    if "keep_count" in data:
        updates["conv_retention.keep_count"] = str(data["keep_count"])
    if updates:
        try:
            db.update_app_settings_bulk(updates)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"会话保留设置写入失败，已整体回滚: {e}")
    return db.get_conv_retention()


# ── 后处理设置 ──


class PostProcessSettingsRequest(BaseModel):
    """后处理开关写入体。全字段 Optional =「不传 = 不改」：四个字段都带默认 True
    时，只发 enabled=false 的调用会把三个子任务开关静默打开。
    """
    enabled: Optional[bool] = None
    generate_title: Optional[bool] = None
    extract_memory: Optional[bool] = None
    generate_skill: Optional[bool] = None


_PP_KEYS = ("enabled", "generate_title", "extract_memory", "generate_skill")


@router.get("/settings/post-process")
def get_post_process_settings():
    """获取后处理设置 — 只读，不修改全局配置"""
    from post_processor import load_post_process_config_from_db

    cfg = load_post_process_config_from_db()
    return {k: getattr(cfg, k) for k in _PP_KEYS}


@router.put("/settings/post-process")
def update_post_process_settings(req: PostProcessSettingsRequest):
    """更新后处理设置并同步到全局 PostProcessConfig"""
    from post_processor import set_post_process_config, load_post_process_config_from_db

    data = req.model_dump(exclude_none=True)  # 未出现的键 = 不改
    updates = {
        f"post_process.{k}": "true" if data[k] else "false"
        for k in _PP_KEYS if k in data
    }
    if updates:
        # 逐条 set_app_setting 中途失败会留下「库里一半新一半旧」，而下面仍会把
        # 全量新值推进内存 cfg —— GET 报的一半和实际跑的一半对不上。
        try:
            db.update_app_settings_bulk(updates)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"后处理设置写入失败，已整体回滚: {e}")
    # 与重启回填共用同一个读函数：两条路径若各拼一次 cfg，非开关字段（超时/频率）
    # 就会在 PUT 后被重置成 dataclass 默认，而重启后不是。
    cfg = load_post_process_config_from_db()
    set_post_process_config(cfg)
    return {k: getattr(cfg, k) for k in _PP_KEYS}


# ── 网络工具密钥（Tavily 等）──


@router.get("/settings/web-tools")
def get_web_tools_settings():
    """获取网络工具配置。密钥只回传掩码值与来源，不回传明文。"""
    env_key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    db_key = (db.get_app_setting("web.tavily_api_key") or "").strip()
    active = env_key or db_key
    source = "env" if env_key else ("db" if db_key else "")
    return {
        "tavily_api_key_set": bool(active),
        "tavily_api_key_masked": _mask_secret(active),
        "tavily_api_key_source": source,
    }


class WebToolsSettingsRequest(BaseModel):
    tavily_api_key: str = Field(..., max_length=256,
                                description="Tavily API Key；传空字符串 = 清除已存密钥")


@router.put("/settings/web-tools")
def update_web_tools_settings(req: WebToolsSettingsRequest):
    """保存/清除 Tavily API Key（存 app_settings；环境变量优先级更高）"""
    # 空串即清除，不必分分支写：set_app_setting(key, "") 就是清除态
    db.set_app_setting("web.tavily_api_key", req.tavily_api_key.strip())
    return get_web_tools_settings()


# ── 通知 webhook（方案 Phase 4：巡检异常 IM 推送）──


@router.get("/settings/notify")
def get_notify_settings():
    """获取通知配置。webhook URL 内含机器人 key，只回传脱敏形态，不回明文。"""
    raw = (db.get_app_setting("notify.webhook_url") or "").strip()
    return {
        "webhook_url_set": bool(raw),
        "webhook_url_masked": _mask_secret(raw),
    }


class NotifySettingsRequest(BaseModel):
    webhook_url: str = Field("", max_length=512,
                             description="企微/钉钉群机器人 webhook 地址；"
                                         "空字符串 = 关闭推送")


@router.put("/settings/notify")
def update_notify_settings(req: NotifySettingsRequest):
    """保存/清除通知 webhook（存 app_settings，空串即关闭）"""
    db.set_app_setting("notify.webhook_url", req.webhook_url.strip())
    return get_notify_settings()


# ── 出站 HTTP 策略（G4 代理 / G5 超时与重试）──


class HttpSettingsRequest(BaseModel):
    """出站 HTTP 策略写入体。三个字段都可选，缺省 = 不改那一项。

    为什么要"不改"这个第三态：GET 只回脱敏代理（可能带密码，不能回明文）。若代理
    字段缺省即视为"清除"，用户只想改超时就会顺带把代理抹了 —— 而抹掉的还是明文
    口令那部分，重连才发现。"" 才是显式清除（直连）。
    """
    # 非空可带 userinfo（http://user:pass@host:port）；scheme 与主机名由 netconfig 校验
    proxy_url: Optional[str] = Field(None, max_length=512,
                                     description="出站代理；http/https/socks5/socks5h/socks4/socks4a；"
                                                 "不传 = 保持原值，空串 = 清除并直连")
    # 0 = 不覆盖，各调用点沿用自己的旧硬编码（LLM read 300s、embedding 10s、测试 10s…）
    timeout_seconds: Optional[int] = Field(None, ge=0, le=600,
                                           description="上游请求超时秒数；0 = 用各端点原默认")
    retry_count: Optional[int] = Field(None, ge=0, le=3,
                                       description="重试次数，仅 LLM 调用生效（openai SDK max_retries）；"
                                                   "embedding/网页抓取/连接测试用裸 httpx 客户端，不重试")


async def _http_view() -> dict:
    # 展示**生效**策略（netconfig 已合并 env 兜底），不是库里的原始行：只看库的话，
    # 代理由 CKUN_HTTP_PROXY 注入的部署里面板会显示"无代理"，而请求其实都在走。
    # 审查 M-13 补漏：get_policy 首访会懒加载 load_policy_from_db（同步 SQLite 读），
    # 本路由 async（httpx 异步探测才保持 async），同步读必须进线程池。
    policy = (netconfig._policy if netconfig._loaded
              else await asyncio.to_thread(netconfig.load_policy_from_db))
    return {
        # 与 api_key 同一套做法：只回脱敏形态，前端改代理时要显式重填
        "proxy_url_masked": netconfig.redact_proxy(policy["proxy_url"]),
        "proxy_url_set": bool(policy["proxy_url"]),
        "proxy_source": await asyncio.to_thread(_proxy_source),
        "timeout_seconds": policy["timeout_seconds"],
        "retry_count": policy["retry_count"],
    }


def _proxy_source() -> str:
    """代理来自哪里：库里那行（非空才算），还是环境变量兜底（运维统一注入的形态）。

    判据必须与 netconfig.load_policy_from_db 用的**同一条**（「库里非空才算 db 来源」）。
    这里曾随切片审查 S0-01 改成「行存在即权威、空串算清除」，于是面板在用户清除代理
    后显示「来自数据库」，而请求其实照 env 走 —— 显示与生效又分叉。
    S0-01 本身对不对留待产品裁决（来龙去脉见 netconfig.load_policy_from_db 的注释），
    裁决前不要单侧改这条，也不要把它当「同族防护漏一处」顺手补齐。
    """
    if (db.get_app_setting("http.proxy_url") or "").strip():
        return "db"
    if (os.environ.get("CKUN_HTTP_PROXY") or "").strip():
        return "env"
    return ""


@router.get("/settings/http")
async def get_http_settings():
    """获取出站 HTTP 策略（代理只回脱敏值）。"""
    return await _http_view()


@router.put("/settings/http")
async def update_http_settings(req: HttpSettingsRequest):
    """更新出站 HTTP 策略并让所有在途客户端按新策略重建。

    只写库不够：llm_adapter 按 (base_url, api_key) 缓存 AsyncOpenAI，代理与超时
    在建 client 时就固化了；不清缓存，用户配好代理后仍会直连到后端重启为止。
    """
    data = req.model_dump(exclude_none=True)  # 未出现的键 = 不改
    # 审查 M-13 补漏：本 handler 的同步 DB 读写（懒加载/整批落库）进线程池
    cur = (netconfig._policy if netconfig._loaded
           else await asyncio.to_thread(netconfig.load_policy_from_db))
    proxy = cur["proxy_url"]
    timeout = cur["timeout_seconds"]
    retries = cur["retry_count"]
    if "proxy_url" in data:
        try:
            proxy = netconfig.normalize_proxy_url(data["proxy_url"])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if "timeout_seconds" in data:
        timeout = int(data["timeout_seconds"])
    if "retry_count" in data:
        retries = int(data["retry_count"])
    # 审查R2修复：三条 set_app_setting 逐条提交与 netconfig 内存策略非原子 ——
    # 中途一条失败会留下「库里一半新一半旧、内存全量新」的分裂态。改为
    # update_app_settings_bulk 单事务落库：整体成功后才应用内存策略并重建
    # 客户端缓存；失败整体回滚并 500（netconfig 保持旧策略，与库一致）。
    updates: dict[str, str] = {}
    if "proxy_url" in data:
        updates["http.proxy_url"] = proxy
    if "timeout_seconds" in data:
        updates["http.timeout_seconds"] = str(timeout)
    if "retry_count" in data:
        updates["http.retry_count"] = str(retries)
    try:
        await asyncio.to_thread(db.update_app_settings_bulk, updates)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"HTTP 策略写入失败，已整体回滚，运行时未变更: {e}")
    netconfig.set_policy(proxy, timeout, retries)
    from llm_adapter import invalidate_client_cache
    dropped = await invalidate_client_cache()
    # PERF-005：embedding 的共享 httpx 客户端同样按 (超时, 代理) 缓存，策略
    # 变更一并重建，避免旧代理配置的连接池残留
    from agent.memory.embedding import invalidate_http_clients as invalidate_embed_clients
    await invalidate_embed_clients()
    return {**(await _http_view()), "clients_rebuilt": dropped["dropped"]}


# ── 日志级别（G9）──


class LogLevelRequest(BaseModel):
    level: Literal["debug", "info", "warning", "error", "critical"]


@router.get("/settings/log-level")
def get_log_level():
    import logconfig
    return {"level": logconfig.get_level(), "levels": list(logconfig.LEVELS)}


@router.put("/settings/log-level")
def update_log_level(req: LogLevelRequest):
    """改后端日志级别：即时生效（不用重启），并落库供下次启动回填。"""
    import logconfig
    db.set_app_setting("log.level", req.level)
    applied = logconfig.apply_level(req.level, persist_note=False)
    return {"level": applied, "levels": list(logconfig.LEVELS)}


# ── 诊断快照（G10）──

# 键名含这些子串的 app_settings 值一律脱敏：代理 URL 可带密码、tavily/模型 key 是密钥
_DIAG_SECRET_HINTS = ("api_key", "token", "secret", "password", "proxy")

# 纯数值的设置键：值不可能是凭证，诊断面板要能读到真实读数。
# 必须逐键列举，不能用前缀 —— models_config.api_key 这类键同样带点，而前缀放行会
# 直接把明文密钥放出去。新增一个带 secret hint 的数值键时，这里要同键加一行，
# 否则它会在诊断面板里被掩成「已设置」而读不到数（历史事故：max_total_tokens）。
_DIAG_NUMERIC_KEYS = frozenset({
    "conv_retention.keep_count",
    "http.retry_count",
    "http.timeout_seconds",
    "memory.blocks_budget_tokens",
    "memory.inject_budget_tokens",
    "memory.max_items",
    "memory.search_timeout_s",
    "permission.allowlist_ttl_hours",
    "permission.approval_fallback_s",
})


def _is_numeric_setting_value(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _mask_secret(value: str) -> str:
    """密钥展示的统一口径：前 4 后 4，短到拼回去没有意义的就只报「已设置」。
    诊断面板与 web-tools 那页此前各写一份同样的表达式，改一处就会漏另一处。"""
    return (value[:4] + "…" + value[-4:]) if len(value) > 12 else ("已设置" if value else "")


def _mask_value(key: str, value: str) -> str:
    lowered = key.lower()
    if "proxy" in lowered:
        # 代理分支先于一切：口令可能是纯数字，不能让数值例外放它过去
        return netconfig.redact_proxy(value)
    if lowered in _DIAG_NUMERIC_KEYS and _is_numeric_setting_value(value):
        # `token` 这个 hint 会顺手命中 inject_budget_tokens / blocks_budget_tokens，
        # 把纯数值预算掩成"已设置"等于让诊断面板读不到它最该读的读数。放行条件是
        # 「在白名单里」且「值确实解析为数字」—— 两者缺一仍按密钥处理，所以一个
        # 16 位纯数字的 tavily key 依旧被打码。
        return value
    if any(h in lowered for h in _DIAG_SECRET_HINTS):
        return _mask_secret(value)
    return value


@router.get("/settings/diagnostics")
async def get_diagnostics():
    """运维诊断快照：DB / 后端版本 / 日志 / HTTP 策略 / 意识 tick / Bridge / 设置键。

    只读，且刻意不含任何明文密钥。路径信息照实回 —— 这个面板的用途就是"我的后端
    到底连的是哪个库、桥有没有接上"，把路径打码就等于把面板废掉。
    """
    import logconfig
    from config import settings as app_config
    from agent.consciousness_tick import get_global_tick
    from agent.bridge_relay import get_bridge_relay

    # 审查 M-13 补漏：本 handler 的所有 SQLite 读写（含懒加载路径）进线程池，
    # busy_timeout=5s 锁竞争时不再冻结事件循环（SSE/agent 流同循环共生）。
    db_path = await asyncio.to_thread(db.get_db_path) or app_config.db_path
    size_bytes = None
    if db_path:
        try:
            size_bytes = await asyncio.to_thread(os.path.getsize, db_path)
        except OSError:
            size_bytes = None

    tick = get_global_tick()
    relay = get_bridge_relay()
    snapshot, counts, policy = await asyncio.gather(
        asyncio.to_thread(db.list_app_settings),
        asyncio.to_thread(db.diag_counts),
        asyncio.to_thread(
            netconfig.load_policy_from_db if not netconfig._loaded else netconfig.get_policy),
    )
    # 读模块全局而不是 _get_global_permission_gate_sync()：后者会在诊断请求里顺手
    # 建一个 gate（加载 allowlist、跑一次过期剪枝），诊断面板不该有写副作用。
    import agent.harness.reins.gate as gate_mod
    gate = gate_mod._global_permission_gate
    allowlist = {
        "ttl_hours": int(getattr(gate, "allowlist_ttl_hours",
                                  gate_mod.get_allowlist_ttl_hours()) or 0),
        "permanent_entries": len(gate.permanent_entries()) if gate is not None else None,
    }

    return {
        "backend": {
            "version": app_config.version,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": sys.platform,
            "pid": os.getpid(),
        },
        "database": {
            "path": db_path,
            "size_bytes": size_bytes,
            "counts": counts,
        },
        "logging": {"level": logconfig.get_level(), "levels": list(logconfig.LEVELS)},
        "http": {
            "proxy": netconfig.redact_proxy(policy["proxy_url"]),
            "proxy_set": bool(policy["proxy_url"]),
            "timeout_seconds": policy["timeout_seconds"],
            "retry_count": policy["retry_count"],
        },
        "tick": {
            "running": bool(getattr(tick, "_task", None) and not tick._task.done()),
            "last_result": tick.last_tick_result,
        },
        "bridge": {
            "connected": bool(relay.connected) if relay is not None else False,
            "present": relay is not None,
        },
        "permissions": allowlist,
        "app_settings": {k: _mask_value(k, v or "") for k, v in snapshot.items()},
    }


# ── 成本定价覆盖（内置定价表静态快照的结构性补丁）──


class CostPricingOverridesRequest(BaseModel):
    # 整体替换语义（与 memory/permission-policy 的「未出现不改」契约不同）：
    # 覆盖表本身就是被反复编辑的字典，整体替换 + 后端全量校验最直观，
    # 半更新语义反而要求前端先 GET 再拼差量。
    # 本地部署（Ollama/vLLM 自有硬件）记 [0, 0] 表示零成本。
    overrides: dict[str, list[float]] = Field(default_factory=dict)


@router.get("/settings/cost-pricing")
def get_cost_pricing():
    """当前生效的用户定价覆盖（模型 ID 小写 → [输入价, 输出价] USD/1M）。"""
    from agent.cost_tracker import get_pricing_overrides
    return {"overrides": {k: list(v) for k, v in get_pricing_overrides().items()}}


@router.put("/settings/cost-pricing")
def update_cost_pricing(req: CostPricingOverridesRequest):
    """整体替换定价覆盖表；任一条非法 422 且不落库（要么整表合法要么不改）。"""
    from agent.cost_tracker import set_pricing_overrides
    try:
        effective = set_pricing_overrides(req.overrides)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"overrides": {k: list(v) for k, v in effective.items()}}
