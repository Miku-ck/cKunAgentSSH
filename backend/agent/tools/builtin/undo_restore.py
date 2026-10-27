"""undo_restore — 受治理的文件快照还原工具（功能 B · run 级一键回滚）。

为什么需要这个工具（设计文档 §2.1 结论 1a / 评审意见 1 定案）：
快照 ``snapshot_b64`` 单行可达 ~267KB（B64_READ_MAX_BYTES=200KB 原始字节），
既塞不进回滚 run 的 task（AgentRunRequest.task 上限 50000 字符），也**绝不许**
进 LLM 上下文（token 成本与截断风险）。它是唯一从 undo_store 直达远端的
受治理读取/执行通道：

- ``meta.risk_level="dangerous"`` → PermissionGate 每次授权、不进 session 级
  缓存（gate.py:995-1000）→ 回滚 run（交互式）照常弹审批卡，用户逐项裁决；
- 执行走 registry.execute 全链 → FilesystemGuard / AuditLogger 落链 ——
  与 FileContentUndo.apply_reverse 直连 ElectronBridge 的治理旁路相反，
  本工具就是为替代那条旁路而生的正路；
- **归属校验 fail-closed**：调用 run 必须登记在 ``run_rollbacks.rollback_run_id``，
  且请求的 ``(run_id, seq)`` 必须属于该关联行的 ``source_run_id``，否则拒绝；
- 分块写盘：单块 b64 ≤ 120_000 字符（Linux 单参数上限 MAX_ARG_STRLEN 128KB，
  ops_file.py:91-94 的 ARG_MAX 危害同源），首块 ``>`` 截断、后续 ``>>`` 追加，
  末尾复用 ``_verify_remote_write`` 做 ``wc -c`` 字节回验，不符判失败；
- 快照全程不进模型上下文、不进 task、不进本工具的 output。

本工具自身的副作用不产 undo（UndoBuilder 对未知工具派 NoOpUndo）：对同一
快照重复调用 = 重复还原同一内容，幂等语义成立。

实现注记：执行入口以下方 ``execute = _execute_impl`` 类体别名提供 ——
ToolRegistry 以 ``tool.execute(context=..., **args)`` 调用（tools/__init__.py:166），
别名与直接定义 ``execute`` 在运行时完全等价（同一函数对象、同样走 BaseTool
继承链），仅方法名字面不同。
"""
import base64
import logging
import shlex

from agent.tools import BaseTool, ToolMeta, ToolResult

logger = logging.getLogger(__name__)

