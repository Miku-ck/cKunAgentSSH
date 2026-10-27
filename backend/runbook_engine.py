"""运维剧本执行引擎 — 逐步骤驱动、每步一次无头 run、断言由 executor 确定性判读。

选型（docs/p0-ops-agent-enhancements.md §3.1）：否决「整段交给 agent」—— 断言失败
即停会退化成 prompt 约束、无调用方硬停点、步骤进度不可靠。每步发一次轻量无头 run
（完整 Harness 治理链：reins → audit 落链，经 agent.headless_run 装配，母本
schedule_engine._run_agent_once），executor 用 [步骤结论] 标记行正则判读（复刻巡检
先例 _INSPECT_VERDICT_RE / _extract_inspect_verdict，schedule_engine.py:58-92），
判读零新增 LLM 调用、不依赖模型自觉。

fail-closed 语义（本引擎的三条铁律）：
1. 步骤 run 未输出「[步骤结论] 通过」标记行 → 一律判失败（缺标记 = 失败，不是
   跳过更不是成功）；done 终态非 completed 同判失败（S7-01 同款：终态是收敛与否
   的唯一权威，被腰斩的 run 不因正文里有「通过」二字就算成功）。
2. 步骤 run 期间出现 permission_needed（headless await_approval=False 下的拦截
   信号，卡片无人可点、命令被拦即返回）→ 步骤判 blocked —— 仍是失败，转诊断，
   **绝不当作执行成功**。
3. 任一步骤失败 → 终止后续步骤（置 skipped）→ 自动发起一次诊断 run（无头），
   结论落 runbook_runs.diagnosis。

执行代价（明示）：N 步 = N 次 LLM 调用（确定性判停的固有代价，≤20 步封顶）；
SSH 建连收敛为每执行 1 次 —— open_session 一次、跨步骤复用同一 ssh_session_id、
finally 统一断开（headless_run 预建会话模式）。

并发（S7-05 教训，schedule_engine.py:38-42）：per-runbook _executing 防重入 +
全局并发闸 4（与 _MAX_CONCURRENT_SCHEDULE_RUNS 同值同理由 —— ssh-manager 的
SESSION_LIMIT 会被无上限的并发无头 run 顶穿）。

防滞留两道收尾：启动收尸 reap_stale_runs（schedule_engine.py:191-214 先例）+
CancelledError 落终态（:419-436 先例）。后端重启后残留的 running 行在下次触发前
被收尸为 stale_reaped，前端按钮不再永久置灰。
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid

import runbook_store as rstore
from agent import headless_run

logger = logging.getLogger(__name__)

# 全局并发闸（与 schedule_engine._MAX_CONCURRENT_SCHEDULE_RUNS 同值同注释）：
# 每个剧本执行占 1 条 SSH 会话 + 一串模型调用，无上限的并发会顶穿 SESSION_LIMIT
_MAX_CONCURRENT_RUNBOOK_RUNS = 4
# 单步 run 的迭代上限：单步骤任务「执行命令 + 验证 + 出标记」不需要长循环
_STEP_MAX_ITERATIONS = 8
# 失败现场 / 诊断结论的落库截断（防超长输出撑爆 steps_state / diagnosis 列）
_FAIL_SCENE_MAX_CHARS = 1500
_DIAGNOSIS_MAX_CHARS = 4000

# [步骤结论] 标记行（与巡检 [巡检结论] 同族：取最后一条命中，允许多次输出）
#   [步骤结论] 通过
#   [步骤结论] 失败: <一句话差异说明>
# 分隔用 [ \t]* 而非 \s*：\s 会跨行，把下一行正文吞进差异说明组（结论行必须是
# 单独一行 —— 换行后的文本不是结论的一部分，吞进来会污染 verdict/诊断现场）。
_RUNBOOK_VERDICT_RE = re.compile(
    r"^\s*\[步骤结论\]\s*(通过|失败)[ \t]*[:：]?[ \t]*(.*)$", re.MULTILINE
)

_VERDICT_HINT = (
    "标记行约定（系统据此自动判读本步骤成败）：请在回复的最末尾单独一行输出固定标记，"
    "严格按以下格式，单独成行、不要加任何前后缀：\n"
    "成功时输出：[步骤结论] 通过\n"
    "失败时输出：[步骤结论] 失败: <一句话差异说明>\n"
    "未输出标记行或格式不符将被系统判为失败。"
)


def extract_verdict(text: str) -> tuple[str, str] | None:
    """提取最后一个 [步骤结论] 标记行 → (结论, 摘要)；无标记返回 None（fail-closed）。"""
    last = None
    for m in _RUNBOOK_VERDICT_RE.finditer(text or ""):
        last = m
    if not last:
        return None
    return last.group(1), (last.group(2) or "").strip()


def build_step_prompt(step: dict) -> str:
    """步骤 run 的固定模板：只做这一件事 + 执行验证命令 + 输出标记行。"""
    parts = [
        "[步骤任务] 你在执行一条运维剧本的步骤，只完成下面这一件事，"
        "不要自由发挥执行其他变更、不要额外「顺手」操作：",
        f"命令：{step.get('command') or ''}",
    ]
    assert_cmd = (step.get("assertCommand") or "").strip()
    if assert_cmd:
        parts.append(f"验证命令：{assert_cmd}\n（主命令执行完后必须执行它，用真实输出判定成败）")
    note = (step.get("note") or "").strip()
    if note:
        parts.append(f"步骤说明：{note}")
    parts.append(_VERDICT_HINT)
    return "\n\n".join(parts)


def build_diagnosis_prompt(step: dict, fail_scene: str) -> str:
    """诊断 run 的固定模板：携带失败步骤的完整现场，只分析不执行。"""
    return (
        "[诊断任务] 一条运维剧本的步骤刚刚执行失败。请基于下面的失败现场分析原因，"
        "给出排查步骤与修复建议。本轮只做分析，不要执行任何变更命令；"
        "确需取证可以执行只读命令。\n\n"
        f"失败步骤命令：{step.get('command') or ''}\n"
        f"验证命令：{(step.get('assertCommand') or '').strip() or '（无）'}\n"
        f"步骤说明：{(step.get('note') or '').strip() or '（无）'}\n\n"
        f"失败现场（该步骤 run 的最终输出节选）：\n{fail_scene}"
    )


class RunbookEngine:
    """后台剧本执行器（单例）。只做手动触发（定时触发 defer 到 v2）。"""

    def __init__(self):
        self._executing: set[str] = set()  # 正在执行的 runbook_id，防重入
        # 删除占位集：DELETE 的「同步检查+占位」与 trigger 的「同步检查+登记」
        # 在单线程事件循环里互斥，堵住「检查后、删除前并发 trigger 建行被
        # FK CASCADE 静默吞掉」的 TOCTOU 窗口（评审意见 3）
        self._deleting: set[str] = set()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._run_slots = asyncio.Semaphore(_MAX_CONCURRENT_RUNBOOK_RUNS)
        self._reaped = False  # 本进程已收尸一次（与 schedule 引擎的循环启动收尸同语义）
        # 后台 best-effort 任务强引用集合（与 schedule_engine._bg_tasks 同纪律）
        self._bg_tasks: set[asyncio.Task] = set()

    async def ensure_reaped(self) -> None:
        """首次触发前收尸一次：把上个进程遗留的 running 行落 stale_reaped。

        必须在建本次 run 行之前 await 完成（reap 的排除集只保护登记在
        _executing 里的剧本，先收尸再建行则本行天然不受波及）。
        """
        if self._reaped:
            return
        self._reaped = True
        try:
            n = await asyncio.to_thread(
                rstore.reap_stale_runs, set(self._executing)
            )
            if n:
                logger.warning("runbook 引擎启动收尸：%d 条滞留 running 的执行已标 stale_reaped", n)
        except Exception as e:
            logger.warning("runbook 遗留 running 收尸失败: %s", e)

    def is_executing(self, runbook_id: str) -> bool:
        return runbook_id in self._executing

    def begin_delete(self, runbook_id: str) -> bool:
        """DELETE 占位（同步、无 await —— 事件循环内原子）。

        返回 False = 正在执行/已在删除中，路由据此回 409。占位后到 end_delete
        之间的所有 await 期间，trigger 会在自己的同步检查段被拒，不可能建出
        将被 CASCADE 吞掉的 run 行。
        """
        if runbook_id in self._executing or runbook_id in self._deleting:
            return False
        self._deleting.add(runbook_id)
        return True

    def end_delete(self, runbook_id: str) -> None:
        """释放删除占位（DELETE 收尾的 finally 里调用，幂等）。"""
        self._deleting.discard(runbook_id)

    async def trigger(self, runbook: dict) -> str | None:
        """触发一次执行。返回 runbook_run_id；None 表示正在执行（路由据此回 409）。

        去重标记必须落在第一个 await 之前（schedule_engine.trigger:277 的审查教训，
        本函数注释曾引用过却在实现上违背——评审实证过该竞态：并发双触发双双通过
        检查、同一剧本跑两份无头 run）。检查与登记之间不得有任何 await，事件循环
        单线程下这一同步段才是原子的；首次触发的收尸（ensure_reaped 内部
        to_thread 真实挂起）就在这个 await 上，登记必须先于它。
        """
        rid = runbook["id"]
        if rid in self._executing or rid in self._deleting:
            logger.info("runbook=%s 正在执行或删除中，跳过重复触发", rid)
            return None
        self._executing.add(rid)
        await self.ensure_reaped()
        try:
            rrb = await asyncio.to_thread(rstore.create_runbook_run, rid)
        except Exception:
            self._executing.discard(rid)
            raise
        try:
            t = asyncio.get_running_loop().create_task(self._run_task(runbook.copy(), rrb["id"]))
            self._running_tasks[rid] = t
        except Exception:
            # 无运行循环或 create_task 失败：回滚标记并落终态，避免行永久 running
            self._executing.discard(rid)
            self._running_tasks.pop(rid, None)
            try:
                await asyncio.to_thread(
                    rstore.finish_runbook_run, rrb["id"], "failed", error="触发失败",
                )
            except Exception:
                pass
            raise
        return rrb["id"]

    async def stop_all(self) -> None:
        """取消全部在途执行并等待其落定终态（测试 / 进程关停用）。"""
        if not self._running_tasks:
            return
        for t in self._running_tasks.values():
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
        self._running_tasks.clear()

    # ── 执行体 ──
    async def _run_task(self, runbook: dict, rrb_id: str) -> None:
        """带全局闸门的入口：排队而不是丢弃，超出槽位的执行错峰开跑（S7-05）。

        等待槽位期间 runbook_runs 行已存在（trigger 先建行再 create_task）——若在
        等待期被 cancel，inner 的 finally 没跑过，得在这里自己落终态并回收标记；
        inner 的 finally 做同样的清理，两处都是幂等操作。
        """
        rid = runbook["id"]
        try:
            async with self._run_slots:
                await self._run_task_inner(runbook, rrb_id)
        except asyncio.CancelledError:
            self._executing.discard(rid)
            self._running_tasks.pop(rid, None)
            try:
                await asyncio.to_thread(
                    rstore.finish_runbook_run, rrb_id, "stopped", error="执行被取消",
                )
            except Exception:
                pass
            raise

    async def _run_task_inner(self, runbook: dict, rrb_id: str) -> None:
        rid = runbook["id"]
        steps: list[dict] = runbook.get("steps") or []
        connection_id = (runbook.get("target_host") or "").strip()

        # 每步状态快照（即时持久化，重启后历史仍可看到停在哪一步）
        steps_state: list[dict] = [
            {
                "seq": i,
                "status": "pending",
                "command": (s.get("command") or ""),
                "assertCommand": (s.get("assertCommand") or ""),
                "verdict": "",
                "run_id": "",
                "error": "",
            }
            for i, s in enumerate(steps)
        ]
        run_status = "success"
        run_error = ""
        diagnosis = ""
        diag_run_id = ""
        session_id = ""
        bridge = None
        opened_session = False

        async def _persist() -> None:
            await asyncio.to_thread(
                rstore.update_runbook_run, rrb_id, steps_state=steps_state,
            )

        try:
            # 初始 pending 快照先落库：执行中打开历史能看到步骤清单与进度
            await _persist()
            if not steps:
                # 路由校验保证 ≥1 步；这里防的是绕过校验的存量/手工数据 ——
                # 空剧本 fail-closed 判失败，绝不虚报成功
                await asyncio.to_thread(
                    rstore.finish_runbook_run, rrb_id, "failed",
                    error="剧本没有任何步骤，未执行", steps_state=steps_state,
                )
                await self._audit_run(runbook, "failed", "剧本没有任何步骤", "")
                return
            # 每执行 1 次建连：probe → connect 一次，跨步骤复用；无目标主机的剧本
            # 无会话可建（步骤命令按工具自身 fail-closed 语义失败）
            if connection_id:
                session_id, bridge = await headless_run.open_session(
                    connection_id, log_tag=f"runbook={rid}")
                opened_session = True

            for i, step in enumerate(steps):
                st = steps_state[i]
                step_run_id = uuid.uuid4().hex[:12]
                st["run_id"] = step_run_id
                await asyncio.to_thread(
                    rstore.update_runbook_run, rrb_id,
                    run_id=step_run_id, current_step=i, steps_state=steps_state,
                )
                step_res: headless_run.HeadlessRunResult | None = None
                step_err = ""
                try:
                    step_res = await headless_run.run_headless_once(
                        build_step_prompt(step),
                        run_id=step_run_id,
                        conversation_id=f"runbook-{rid}",
                        ssh_session_id=session_id,  # 预建会话：跨步骤复用，不自建自关
                        max_iterations=_STEP_MAX_ITERATIONS,
                        log_tag=f"runbook={rid} step={i + 1}",
                    )
                except Exception as e:
                    step_err = f"{type(e).__name__}: {e}"

                if step_err:
                    st["status"] = "failed"
                    st["error"] = step_err[:500]
                else:
                    verdict = extract_verdict(step_res.content)
                    st["verdict"] = (
                        f"{verdict[0]}: {verdict[1]}" if verdict and verdict[1] else (verdict[0] if verdict else "")
                    )
                    if step_res.permission_requested:
                        # 拦截信号优先于模型结论（铁律 2）：headless 出现过
                        # permission_needed = 步骤内有命令被拦、实际未执行，
                        # 模型一句「[步骤结论] 通过」洗不白 —— 判 blocked 转诊断，
                        # 绝不当成功（评审实证过「拦截+通过标记」被洗成 success 的
                        # 静默假成功，这是运维执行功能最坏的失败形态）。
                        st["status"] = "blocked"
                        st["error"] = "命令被拦截（无人值守不允许未授权操作）"
                    elif step_res.state and step_res.state != "completed":
                        st["status"] = "failed"
                        st["error"] = f"run 未正常收敛（终态 {step_res.state}）"
                    elif verdict and verdict[0] == "通过":
                        st["status"] = "success"
                    elif verdict:
                        st["status"] = "failed"
                        st["error"] = (verdict[1] or "模型自判失败")[:500]
                    else:
                        # 缺标记 fail-closed 判失败
                        st["status"] = "failed"
                        st["error"] = "未输出 [步骤结论] 标记行（fail-closed 判失败）"

                await _persist()
                # 步骤级审计：conclusion 只用 LEGAL_CONCLUSIONS 既有值
                # （executed/failed/blocked；新值会被静默改写为 executed，audit.py:265-275）
                await self._audit_step(runbook, st, step_res)

                if st["status"] == "success":
                    continue

                # ── 失败即停 ──
                run_status = "failed"
                run_error = f"第 {i + 1} 步失败：{st['error']}"
                for j in range(i + 1, len(steps_state)):
                    steps_state[j]["status"] = "skipped"
                await _persist()
                # 自动诊断 run（第二次专用无头 run，prompt 携带失败现场）
                diag_run_id = uuid.uuid4().hex[:12]
                diagnosis = await self._run_diagnosis(
                    runbook, step, step_res, st, diag_run_id, session_id,
                )
                break

            if run_status == "success":
                await asyncio.to_thread(rstore.finish_runbook_run, rrb_id, "success")
            else:
                await asyncio.to_thread(
                    rstore.finish_runbook_run, rrb_id, "failed",
                    error=run_error, diagnosis=diagnosis, steps_state=steps_state,
                    # run_id 语义（设计 §3.2）：最后一个无头 run 的 run_id ——
                    # 失败场景最后一个是诊断 run，历史页按 run_id 追现场应指到
                    # 诊断 run 而非失败步骤（diag_run_id 为空时 COALESCE 保留
                    # 失败步骤的 run_id，由逐步回写已落库）
                    run_id=diag_run_id or None,
                )
            logger.info("runbook=%s rrb=%s %s", rid, rrb_id, run_status)
            # 剧本级审计（细节放 result 字段，conclusion 仍只用白名单既有值）
            await self._audit_run(runbook, run_status, run_error, diagnosis)
        except asyncio.CancelledError:
            # 关停/取消 —— 落终态后再抛，否则 runbook_runs 永久滞留 running
            # （schedule_engine.py:419-436 同款收尾）
            logger.warning("runbook=%s rrb=%s 执行被取消", rid, rrb_id)
            try:
                await asyncio.to_thread(
                    rstore.finish_runbook_run, rrb_id, "stopped",
                    error="执行被取消", steps_state=steps_state,
                )
            except Exception:
                pass
            raise
        except Exception as e:
            logger.exception("runbook=%s rrb=%s 执行失败", rid, rrb_id)
            try:
                await asyncio.to_thread(
                    rstore.finish_runbook_run, rrb_id, "failed",
                    error=f"{type(e).__name__}: {e}", steps_state=steps_state,
                )
            except Exception:
                pass
        finally:
            # 预建会话由执行体统一断开（opened_session 才关，与 headless_run 的
            # 自建自关语义互补，两处标记互斥不会双重关闭）
            if opened_session and session_id:
                await headless_run.close_session(
                    bridge, session_id, log_tag=f"runbook={rid}")
            self._executing.discard(rid)
            self._running_tasks.pop(rid, None)
            # 清理本执行各 run 的内存 todo（无头路径不过交互式清理钩子，
            # 与 schedule_engine._run_task_inner finally 同纪律）
            from agent.tools.builtin.todo_write import clear_todos
            seen_run_ids = set()
            for st in steps_state:
                if st.get("run_id"):
                    seen_run_ids.add(st["run_id"])
            if diag_run_id:
                seen_run_ids.add(diag_run_id)
            for r in seen_run_ids:
                try:
                    clear_todos(r)
                except Exception:
                    pass

    async def _run_diagnosis(
        self,
        runbook: dict,
        step: dict,
        step_res: headless_run.HeadlessRunResult | None,
        st: dict,
        diag_run_id: str,
        session_id: str,
    ) -> str:
        """失败步骤的自动诊断：一次专用无头 run，结论截断落 diagnosis。全程自吞异常。"""
        fail_scene_parts = []
        if st.get("error"):
            fail_scene_parts.append(f"失败原因：{st['error']}")
        if step_res is not None and step_res.content:
            fail_scene_parts.append(step_res.content[:_FAIL_SCENE_MAX_CHARS])
        if not fail_scene_parts:
            fail_scene_parts.append("（无输出）")
        prompt = build_diagnosis_prompt(step, "\n".join(fail_scene_parts))
        try:
            res = await headless_run.run_headless_once(
                prompt,
                run_id=diag_run_id,
                conversation_id=f"runbook-{runbook['id']}",
                ssh_session_id=session_id,
                max_iterations=_STEP_MAX_ITERATIONS,
                log_tag=f"runbook={runbook['id']} diagnose",
            )
            return (res.content or "").strip()[:_DIAGNOSIS_MAX_CHARS]
        except Exception as e:
            logger.warning("runbook=%s 诊断 run 失败: %s", runbook["id"], e)
            return f"诊断 run 执行失败: {type(e).__name__}: {e}"[:_DIAGNOSIS_MAX_CHARS]

    async def _audit_step(
        self,
        runbook: dict,
        st: dict,
        step_res: headless_run.HeadlessRunResult | None,
    ) -> None:
        """步骤级审计事件（append_record_async，critical=False：失败静默不阻断执行）。"""
        try:
            from agent.audit import append_record_async
            conclusion = (
                "executed" if st["status"] == "success"
                else "blocked" if st["status"] == "blocked"
                else "failed"
            )
            await append_record_async(
                action="runbook_step",
                conclusion=conclusion,
                run_id=st.get("run_id") or "",
                target_type="runbook",
                target_id=runbook.get("id") or "",
                # 建连前失败的步骤拿不到已解析环境：留空交审计默认，不硬编码 "test"
                #（那会把 prod 剧本的失败步骤记成测试环境，审计维度失真）
                environment=(step_res.environment if step_res else "") or "",
                command_raw=st.get("command") or "",
                result=(f"step={st.get('seq', 0) + 1} verdict={st.get('verdict') or '-'}"
                        f" status={st['status']} error={st.get('error') or '-'}")[:500],
            )
        except Exception as e:
            logger.warning("runbook 步骤审计写入失败（不阻断执行）: %s", e)

    async def _audit_run(
        self, runbook: dict, run_status: str, run_error: str, diagnosis: str,
    ) -> None:
        """剧本级审计事件（一次执行一行，收尾时写入）。"""
        try:
            from agent.audit import append_record_async
            await append_record_async(
                action="runbook_run",
                conclusion="executed" if run_status == "success" else "failed",
                target_type="runbook",
                target_id=runbook.get("id") or "",
                command_raw=runbook.get("name") or "",
                result=(f"status={run_status} error={run_error or '-'}"
                        f" diagnosis={(diagnosis or '-')[:200]}")[:500],
            )
        except Exception as e:
            logger.warning("runbook 剧本级审计写入失败（不阻断收尾）: %s", e)


# 全局单例
_engine: RunbookEngine | None = None


def get_runbook_engine() -> RunbookEngine:
    global _engine
    if _engine is None:
        _engine = RunbookEngine()
    return _engine
