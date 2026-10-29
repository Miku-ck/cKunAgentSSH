"""
任务列表工具 — Agent 任务管理

提供任务列表的增删改查能力，用于 Agent 在执行多步骤任务时
维护自己的待办清单。任务列表按 agent_run_id 隔离存储在内存中
（模块级 dict），不落盘。
"""
import time
import uuid

from agent.tools import BaseTool, ToolResult, ToolMeta, ToolContext

# 模块级存储（每个 agent run 一个独立 todo 列表）
# key: agent_run_id, value: list[dict]
_todo_store: dict[str, list[dict]] = {}

# 单个 run 的任务数上限，防止内存膨胀
_MAX_TODOS_PER_RUN = 100

# 状态与长度上限的唯一来源：schema 的 enum、_update 的校验都从这里派生，
# 免得出现「schema 收 3 个值、代码放行任意字符串」的口径漂移
_STATUSES = ("pending", "in_progress", "completed")

# 每条任务的内容上限：create/update 都会把整张表回显进上下文，
# 单条不限长度 × 100 条会让一次工具调用塞进几十 K token
_MAX_CONTENT_CHARS = 200


def clear_todos(run_id: str) -> None:
    """清理指定 run_id 的任务列表（Agent 运行结束后调用，防止内存泄漏）"""
    _todo_store.pop(run_id, None)


def _get_todos(run_id: str) -> list[dict]:
    """获取指定 run_id 的任务列表（不存在则初始化为空列表）"""
    if run_id not in _todo_store:
        _todo_store[run_id] = []
    return _todo_store[run_id]


def _content_too_long_error(content: str) -> str | None:
    if len(content) > _MAX_CONTENT_CHARS:
        return (f"任务内容需 ≤{_MAX_CONTENT_CHARS} 字符，收到 {len(content)}；"
                "请改写成一句话待办（细节留在自己的推理里，不要塞进任务项）")
    return None


def _format_todos(todos: list[dict]) -> str:
    """格式化任务列表为文本

    格式：
        [1] [in_progress] 实现用户登录功能
        [2] [pending] 编写测试用例
        [3] [completed] 设计数据库 schema
    """
    if not todos:
        return "（无任务）"
    lines = []
    for idx, todo in enumerate(todos, start=1):
        status = todo.get("status", "pending")
        content = todo.get("content", "")
        lines.append(f"[{idx}] [{status}] {content}")
    return "\n".join(lines)


