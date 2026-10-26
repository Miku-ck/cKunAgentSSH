"""运行环境（test / prod）的服务端权威判定。

## 这个模块解决的问题

`AgentRunRequest.environment` 是**渲染层自报**的字段，默认 "test"。而 prod 强化
（危险命令只认预登记 allowlist、拒绝临时审批）正是靠这个值分流的 —— 于是任何能
发 HTTP 请求的一方（渲染层脚本、被注入的插件、误配置的调用方）只要把一条 prod
主机的命令声明成 test，整套生产强化就静默失效，而审批卡上还写着「环境：test」。

环境标签不该由**这一次请求**说了算，它属于**目标主机**：用户在连接面板里持久化
配置的 `environment` 字段才是这条主机的属性。本模块把它读出来当权威值。

## 判定规则（只能收紧，不能放宽）

| 连接配置 | 客户端声称 | 判定结果 | 含义 |
|---------|-----------|---------|------|
| prod | test | **prod** | 客户端想降级，拒绝 |
| prod | prod | prod | 一致 |
| test | prod | **prod** | 客户端主动收紧，尊重 |
| test | test | test | 一致 |
| 读不到 | * | 客户端值 | 未核实（source 标明） |

「读不到」包括：桥未注入 token、WS 未连、会话已断开、连接配置已删除、Electron
版本还没有 ssh_session_info 这个 op。这些情况**不让 run 失败** —— 环境标签是
收紧手段，不是可用性前提；但 `source` 会一路带到审批卡与审计记录里，让审批人
看见「这个 test 是客户端声明的，服务端没核实过」。

## 为什么不做「读不到就当 prod」

那会让所有离线/旧版场景下的危险命令突然全部走预登记 allowlist，把可用性事故
伪装成安全收益。默认严格应当由部署方显式配置（例如把生产主机在连接面板里标成
prod），而不是靠猜。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 合法环境值。与 agent.audit / routes.audit / AuditView 的筛选项同源。
VALID_ENVIRONMENTS: tuple[str, ...] = ("test", "prod")

# 来源标识 → 审批卡上的中文补充说明（空串表示无需补充）
SOURCE_LABELS: dict[str, str] = {
    "connection": "服务端按主机连接配置判定",
    "connection_unknown": "服务端未能确定主机环境，按客户端声明",
    "bridge_unavailable": "桥接不可用，按客户端声明（未核实）",
    "client": "客户端声明（未核实）",
}


@dataclass(frozen=True)
class EnvironmentResolution:
    """一次 run 的环境判定结果（不可变，便于直接塞进 ctx.metadata）。"""

    effective: str
    requested: str
    source: str
    host: str = ""
    connection_id: str = ""
    tightening: bool = False
    note: str = ""

    @property
    def source_label(self) -> str:
        # 不是死代码：审批卡「环境来源」中文说明的取值入口，被
        # backend/tests/test_governance_env_and_audit_failclosed.py 钉住
        # （断言桥不可用时标签非空）。生产侧的卡片拼装在 reins/core.py 的
        # permission_display_fields 里按同一张 SOURCE_LABELS 表直查（行为等价：
        # source 取值只会是本模块的四个枚举，两处 fallback 分支都不可达）——
        # 若要收敛成只走本属性需改 core.py，勿在两侧各删一份。
        return SOURCE_LABELS.get(self.source, SOURCE_LABELS["client"])


def _normalize(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in VALID_ENVIRONMENTS else "test"


async def resolve_run_environment(
    requested: str,
    ssh_session_id: str = "",
) -> EnvironmentResolution:
    """判定本次 run 的真实环境标签。

    任何异常都降级为「按客户端声明 + 标明未核实」，绝不因为查不到主机信息而
    让 run 起不来 —— 环境标签用于收紧审批，可用性优先级更高，但证据不能因此
    撒谎（source 会被带进审批卡与审计）。
    """
    req = _normalize(requested)
    note = ""
    if (requested or "").strip().lower() not in VALID_ENVIRONMENTS:
        # 非法取值可能是客户端拼写错误，也可能是绕过尝试 —— 两者都该在服务端日志
        # 留一行；只写在 note 里等于没留（note 只随本次 run 的内存对象存在）。
        note = f"客户端环境值 {requested!r} 非法，按 test 处理"
        logger.warning("环境判定：%s", note)

    session_id = (ssh_session_id or "").strip()
    if not session_id:
        return EnvironmentResolution(
            effective=req, requested=req, source="client", note=note)

    info = await _fetch_session_info(session_id)
    if info is None:
        return EnvironmentResolution(
            effective=req, requested=req, source="bridge_unavailable", note=note)

    host_env = (str(info.get("environment") or "")).strip().lower()
    host = str(info.get("host") or "")
    connection_id = str(info.get("connectionId") or "")
    if host_env not in VALID_ENVIRONMENTS:
        return EnvironmentResolution(
            effective=req, requested=req, source="connection_unknown",
            host=host, connection_id=connection_id, note=note)

    effective = "prod" if "prod" in (host_env, req) else "test"
    tightening = effective == "prod" and req != "prod"
    if tightening:
        # 这条日志是「有人试图把生产主机当测试机跑」的唯一服务端线索
        logger.warning(
            "环境判定收紧: 客户端声称 %s，主机连接 %s(%s) 标记 %s → 按 prod 执行",
            req, connection_id or "-", host or "-", host_env,
        )
    return EnvironmentResolution(
        effective=effective,
        requested=req,
        source="connection",
        host=host,
        connection_id=connection_id,
        tightening=tightening,
        note=note,
    )


async def _fetch_session_info(session_id: str) -> dict | None:
    """问主进程要这条会话背后的连接配置。失败一律返回 None（不抛）。"""
    from agent.electron_bridge import ElectronBridge

    bridge = ElectronBridge()
    if not bridge.is_available():
        return None
    try:
        info = await bridge.session_info(session_id)
    except Exception as e:
        logger.info("环境判定：读取会话 %s 的连接配置失败（按客户端声明）: %s",
                    session_id, e)
        return None
    return info or None
