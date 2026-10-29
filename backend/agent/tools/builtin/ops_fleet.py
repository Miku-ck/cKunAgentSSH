"""
多会话只读扇出 — 一轮并发采集多台主机的只读观测，输出并排对比 + 超阈值标记。

设计要点：
- **只读铁律**：远端只跑 sys_overview 的固定观测模板（复用 ops_obs.sys_overview_cmd，
  与远端白名单 RE_SYS_OVERVIEW 同源），无任何写入/命令执行面；targets 只接受
  连接配置 id，不接受自由文本命令 —— 注入面为零。
- **目标解析**：targets 是 fleet_sessions 列出的连接配置 id。每台经
  bridge.connect_ssh 建临时轻量会话（S0-06 归属机制 → disconnect 只能断
  自己建的，用户终端不受影响），采集完在 finally 里全部断开，不残留连接。
- **fail-closed + 逐台隔离**：桥不可用整批拒绝；单台建连/采集失败只标记该行
  （❌ + 原因），不放大为整批失败 —— 多机巡检一台挂了不应掩掉其余各台的观测。
- **并发**：asyncio.gather，每台独立会话/独立 exec channel，天然可并发。
- **输出**：机器可读的对比表（load1 / 内存% / 磁盘 max%）+ 超阈值 ⚠ 标记 +
  各机观测明细。解析失败的字段如实标「未解析」，绝不猜数。
"""
import asyncio
import logging
import re

from agent.electron_bridge import BridgeError, ElectronBridge
from agent.tools import BaseTool, ToolMeta, ToolResult
from agent.tools.builtin.ops_obs import sys_overview_cmd

logger = logging.getLogger(__name__)

# 单批扇出上限：8 台并发建连 + 采集，超过既拖慢整体也放大桥接压力
_MAX_FLEET_TARGETS = 8
# 单台采集的超时（与 sys_overview 的 15s 同量级，留建连余量由 connect 自己管）
_COLLECT_TIMEOUT = 20
# 内置阈值（运维巡检的常见红线；保持简单 —— 工具参数越少越不容易被模型误用）
_THRESHOLD_LOAD1 = 4.0     # 1 分钟负载（无核数信息时的保守固定值）
_THRESHOLD_MEM_PCT = 90.0
_THRESHOLD_DISK_PCT = 90.0

_RE_LOAD = re.compile(r"load average:\s*([\d.]+)")
_RE_MEM = re.compile(r"Mem:\s+(\d+)\s+(\d+)\s+(\d+)")
_RE_DF = re.compile(r"\s(\d+)%\s+")          # df -h 的 Use% 列


def _parse_overview(output: str) -> dict:
    """从 sys_overview 输出提取 (load1, mem_used_pct, disk_max_pct)；缺失 → None。"""
    load1 = mem_pct = disk_max = None
    m = _RE_LOAD.search(output or "")
    if m:
        try:
            load1 = float(m.group(1))
        except ValueError:
            pass
    m = _RE_MEM.search(output or "")
    if m and float(m.group(1)) > 0:
        try:
            mem_pct = round(float(m.group(2)) / float(m.group(1)) * 100, 1)
        except (ValueError, ZeroDivisionError):
            pass
    pcts = [float(p) for p in _RE_DF.findall(output or "")]
    if pcts:
        disk_max = max(pcts)
    return {"load1": load1, "mem_pct": mem_pct, "disk_max": disk_max}


def _flag(value, threshold) -> str:
    if value is None:
        return "-"
    mark = "⚠" if value > threshold else ""
    return f"{value}{mark}"


class FleetSessionsTool(BaseTool):
    """列出可扇出的目标主机（连接配置）。"""

    name = "fleet_sessions"
    description = (
        "列出可扇出观测的目标主机（连接配置 id/名称/主机/环境）。"
        "把返回的 id 传给 fleet_overview 即可一次并发采集多台主机的系统概览。"
    )
    parameters = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    meta = ToolMeta(risk_level="safe", reversible=True, idempotent=True, timeout=20.0)

    async def execute(self, context) -> ToolResult:
        bridge = ElectronBridge()
        if not bridge.is_available():
            return ToolResult(False, "", "拒绝执行：Electron Bridge 未就绪（fail-closed）")
        try:
            connections = await bridge.list_connections()
        except BridgeError as e:
            return ToolResult(False, "", f"无法获取连接配置: {e}", priority="high")
        if not connections:
            return ToolResult(True, "当前没有任何已配置的 SSH 连接。", priority="low")
        lines = ["可扇出的目标主机（把 id 传给 fleet_overview）："]
        for c in connections:
            lines.append(
                f"- id={c.get('id')}  名称={c.get('name')}  主机={c.get('host')}"
                f"  环境={c.get('environment', '未知')}"
            )
        return ToolResult(True, "\n".join(lines), priority="low")