class TodoWriteTool(BaseTool):
    """任务列表管理工具

    支持 create / update / delete / list / clear 五种操作。
    任务列表按 agent_run_id 隔离，存储在模块级内存 dict 中。
    当 ToolContext 无 agent_run_id 时降级为无操作工具（list 返回空列表）。
    """

    name = "todo_write"
    description = (
        "管理 Agent 的任务列表，支持创建、更新、删除、列出、清空任务项。"
        "用于多步骤任务规划与进度跟踪。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "delete", "list", "clear"],
                "description": "操作类型：create 创建新任务；update 更新任务；delete 删除任务；list 列出全部；clear 清空全部",
            },
            "id": {
                "type": "string",
                "description": "任务 ID（update / delete 时必填）",
            },
            "content": {
                "type": "string",
                "description": "任务内容（create 时必填，update 时可选）",
            },
            "status": {
                "type": "string",
                "enum": list(_STATUSES),
                "description": "任务状态（update 时可选）",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=False,
        cacheable=False,
    )

    async def execute(self, context: ToolContext, action: str,
                      id: str = None, content: str = None,
                      status: str = None) -> ToolResult:
        """执行任务列表操作

        参数:
            context: 工具执行上下文（用于获取 agent_run_id）
            action: 操作类型（create/update/delete/list/clear）
            id: 任务 ID（update/delete 时必填）
            content: 任务内容（create 必填，update 可选）
            status: 任务状态（update 可选）

        返回:
            ToolResult，output 为格式化后的任务列表文本
        """
        # 无 agent_run_id 时降级为无操作工具
        run_id = context.agent_run_id
        if not run_id:
            if action == "list":
                return ToolResult(True, "（无任务）", priority="normal")
            return ToolResult(
                True,
                "（无 agent_run_id，todo 工具降级为无操作）",
                priority="low",
            )

        todos = _get_todos(run_id)

        if action == "create":
            return self._create(todos, content)
        elif action == "update":
            return self._update(todos, id, content, status)
        elif action == "delete":
            return self._delete(todos, id)
        elif action == "list":
            return ToolResult(True, _format_todos(todos), priority="normal")
        elif action == "clear":
            todos.clear()
            return ToolResult(True, "任务列表已清空", priority="normal")
        else:
            return ToolResult(
                False, "", f"未知 action: {action}", priority="high"
            )

    def _create(self, todos: list[dict], content: str) -> ToolResult:
        """创建新任务"""
        if not content:
            return ToolResult(
                False, "", "create 操作需要 content 参数", priority="high"
            )
        too_long = _content_too_long_error(content)
        if too_long:
            return ToolResult(False, "", too_long, priority="high")
        # 任务数上限保护，防止内存膨胀
        if len(todos) >= _MAX_TODOS_PER_RUN:
            return ToolResult(
                False, "",
                f"任务数已达上限 {_MAX_TODOS_PER_RUN}，拒绝新增",
                priority="high",
            )
        # 生成 ID：todo_{timestamp_ms}_{random_hex}
        todo_id = f"todo_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        todo = {
            "id": todo_id,
            "content": content,
            "status": "pending",
        }
        todos.append(todo)
        return ToolResult(
            True,
            f"已创建任务: {todo_id}\n" + _format_todos(todos),
            priority="normal",
        )

    def _update(self, todos: list[dict], todo_id: str,
                content: str, status: str) -> ToolResult:
        """更新已有任务（content 和/或 status）"""
        if not todo_id:
            return ToolResult(
                False, "", "update 操作需要 id 参数", priority="high"
            )
        if not content and not status:
            return ToolResult(
                False, "",
                "update 操作至少需要 content 或 status 之一",
                priority="high",
            )
        # 声明了 enum 不等于收到的就在 enum 里：脏状态会被原样渲染回模型
        if status and status not in _STATUSES:
            return ToolResult(
                False, "",
                f"status 必须是 {'/'.join(_STATUSES)} 之一，收到 {status!r}",
                priority="high",
            )
        if content:
            too_long = _content_too_long_error(content)
            if too_long:
                return ToolResult(False, "", too_long, priority="high")
        idx = self._find(todos, todo_id)
        if idx < 0:
            return ToolResult(
                False, "", f"未找到任务: {todo_id}", priority="high"
            )
        target = todos[idx]
        if content:
            target["content"] = content
        if status:
            target["status"] = status
        return ToolResult(
            True,
            f"已更新任务: {todo_id}\n" + _format_todos(todos),
            priority="normal",
        )

    @staticmethod
    def _find(todos: list[dict], todo_id: str) -> int:
        """按 ID 定位任务下标，未找到返回 -1（update / delete 共用）"""
        for i, t in enumerate(todos):
            if t["id"] == todo_id:
                return i
        return -1

    def _delete(self, todos: list[dict], todo_id: str) -> ToolResult:
        """按 ID 删除任务"""
        if not todo_id:
            return ToolResult(
                False, "", "delete 操作需要 id 参数", priority="high"
            )
        idx = self._find(todos, todo_id)
        if idx < 0:
            return ToolResult(
                False, "", f"未找到任务: {todo_id}", priority="high"
            )
        del todos[idx]
        return ToolResult(
            True,
            f"已删除任务: {todo_id}\n" + _format_todos(todos),
            priority="normal",
        )