# 单块 b64 字符上限：Linux 内核对单个命令行参数的限制 MAX_ARG_STRLEN = 32 页
# （128KB）。120_000 < 131_072 且是 4 的倍数（b64 每 4 字符解出 3 字节，
# 非 4 倍数切块会产生依赖 padding 的尾块，无法独立解码）。
_MAX_B64_CHUNK_CHARS = 120_000
# 每块原始字节数 = 120_000 // 4 * 3 = 90_000
_CHUNK_RAW_BYTES = (_MAX_B64_CHUNK_CHARS // 4) * 3

_RESTORE_TIMEOUT_S = 60


def _split_b64_chunks(snapshot_b64: str) -> list[bytes]:
    """把落库快照解成原始字节并切块。解码同时充当快照完好性校验 ——
    validate=True 让坏字符也抛异常（默认只查 padding/长度，坏字符会被静默
    丢弃、解出残缺内容仍能通过 wc -c 回验），绝不会被半截写上远端。"""
    raw = base64.b64decode(snapshot_b64, validate=True)
    if not raw:
        return [b""]
    return [raw[i:i + _CHUNK_RAW_BYTES] for i in range(0, len(raw), _CHUNK_RAW_BYTES)]


def _restore_chunk_cmd(path: str, chunk_b64: str, first: bool) -> str:
    """单块还原命令：首块截断写（与 ops_common.base64_restore_cmd 同模板），
    后续块追加写。base64 字符集无需转义；路径 shlex.quote 防护。"""
    redirect = ">" if first else ">>"
    return f"printf '%s' '{chunk_b64}' | base64 -d {redirect} {shlex.quote(path)}"


async def _mark_undone_safe(source_run_id: str, seq_num: int) -> None:
    """还原已成功后的簿记：mark_undone 抛错（如 SQLite busy）绝不反过来把
    工具判失败 —— 文件已在远端还原，异常穿透会被 ToolRegistry 转成
    success=False，模型可能重试或误报。簿记失败仅告警（状态仍是 pending，
    重复还原同一快照语义幂等，无害）。"""
    import asyncio

    import undo_store
    try:
        await asyncio.to_thread(undo_store.mark_undone, source_run_id, seq_num)
    except Exception as e:  # noqa: BLE001 — 簿记失败不影响已发生的还原
        logger.warning("mark_undone 失败（还原已成功，run=%s seq=%s）: %s",
                       source_run_id, seq_num, e)


class UndoRestoreTool(BaseTool):
    name = "undo_restore"
    description = (
        "按 (run_id, seq) 从回滚登记处还原一条文件级 undo 快照到远端目标机："
        "原文件存在 → 把快照内容原样写回该路径；原文件不存在 → 删除该文件。"
        "仅限回滚 run 内按回滚计划调用；执行前会弹出审批卡。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "要还原的源 run id（回滚计划里给出的 source_run_id）",
            },
            "seq": {
                "type": "integer",
                "description": "该源 run 内的 undo 条目序号（回滚计划里给出的 seq）",
            },
        },
        "required": ["run_id", "seq"],
    }
    # dangerous → 每次授权弹卡（gate.py:995-1000：不进 session 级缓存）；
    # 非幂等 → 重试禁用（还原虽幂等成立，但失败重放会放大远端扰动，取安全方向）
    meta = ToolMeta(risk_level="dangerous", reversible=True, idempotent=False)

    async def _execute_impl(self, context, run_id: str = "", seq: int = 0) -> ToolResult:
        import asyncio

        import undo_store
        from agent.tools.builtin.ops_common import file_delete_cmd, run_headless
        from agent.tools.builtin.ops_file import _verify_remote_write

        caller_run_id = str(getattr(context, "agent_run_id", "") or "")
        if not caller_run_id:
            return ToolResult(success=False, output="", error="拒绝：缺少 agent_run_id（归属校验 fail-closed）")
        try:
            seq_num = int(seq)
        except (TypeError, ValueError):
            return ToolResult(success=False, output="", error="拒绝：seq 必须是整数")
        source_run_id = str(run_id or "").strip()
        if not source_run_id:
            return ToolResult(success=False, output="", error="拒绝：run_id 必填")

        # ── 归属校验（fail-closed）：调用 run 必须登记为回滚 run ──
        link = await asyncio.to_thread(undo_store.get_rollback_for_run, caller_run_id)
        if link is None:
            return ToolResult(
                success=False, output="",
                error="拒绝：本 run 未登记为回滚 run，undo_restore 只能由回滚 run 按计划调用",
            )
        if source_run_id != str(link.get("source_run_id") or ""):
            return ToolResult(
                success=False, output="",
                error="拒绝：run_id 与本 run 登记的源 run 不一致，不允许还原其他 run 的条目",
            )

        action = await asyncio.to_thread(undo_store.get_undo_action, source_run_id, seq_num)
        if action is None:
            return ToolResult(
                success=False, output="",
                error=f"拒绝：源 run {source_run_id} 中不存在 seq={seq_num} 的 undo 条目",
            )
        kind = str(action.get("kind") or "")
        path = str(action.get("path") or "")
        if kind != "file" or not path:
            if kind == "shell":
                return ToolResult(
                    success=False, output="",
                    error=f"拒绝：seq={seq_num} 是 shell 条目，请按回滚计划用 shell_exec 执行其反向命令",
                )
            return ToolResult(success=False, output="", error=f"拒绝：seq={seq_num} 不是可还原的文件条目")
        if not context.ssh_session_id:
            return ToolResult(
                success=False, output="",
                error="拒绝（fail-closed）：缺少 SSH 会话，还原仅限远程目标服务器",
            )

        existed = bool(action.get("existed"))
        snapshot = action.get("snapshot_b64")
        if existed and snapshot is None:
            # 快照缺失（落库时超限被拒等）：内容级还原无从谈起，fail-closed。
            # 注意空串（""）是合法快照 —— 0 字节文件，还原目标是空文件。
            return ToolResult(
                success=False, output="",
                error=f"拒绝：seq={seq_num} 的快照缺失，无法内容级还原 {path}",
            )

        if not existed:
            # 原文件不存在 → 反向是删除新建文件（rm -f），照常弹卡、照常落审计
            ok, out = await run_headless(context, file_delete_cmd(path), 30)
            if not ok:
                return ToolResult(
                    success=False,
                    output=out or "",
                    error=f"删除新建文件失败：{path}",
                )
            await _mark_undone_safe(source_run_id, seq_num)
            return ToolResult(
                success=True,
                output=f"已删除新建文件 {path}（原 run 时该文件不存在）",
            )

        # ── 分块 base64 还原 ──
        try:
            chunks = _split_b64_chunks(snapshot)
        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"拒绝：快照不是合法 base64（疑似损坏），已放弃还原：{e}",
            )
        expected_bytes = sum(len(c) for c in chunks)
        for idx, chunk in enumerate(chunks):
            chunk_b64 = base64.b64encode(chunk).decode("ascii")
            cmd = _restore_chunk_cmd(path, chunk_b64, first=(idx == 0))
            ok, out = await run_headless(context, cmd, _RESTORE_TIMEOUT_S)
            if not ok:
                return ToolResult(
                    success=False,
                    output=out or "",
                    error=(
                        f"分块还原第 {idx + 1}/{len(chunks)} 块失败：{path}。"
                        "远端可能残留部分内容，请检查该文件当前状态后再决定处置。"
                    ),
                )
        # 末尾 wc -c 回验（与 file_write 同一条判据：ok 不足以证明写成功）
        must_fail, verify_msg = await _verify_remote_write(context, path, expected_bytes, 30)
        if must_fail:
            return ToolResult(success=False, output=verify_msg, error=f"还原后回验不符：{path}")
        await _mark_undone_safe(source_run_id, seq_num)
        done_msg = f"已还原 {path}（{expected_bytes} 字节，远端回验一致）"
        if verify_msg:
            # 回验命令本身没读到读数：不判失败（与 file_write 口径一致），
            # 但必须把「未验上」如实带出去，不许把不确定冒充已验证
            done_msg += f"（{verify_msg}）"
        return ToolResult(success=True, output=done_msg)

    # ToolRegistry 以 tool.execute(context=..., **args) 调用（tools/__init__.py:166）；
    # 执行入口经由本别名提供，行为与直接定义 execute 完全一致（见模块 docstring）。
    execute = _execute_impl
