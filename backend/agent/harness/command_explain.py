"""
CommandExplain — 命令后果的中文说明层

定位：审批卡片的「可读性」层，不是安全判定层。
- 输入是 safety.classify_command 产出的英文 reason（如
  "recursive delete of root filesystem"），输出面向非专业用户的中文后果说明。
- 本模块的产出不参与任何放行/拦截决策（不进 _grant_key / check_permission），
  也不写入审计 result —— 审计链继续保留英文 reason 以维持历史数据一致性。
  因此解释缺失或失败只会让卡片文案变通用，绝不会让命令变成免审。

覆盖策略：静态后果表优先（确定性、零延迟、可测试全覆盖），
未命中一律回退通用文案；仅「未知命令默认审批」族标记 needs_llm，
由调用方决定是否额外发起一次带超时的 LLM 生成（见 maybe_explain_via_llm）。

失败姿态：解释是卡片的增益信息，任何失败都只保留静态文案，
绝不参与放行/拦截判定 —— 生成失败永远不会变成"免审放行"。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # 仅类型标注，避免运行时把 contracts 拉进 safety 依赖链
    from .contracts import RunContext

logger = logging.getLogger(__name__)

# 未命中后果表时的兜底文案（永不空白）
FALLBACK_TEXT = (
    "系统未能确定这条命令的具体用途，无法预知后果。"
    "请在下方核对命令原文，确认它确实是你要求的操作后再授权。"
)

# 后果文案里插入的命令片段上限（路径可能极长，避免撑爆卡片）
_MAX_TARGET_LEN = 120


@dataclass(frozen=True)
class ExplainResult:
    """一条命令的后果说明

    text:   中文后果文案，展示层可直接使用
    source: "static" 命中后果表 | "fallback" 通用兜底
    needs_llm: 该 reason 属于「未知命令」族，静态文案信息量不足，可交给 LLM 补充
    """
    text: str
    source: str
    needs_llm: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# 后果表 —— 键为 safety.py 产出的英文 reason（正则）
#
# 为什么不改 safety.py 的 (pattern, reason) 为三元组：
# reason 会进审计链（guards.py 的 result=f"hardline: {reason}"），历史数据已是
# 英文；且 scripts/deploy/agent-exec.py 另有一份 HARDLINE 表副本。改键面过广，
# 故此处按 reason 文本匹配，并由 test_command_explain.py 做「reason 全集必须命中」
# 的覆盖测试来防漂移。
#
# 约定：
# - 顺序敏感——更具体的规则排在前（首条命中即返回）。
# - 模板里的 {target} 取自 reason 尾部 (…) 分组，或规则自身命名组 (?P<target>…)。
# ──────────────────────────────────────────────────────────────────────────────
CONSEQUENCE_RULES: list[tuple[re.Pattern, str, bool]] = [
    # ── 删除 / 磁盘破坏 ──
    (re.compile(r"^recursive delete of root filesystem"),
     "递归删除整台机器的全部文件（{target}），系统会立即瘫痪，几乎无法恢复。", False),
    (re.compile(r"^recursive delete of home directory or \.ssh"),
     "递归删除用户主目录或 SSH 密钥目录（{target}），登录凭据会一并丢失，通常无法恢复。", False),
    (re.compile(r"^recursive delete of \.ssh under home"),
     "递归删除主目录下的 SSH 密钥目录（{target}），可能导致此后无法登录其他服务器。", False),
    (re.compile(r"^recursive delete of sensitive system path"),
     "递归删除系统关键目录下的内容（{target}），会破坏已安装的软件，甚至导致系统无法启动。", False),
    (re.compile(r"^recursive delete under home directory"),
     "递归删除主目录下的内容（{target}），用户数据会被删除且一般不会自动恢复。", False),
    (re.compile(r"^recursive delete of absolute path"),
     "递归删除该路径下的全部文件和子目录（{target}），没有备份就无法恢复。", False),
    (re.compile(r"^recursive delete from root"),
     "从根目录开始递归删除，影响范围可能覆盖整台机器，无法恢复。", False),
    (re.compile(r"^recursive delete with wildcard"),
     "用通配符递归删除文件，实际匹配范围可能远大于预期，删除后无法恢复。", False),
    (re.compile(r"^recursive delete without explicit target"),
     "递归删除但看不出删除目标，实际可能删到任意目录，风险无法评估。", False),
    (re.compile(r"^recursive delete with unknown \(xargs\) target"),
     "删除目标由 xargs 在运行时决定（{target}），无法提前判断会删掉哪些文件。", False),
    (re.compile(r"^delete of sensitive system path or home"),
     "删除系统关键目录或主目录下的文件（{target}），可能造成软件损坏或凭据丢失。", False),
    (re.compile(r"^find -delete/-exec on root filesystem"),
     "在整个文件系统范围内批量删除或执行命令，等同于全盘清除，无法恢复。", False),
    (re.compile(r"^find -delete/-exec on sensitive system path"),
     "在系统关键目录内批量删除或执行命令（{target}），影响面难以预估。", False),
    (re.compile(r"^format filesystem"),
     "格式化磁盘分区，分区内原有数据全部丢失，基本不可恢复。", False),
    (re.compile(r"^dd to raw block device"),
     "把数据直接写入裸磁盘设备（{target}），会覆盖设备原内容并破坏文件系统。", False),
    (re.compile(r"^dd writes non-standard device"),
     "dd 的输出目标不是普通文件而是设备（{target}），可能直接覆盖磁盘内容。", False),
    (re.compile(r"^dd reads sensitive system path"),
     "把系统敏感文件作为 dd 输入读取（{target}），内容可能随输出外泄。", False),
    (re.compile(r"^move to /dev/null"),
     "把文件移动到 /dev/null，内容会被立即丢弃且无法找回。", False),
    (re.compile(r"^redirect to raw block device"),
     "把命令输出重定向写入裸磁盘设备，会覆盖磁盘原有数据。", False),

    # ── 可用性中断 ──
    (re.compile(r"^fork bomb"),
     "启动自我复制的进程炸弹，会迅速耗尽系统资源，机器失去响应且只能强制重启。", False),
    (re.compile(r"^system shutdown|^system halt|^system poweroff|^init 0|^systemctl power action|^telinit 0/6"),
     "立即关机或断电，主机上所有业务与远程连接都会中断，通常需人工到场才能恢复。", False),
    (re.compile(r"^system reboot"),
     "立即重启主机，正在处理的任务与未保存数据会丢失。", False),
    (re.compile(r"^kill all processes"),
     "杀掉机器上几乎所有进程，业务会全面中断。", False),
    (re.compile(r"^kill PID 1"),
     "杀掉 PID 1（init/systemd），系统会立即崩溃或重启。", False),
    (re.compile(r"^dmesg ring buffer clear"),
     "清空内核日志缓冲区（dmesg -C），排查故障所需的内核证据会立即消失且无法找回。", False),
    (re.compile(r"^ifconfig interface state change"),
     "改动网卡状态或标志（{target}），把网卡置 down 会直接中断这台机器对外的连接，远程会话可能就此失联。", False),
    (re.compile(r"^hostname change"),
     "修改主机名（{target}），可能影响依赖主机名的服务注册、证书与日志归属。", False),

    # ── 权限与账户 ──
    (re.compile(r"^sudo command"),
     "以 root 最高权限执行命令，真实后果取决于后面的命令——它可以改动或删除机器上任何文件。", False),
    (re.compile(r"^switch user"),
     "切换到其他用户身份执行操作，可能绕过当前账号的权限限制。", False),
    (re.compile(r"^sudo password guessing"),
     "从标准输入向 sudo 投喂密码，属于凭据自动猜测/代填，可能误用他人密码。", False),
    (re.compile(r"^user account management"),
     "新增、修改或删除系统账户，可能创建出拥有高权限的账户（包括后门账户）。", False),
    (re.compile(r"^group creation"),
     "创建用户组，会改变账户的权限归属。", False),
    (re.compile(r"^password change \(passwd\)"),
     "修改账户口令，执行成功后旧密码立即失效。", False),
    (re.compile(r"^direct edit of sudoers/pw database"),
     "直接编辑 sudoers 或口令数据库，一旦写错可能锁死全部管理员权限。", False),
    (re.compile(r"^chmod on sensitive system path"),
     "修改系统关键文件的权限（{target}），设置不当会让系统失去保护甚至无法使用。", False),
    (re.compile(r"^chmod 777"),
     "把文件设为所有用户都可读写执行（777），任何进程都能篡改它。", False),
    (re.compile(r"^chmod octal mode"),
     "修改文件权限位，若开启全局可写或 setuid，会明显削弱系统防护。", False),
    (re.compile(r"^recursive chown"),
     "递归改变更目录下所有文件的属主，可能导致服务因权限错乱而无法启动。", False),
    (re.compile(r"^special file creation"),
     "创建设备节点/命名管道等特殊文件，这类文件常被用于隐藏和植入。", False),
    (re.compile(r"^kernel module operation"),
     "加载或卸载内核模块，错误的模块会让系统直接崩溃。", False),
    (re.compile(r"^sysctl runtime kernel parameter change"),
     "修改内核运行时参数，影响整机行为并立即生效，不一定能回退。", False),

    # ── 间接执行 / 来源不明 ──
    (re.compile(r"^curl pipe shell|^wget pipe shell"),
     "从网络下载脚本并立即执行，执行前看不到脚本内容，等同于盲目运行陌生人给的程序，无法撤销。", False),
    (re.compile(r"^base64 decode piped to interpreter"),
     "把 base64 编码后的内容直接交给解释器执行，真实命令被刻意隐藏，无法预判后果。", False),
    (re.compile(r"^netcat -e"),
     "用 netcat 建立反向 shell，会把本机命令行交给外部机器控制，是典型的后门特征。", False),
    (re.compile(r"^execute downloaded script with shell"),
     "用 shell 直接执行刚下载的脚本，内容不可预知，无法撤销。", False),
    (re.compile(r"^execute downloaded file|^execute file from temp dir after download"),
     "执行刚从网络下载到本机的程序，来源与内容均未经验证。", False),
    (re.compile(r"^chmod \+x on downloaded file"),
     "给刚下载的文件加上可执行权限，通常是准备运行来源不明的程序。", False),
    (re.compile(r"^interpreter/indirect execution"),
     "通过解释器间接执行代码（{target}），真实行为写在脚本内容里，静态检查看不到。", False),
    (re.compile(r"^awk/sed inline code execution"),
     "awk/sed 的 system() 一类的内联代码会另起进程执行，真实动作藏在脚本片段里，静态检查看不到。", False),
    (re.compile(r"^destructive command with indirect expansion"),
     "破坏性命令被 $()、反引号或变量展开包裹，真正要删改的对象要到运行时才确定，无法判断影响范围。", False),
    (re.compile(r"^sed in-place edit"),
     "原地改写文件内容（-i），原内容不保留，改错后难以恢复。", False),
    (re.compile(r"^journalctl log deletion"),
     "清理系统日志（--vacuum），排查故障所需的历史证据会被永久删除。", False),

    # ── 凭据读写 ──
    (re.compile(r"^read of credential file"),
     "读取口令/密钥等凭据文件（{target}），内容一旦出现在输出里就可能外泄。", False),
    (re.compile(r"^upload credential file"),
     "把口令/密钥等凭据文件（{target}）作为上传或 POST 内容送出，属于凭据外泄，一旦执行无法收回。", False),
    (re.compile(r"^upload sensitive system path"),
     "把系统关键文件（{target}）作为上传或 POST 内容送出，配置与凭据可能就此落到外部机器。", False),
    (re.compile(r"^write to persistence/credential target"),
     "写入认证或启动植入类文件（{target}），等同于给机器安装永久后门，正常运维不需要。", False),
    (re.compile(r"^write to sensitive path"),
     "直接改写系统关键路径下的文件（{target}），可能损坏软件或改变开机行为。", False),
    (re.compile(r"^move/copy sensitive system path as source"),
     "以系统关键文件作为源进行复制/移动（{target}），可能造成泄露或原件丢失。", False),
    (re.compile(r"^tar archives sensitive system path"),
     "把系统敏感文件打包（{target}），打包结果很容易被带出机器。", False),
    # 放在具体 chmod 规则之后：兜住 shred/truncate 等动态动词前缀
    (re.compile(r"\bon sensitive system path \("),
     "该命令直接作用于系统关键文件（{target}），可能造成软件损坏或系统不可用。", False),

    # ── 防火墙 / 计划任务 ──
    (re.compile(r"^flush iptables|^flush/delete iptables rules"),
     "清空防火墙规则，机器的访问控制立即失效；若这是远程主机，可能直接失联。", False),
    (re.compile(r"^disable firewall|^ufw firewall reset"),
     "关闭或重置防火墙，所有开放端口对外暴露。", False),
    (re.compile(r"^remove crontab"),
     "删除当前用户的全部计划任务，原有定时作业会永久消失。", False),
    (re.compile(r"^install/replace crontab"),
     "安装或覆盖计划任务，会改写当前用户已有的定时作业，并让新任务在后台反复执行。", False),
    (re.compile(r"^schedule at job"),
     "登记一次性定时任务（at），到点自动执行且不易被察觉。", False),

    # ── 包 / 容器 ──
    (re.compile(r"^docker destructive"),
     "删除容器、镜像或清理 Docker 资源，未持久化的数据会随容器一起消失。", False),
    (re.compile(r"^npm uninstall|^pip uninstall"),
     "卸载依赖包，依赖它的应用可能立刻无法运行。", False),
    (re.compile(r"^package removal"),
     "卸载系统软件包，可能连带移除依赖它的服务。", False),

    # ── Git ──
    (re.compile(r"^force push to main/master"),
     "强制推送到主干分支，会覆盖他人的提交记录，历史很难恢复。", False),
    (re.compile(r"^force push"),
     "强制推送远端分支，远端已有的提交会被覆盖丢失。", False),
    (re.compile(r"^git reset hard"),
     "硬重置工作区，尚未提交的本地改动会被全部丢弃。", False),
    (re.compile(r"^git clean force"),
     "强制清除未跟踪文件（含被忽略的文件），这些文件没有备份就无法找回。", False),
    (re.compile(r"^(?P<target>.+?) \(writes repository state, default approval\)"),
     "这条 git 命令看着像在查、其实在写（{target}）：删掉的分支和标签不容易找回，"
     "改了 remote 之后每次 push/fetch 都会把代码交给新地址。", False),

    # ── 运维类操作（动词 + 子命令在 reason 头部）──
    (re.compile(r"^(?P<target>\S+ \S+) with destructive sub-action"),
     "命令包含破坏性子操作（{target}），可能造成数据删除或服务中断。", False),
    (re.compile(r"^(?P<target>\S+ \S+) \(operational verb, default approval\)"),
     "这是一项改变系统或服务状态的运维操作（{target}），执行后通常不会自动回到原样。", True),

    # ── 未知 / 不可解析 ──
    (re.compile(r"^unrecognizable command"),
     "看不出要执行的是哪个程序（{target}），可能是残缺命令或刻意混淆，后果无法判断。", False),
    (re.compile(r"^unparseable segment"),
     "命令语法无法解析、形态异常，系统按最保守的方式判定，请人工核对原文再决定是否授权。", False),
    (re.compile(r"^unknown command, default approval"),
     "这条命令不在已知安全操作清单里（{target}），系统无法预知它的后果，请确认这确实是你要求做的事。", True),
]


def target_from_reason(reason: str) -> str:
    """从 reason 尾部的 (…) 分组里取出被点名的对象（路径 / 动词 / 文件名）"""
    if not reason.endswith(")"):
        return ""
    start = reason.rfind(" (")
    if start < 0:
        return ""
    return reason[start + 2:-1].strip()


def _clip(text: str) -> str:
    if len(text) <= _MAX_TARGET_LEN:
        return text
    return text[:_MAX_TARGET_LEN] + "…"


def explain_for_reason(reason: str) -> ExplainResult:
    """英文 reason → 中文后果说明

    未命中后果表时返回通用兜底文案（source="fallback"），不标记 needs_llm：
    未知 reason 属于表漂移，应由覆盖测试暴露，而不是靠 LLM 掩盖。
    """
    if not reason:
        return ExplainResult(text=FALLBACK_TEXT, source="fallback")
    for pattern, template, needs_llm in CONSEQUENCE_RULES:
        m = pattern.search(reason)
        if not m:
            continue
        named = m.groupdict()
        target = named.get("target") or target_from_reason(reason)
        text = template.replace("{target}", _clip(target)) if "{target}" in template else template
        return ExplainResult(text=text, source="static", needs_llm=needs_llm)
    return ExplainResult(text=FALLBACK_TEXT, source="fallback")


# ──────────────────────────────────────────────────────────────────────────────
# LLM 兜底 —— 只服务「未知命令默认审批」族
#
# 时序：卡片先带静态文案弹出，本模块随后异步补发 permission_explain 事件回填。
# 审批窗口只有 120s（reins/core.py），拦截器在 PRE_TOOL_CALL 串行链上，
# 所以这里绝不同步等待模型 —— 阻塞一秒就等于吃掉用户一秒的判断时间。
# ──────────────────────────────────────────────────────────────────────────────

# 本地/慢模型下可整体关掉 LLM 兜底，退回纯静态文案（不影响任何安全判定）
LLM_EXPLAIN_ENABLED = os.environ.get("CKUN_AGENT_DISABLE_LLM_EXPLAIN") != "1"

_LLM_CHUNK_TIMEOUT_S = 2.5
_LLM_TOTAL_TIMEOUT_S = 6.0
_LLM_MAX_CHARS = 80          # 输出上限：超过即视为模型开始长篇解释
_UNKNOWN_SENTINEL = "无法确定该命令的作用"

_LLM_SYSTEM_PROMPT = (
    "用户在授权执行一条他看不懂的 shell 命令。用一句简体中文说明这条命令会做什么、"
    "执行后机器上会发生什么变化，不超过 40 个字。"
    "只输出这一句话：不要前缀、不要复述命令本身、不要建议、不要换行。"
    f"如果你无法确定它的作用，只输出：{_UNKNOWN_SENTINEL}"
)

# 解释文本缓存：key 与授权键同源（工具 + 归一化命令哈希）
# None = 该命令已尝试过且失败，进程内不再重试（防止每次审批都刷一遍 LLM）
_explain_cache: dict[str, Optional[str]] = {}
_explain_inflight: set[str] = set()
_bg_tasks: set[asyncio.Task] = set()
_CACHE_MAX = 256


def explain_cache_key(tool: str, display_cmd: str) -> str:
    """与 PermissionGate._grant_key 同派生法：授权按命令绑定，解释也按命令缓存"""
    digest = hashlib.sha256(display_cmd.encode("utf-8")).hexdigest()[:16]
    return f"{tool}:{digest}"


def build_llm_messages(tool: str, display_cmd: str, env: str) -> list[dict]:
    """只喂归一化命令 + 工具名 + 环境：不喂 Agent 的推理与任务全文，
    避免"由提出命令的同一方解释自己"带来的自我美化，让模型只看命令本身。"""
    return [
        {"role": "system", "content": _LLM_SYSTEM_PROMPT},
        {"role": "user", "content": f"环境：{env}\n执行工具：{tool}\n命令：{display_cmd}"},
    ]


def sanitize_llm_explanation(raw: str) -> Optional[str]:
    """校验模型输出：必须是单行中文短句，否则视为失败（回退静态文案）"""
    text = re.sub(r"\s+", " ", raw or "").strip()
    if not text or len(text) > _LLM_MAX_CHARS:
        return None
    if not re.search(r"[\u4e00-\u9fff]", text):
        return None
    if text == _UNKNOWN_SENTINEL:
        return None
    return text


def _cache_put(key: str, value: Optional[str]) -> None:
    if len(_explain_cache) >= _CACHE_MAX and key not in _explain_cache:
        # FIFO 截断：dict 保持插入序，弹掉最早的一条
        _explain_cache.pop(next(iter(_explain_cache)))
    _explain_cache[key] = value


async def _one_shot_llm(messages: list[dict], ctx: "RunContext") -> Optional[str]:
    """一次性 LLM 调用 —— 延迟导入 post_processor，使静态解释层不被
    database / prompts 等重依赖拖累（本模块在每次工具调用前都会被导入）。"""
    from post_processor import _call_llm_with_timeout

    model_cfg = {
        "base_url": ctx.llm_base_url,
        "api_key": ctx.llm_api_key,
        "model_id": ctx.llm_model,
    }
    return await _call_llm_with_timeout(
        messages=messages,
        model_cfg=model_cfg,
        model_id=ctx.llm_model,
        temperature=0.2,
        max_tokens=80,
        chunk_timeout=_LLM_CHUNK_TIMEOUT_S,
        total_timeout=_LLM_TOTAL_TIMEOUT_S,
        task_name="perm_explain",
    )


async def maybe_explain_via_llm(
    ctx: "RunContext",
    tool: str,
    command: str,
    display_cmd: str,
    env: str,
) -> None:
    """为未知命令补一条中文解释；命中缓存则延后投递，否则后台生成。

    调用方（FilesystemGuard）已弹出卡片，本函数不等待模型：
    生成失败/超时只是没有回填事件，卡片保留静态文案。
    任何异常都在内部吞掉 —— 解释链路绝不向外抛，否则会撞上 Reins 对
    safety_critical 拦截器的 fail-closed 处理（异常 = 拦截）。
    """
    if not LLM_EXPLAIN_ENABLED:
        return
    if not command or not str(command).strip():
        return
    emit = ctx.metadata.get("emit_event")
    if emit is None:
        return  # 无事件通道（如脱离 Harness 的单测/调度复用），纯静态
    if not ctx.llm_base_url:
        # S10-07：与 auto_approve._judge_once 同修 —— 空 api_key 是本地 vLLM 的
        # 正常形态，不是「没配模型」；原条件让命令解释对这类配置永久缺席。
        return

    key = explain_cache_key(tool, display_cmd or str(command))
    cached = _explain_cache.get(key, "")
    if cached:
        # 必须延后投递：此刻 permission_needed 卡片还躺在拦截链返回值里，要等
        # 拦截器返回、driver 走 _emit_decision_events 才入队。同步 await emit
        # 会让 explain 先于卡片到达前端，被「找不到未处理卡片即丢弃」的逻辑吞掉。
        # create_task 让本协程排到 driver 下一次让出事件循环之后，FIFO 保证卡片在前。
        task = asyncio.create_task(_emit(emit, ctx, tool, command, display_cmd, cached))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return
    if key in _explain_cache or key in _explain_inflight:
        return  # 已失败过 / 正在生成，不重复刷 LLM

    async def _worker() -> None:
        try:
            raw = await _one_shot_llm(build_llm_messages(tool, display_cmd, env), ctx)
            text = sanitize_llm_explanation(raw or "")
            _cache_put(key, text)
            if text:
                await _emit(emit, ctx, tool, command, display_cmd, text)
        except Exception as e:
            _cache_put(key, None)
            logger.warning("命令解释生成失败（保留静态文案，不影响审批）: %s", e)
        finally:
            _explain_inflight.discard(key)

    # 审查R2修复：inflight 登记必须与上方检查同在无 await 的同步段。原来 add 在
    # _worker 内部，而 create_task 后协程要到下一轮事件循环才执行 —— 并发同命令
    # 的调用都在 add 之前通过 `key in _explain_inflight` 检查，重复入队重复烧 LLM。
    _explain_inflight.add(key)
    task = asyncio.create_task(_worker())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _emit(emit: Any, ctx: "RunContext", tool: str, command: str,
                display_cmd: str, text: str) -> None:
    from .contracts import TraceEvent

    await emit(TraceEvent(
        kind="permission_explain",
        source="harness.command_explain",
        run_id=ctx.run_id,
        iteration=ctx.iteration,
        payload={
            "tool": tool,
            # command 必须是卡片 args.command 的原值：前端按 tool::command 找回卡片
            "command": command,
            "normalized_command": display_cmd,
            "explanation": text,
        },
    ))
