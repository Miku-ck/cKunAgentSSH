"""
模型与设置路由 — 模型配置 CRUD + 连接测试
"""
import asyncio
import os
from typing import Optional, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import database as db


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

# 注意：/settings/models/default 必须注册在 /settings/models/{config_id} 之前，
# 否则 FastAPI 按注册顺序匹配时 "default" 会被 {config_id} 路径先捕获导致功能失效。
@router.patch("/settings/models/default")
def set_default_model(
    model_id: str = Query(..., description="要设为默认的模型 id"),
    mode: Literal["ssh"] = Query("ssh", description="模式"),
):
    """设置默认模型"""
    model = db.get_model_config(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    db.set_default_model(model_id, mode=mode)
    return {"default": model_id, "mode": mode}


@router.get("/settings/models/default")
def get_default_model(mode: Literal["ssh"] = Query("ssh")):
    """获取默认模型"""
    model = db.get_default_model(mode=mode)
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/settings/models/{config_id}")
def update_model_config(config_id: str, req: UpdateModelRequest):
    """更新模型配置"""
    # 路径参数用 config_id 避免与 UpdateModelRequest.model_id 字段同名冲突
    data = req.model_dump(exclude_unset=True)
    result = db.update_model_config(config_id, **data)
    if not result:
        raise HTTPException(status_code=404, detail="Model not found")
    # 模型配置变更后清除 MemoryManager 缓存（base_url/api_key 可能已变）
    from agent.memory import invalidate_memory_manager_cache
    invalidate_memory_manager_cache()
    return result


@router.delete("/settings/models/{config_id}")
def delete_model_config(config_id: str):
    """删除模型配置"""
    try:
        deleted = db.delete_model_config(config_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Model not found")
        from agent.memory import invalidate_memory_manager_cache
        invalidate_memory_manager_cache()
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
    import logging

    log = logging.getLogger("cKunAgentic.settings")

    # 审查 M-13：db 同步调用放线程池（本路由因 httpx 异步探测保持 async）
    model = await asyncio.to_thread(db.get_model_config, config_id, mask_key=False)
    if not model:
        log.warning("[TEST] config not found: %s", config_id)
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

    log.info(
        "[TEST] config_id=%s base_url=%s model_name=%s (fallback_id=%s) api_key_len=%d",
        config_id, models_url, model_name, used_fallback_id,
        len(api_key) if api_key != "EMPTY" else 0,
    )

    start = _time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(models_url, headers=headers)
            log.info(
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
                log.warning(
                    "[TEST] model not in upstream list: %s (available_count=%d)",
                    model_name, len(available),
                )
                return {"status": "error", "detail": detail, "model_name": model_name}
        latency_ms = int((_time.monotonic() - start) * 1000)
        log.info("[TEST] ok latency=%dms", latency_ms)
        return {"status": "ok", "latency_ms": latency_ms, "model_name": model_name}
    except httpx.TimeoutException:
        log.warning("[TEST] timeout (10s) url=%s", models_url)
        return {"status": "error", "detail": "连接超时 (10s)", "model_name": model_name}
    except httpx.HTTPStatusError as e:
        log.warning(
            "[TEST] upstream HTTP %d: %s",
            e.response.status_code, e.response.text[:200],
        )
        return {
            "status": "error",
            "detail": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            "model_name": model_name,
        }
    except Exception as e:
        log.warning("[TEST] unexpected: %s", e)
        return {"status": "error", "detail": str(e) or type(e).__name__, "model_name": model_name}


# ── 记忆设置 ──


class MemorySettingsRequest(BaseModel):
    enabled: bool = True
    # 白名单与前端 settings 仓库的 union 同口径（stores/settings.ts 只认 auto/manual，
    # 回填时其余值静默丢弃）：这里不校验的话，脏值能写进库并一直留在配置接口里
    strategy: Literal["auto", "manual"] = "auto"
    max_items: int = Field(100, ge=10, le=500)


@router.get("/settings/memory")
def get_memory_settings():
    """获取记忆设置"""
    return db.get_memory_settings()


@router.put("/settings/memory")
def update_memory_settings(req: MemorySettingsRequest):
    """更新记忆设置"""
    db.set_app_setting("memory.enabled", "true" if req.enabled else "false")
    db.set_app_setting("memory.strategy", req.strategy)
    db.set_app_setting("memory.max_items", str(req.max_items))
    return db.get_memory_settings()


# ── 会话保留清理（用户数据，默认关闭）──


class ConversationRetentionRequest(BaseModel):
    enabled: bool = False
    keep_count: int = Field(1000, ge=0, le=100000)


@router.get("/settings/conv-retention")
def get_conv_retention():
    """获取会话保留设置（默认关闭）。开启后按 keep_count 清理最旧的已结束会话。"""
    return db.get_conv_retention()


@router.put("/settings/conv-retention")
def update_conv_retention(req: ConversationRetentionRequest):
    """更新会话保留设置。enabled=false（默认）时不做任何自动清理。"""
    db.set_app_setting("conv_retention.enabled", "true" if req.enabled else "false")
    db.set_app_setting("conv_retention.keep_count", str(req.keep_count))
    return db.get_conv_retention()


# ── 后处理设置 ──


class PostProcessSettingsRequest(BaseModel):
    enabled: bool = True
    generate_title: bool = True
    extract_memory: bool = True
    generate_skill: bool = True


@router.get("/settings/post-process")
def get_post_process_settings():
    """获取后处理设置 — 只读，不修改全局配置"""
    def _bool(key: str, default: bool) -> bool:
        v = db.get_app_setting(key)
        if v is None:
            return default
        return v.lower() == "true"

    return {
        "enabled": _bool("post_process.enabled", True),
        "generate_title": _bool("post_process.generate_title", True),
        "extract_memory": _bool("post_process.extract_memory", True),
        "generate_skill": _bool("post_process.generate_skill", True),
    }


@router.put("/settings/post-process")
def update_post_process_settings(req: PostProcessSettingsRequest):
    """更新后处理设置并同步到全局 PostProcessConfig"""
    from post_processor import set_post_process_config, PostProcessConfig

    db.set_app_setting("post_process.enabled", "true" if req.enabled else "false")
    db.set_app_setting("post_process.generate_title", "true" if req.generate_title else "false")
    db.set_app_setting("post_process.extract_memory", "true" if req.extract_memory else "false")
    db.set_app_setting("post_process.generate_skill", "true" if req.generate_skill else "false")

    cfg = PostProcessConfig(
        enabled=req.enabled,
        generate_title=req.generate_title,
        extract_memory=req.extract_memory,
        generate_skill=req.generate_skill,
    )
    set_post_process_config(cfg)
    return {
        "enabled": cfg.enabled,
        "generate_title": cfg.generate_title,
        "extract_memory": cfg.extract_memory,
        "generate_skill": cfg.generate_skill,
    }


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
        "tavily_api_key_masked": (active[:4] + "…" + active[-4:]) if len(active) > 12 else ("已设置" if active else ""),
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