class FleetOverviewTool(BaseTool):
    """多会话只读扇出：一轮并发采集多台主机的系统概览并对比。"""

    name = "fleet_overview"
    description = (
        "对多台主机并发执行只读系统概览（CPU 负载/内存/磁盘），输出并排对比与"
        "超阈值标记（负载>4、内存>90%、磁盘>90% 标 ⚠）。targets 传 fleet_sessions"
        "返回的连接配置 id（最多 8 台）。任何单台失败只标记该行，不影响其余主机。"
        "本工具完全只读：远端只跑固定的观测脚本，不建临时文件、不改任何状态。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "连接配置 id 列表（来自 fleet_sessions），最多 8 台",
            },
            "top": {
                "type": "integer",
                "description": "每台磁盘占用 TOP 行数（1-20，默认 3）",
                "default": 3,
            },
        },
        "required": ["targets"],
        "additionalProperties": False,
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        # 建连 + 采集 + 断开的全流程声明上限
        timeout=90.0,
    )

    async def execute(self, context, targets, top=3):
        # 参数收敛
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list) or not targets:
            return ToolResult(False, "", "targets 不能为空（连接配置 id 列表）")
        targets = [str(t) for t in targets if str(t).strip()]
        truncated_note = ""
        if len(targets) > _MAX_FLEET_TARGETS:
            truncated_note = (
                f"\n[提示] 传入 {len(targets)} 台，超出单批上限 {_MAX_FLEET_TARGETS}，"
                f"已截断为前 {_MAX_FLEET_TARGETS} 台；其余请分批扇出。"
            )
            targets = targets[:_MAX_FLEET_TARGETS]
        try:
            top = int(top)
        except (TypeError, ValueError):
            top = 3
        top = max(1, min(top, 20))

        bridge = ElectronBridge()
        if not bridge.is_available():
            return ToolResult(False, "", "拒绝执行：Electron Bridge 未就绪（fail-closed）")
        try:
            connections = await bridge.list_connections()
        except BridgeError as e:
            return ToolResult(False, "", f"无法获取连接配置: {e}", priority="high")
        by_id = {c.get("id"): c for c in connections if c.get("id")}

        unknown = [t for t in targets if t not in by_id]
        valid = [t for t in targets if t in by_id]
        if not valid:
            return ToolResult(
                False, "",
                "targets 全部不是有效的连接配置 id。先用 fleet_sessions 查看可用目标。",
                priority="high",
            )

        command = sys_overview_cmd(top)   # 与 sys_overview 同源模板 → 白名单已覆盖
        results = await asyncio.gather(
            *[self._collect_one(bridge, by_id[t], command) for t in valid]
        )

        return self._compose(valid, unknown, results, truncated_note)

    async def _collect_one(self, bridge: ElectronBridge, conn: dict, command: str):
        """单台采集：建临时会话 → exec → finally 断开。逐台隔离异常。"""
        session = None
        try:
            connected = await bridge.connect_ssh(conn["id"], timeout=30.0)
            session = connected.get("sessionId") if isinstance(connected, dict) else None
            if not session:
                return (conn, False, "建连失败（未返回会话 id）", "")
            from agent.tools.builtin.ops_common import exec_remote
            ok, output, _rc, err = await exec_remote(session, command, _COLLECT_TIMEOUT)
            if err:
                return (conn, False, f"采集异常: {err}", "")
            if not ok:
                # 命令到了远端但失败 —— 带回部分输出供排查
                return (conn, False, "观测命令执行失败", output)
            return (conn, True, "", output)
        except Exception as e:
            return (conn, False, f"采集异常: {e}", "")
        finally:
            if session:
                try:
                    await bridge.disconnect_ssh(session, timeout=10.0)
                except Exception as e:
                    # 断连失败只留痕（会话由 S0-06 归属机制最终可回收）
                    logger.warning("临时会话 %s 断开失败: %s", session, e)

    def _compose(self, valid: list, unknown: list, results: list, note: str) -> ToolResult:
        lines: list[str] = []
        status_rows: list[str] = []
        ok_count = 0
        for (conn, ok, err, output) in results:
            name = conn.get("name") or conn.get("id")
            if ok:
                ok_count += 1
                m = _parse_overview(output)
                status_rows.append(
                    f"{name}  ok  load1={_flag(m['load1'], _THRESHOLD_LOAD1)}"
                    f"  内存%={_flag(m['mem_pct'], _THRESHOLD_MEM_PCT)}"
                    f"  磁盘max%={_flag(m['disk_max'], _THRESHOLD_DISK_PCT)}"
                )
            else:
                status_rows.append(f"{name}  ❌  ({err})")
        for t in unknown:
            status_rows.append(f"{t}  ❌  (连接配置不存在)")

        lines.append(f"=== 扇出对比（成功 {ok_count}/{len(results)}）===")
        lines.extend(status_rows)
        flagged = [r for r in status_rows if "⚠" in r]
        if flagged:
            lines.append("")
            lines.append("⚠ 超阈值主机（负载>%.0f 或 内存%%>%.0f 或 磁盘%%>%.0f）：" % (
                _THRESHOLD_LOAD1, _THRESHOLD_MEM_PCT, _THRESHOLD_DISK_PCT))
            lines.extend(f"  - {r}" for r in flagged)
        lines.append("")
        lines.append("=== 各机观测明细 ===")
        for (conn, ok, err, output) in results:
            name = conn.get("name") or conn.get("id")
            lines.append(f"### {name} ({conn.get('host') or '?'}) [{conn.get('environment') or '未知'}]")
            if ok:
                lines.append(output or "(空输出)")
            else:
                lines.append(f"采集失败: {err}")
                if output:
                    lines.append(output)
            lines.append("")
        if note:
            lines.append(note)
        text = "\n".join(lines).strip()
        # 部分失败不算整体失败（观测数据本身有效）；全失败才报失败
        all_failed = ok_count == 0
        return ToolResult(
            success=not all_failed,
            output=text if not all_failed else "",
            error="所有目标均采集失败（见输出明细）" if all_failed else "",
            priority="normal" if not all_failed else "high",
        )
