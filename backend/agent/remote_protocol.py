"""远程执行输出的哨兵协议 — 单一实现源。

生产者只有两处、且都用这里的常量：`ops_common.exec_remote` / `run_headless` 把
stderr 与退出码以固定哨兵行追加到命令输出尾部（`\\n[stderr]\\n...` +
`\\n[exit_code=N]`）；`harness/driver` 的可见终端精确通道成功结果同样补 exit_code 行
（ConfidenceMonitor 要按它区分"探针非零退出"与"调用真实失败"）。消费方分散在
host_profile、ops_job、ops_obs、reins.flow 处，此前各自手抄前缀字面量，并抄出三种
不一致写法（`"[exit_code="` / 少一个 `=` 的 `"[exit_code"` / 正则）—— 正是本仓库
反复出现的「跨层 key 漂移」家族。收敛到此模块。

只依赖标准库，且**不得 import 任何 tools/ 或 harness/ 模块**：host_profile 是纯
stdlib leaf，让它跨层去 `tools/builtin/ops_common` 取哨兵判定会把协议拖进循环依赖。

测试里保留字面量是有意为之：契约钉就要钉住线上真实的字节，不能在测试里 import
常量，否则改错常量无人报警。
"""
from __future__ import annotations

import re

EXIT_CODE_PREFIX = "[exit_code="
STDERR_SENTINEL = "[stderr]"

# 退出码可为负（信号终止），生产者写的是 str(int)，故 -?\d+；括号捕获组供 remote_parts 取数
_EXIT_SENTINEL_RE = re.compile(re.escape(EXIT_CODE_PREFIX) + r"(-?\d+)\]")


def is_exit_code_line(line: str) -> bool:
    """整行是退出码哨兵行。"""
    return line.startswith(EXIT_CODE_PREFIX)


def is_sentinel_line(line: str) -> bool:
    """整行是任一尾部哨兵行（`[exit_code=..]` 或 `[stderr]`）。"""
    return is_exit_code_line(line) or line == STDERR_SENTINEL


def has_sentinel_fragment(line: str) -> bool:
    """行内**任意位置**含哨兵前缀。

    用于消费「可能被 truncate_output 的 head+tail 拼接过」的输出：截断会把哨兵粘到
    某个数据行尾部，此时按整行前缀判定会漏。比 is_sentinel_line 宽松，仅限这一用途。
    """
    return EXIT_CODE_PREFIX in line or line == STDERR_SENTINEL


def has_exit_sentinel(text: str) -> bool:
    """输出中是否出现 `[exit_code=N]` —— 命令确已在远端执行过的证据。"""
    return bool(_EXIT_SENTINEL_RE.search(text))


def remote_parts(raw: str) -> tuple[str, str, int | None]:
    """把回传拆成 (stdout 正文, stderr 文本, 退出码)；退出码 None = 没在远端跑起来。

    拼装顺序由 `ops_common.exec_remote` 决定（`正文 → [stderr] 段 → [exit_code=N]`），
    拆分口径必须与那里一致，所以拆解也收在本模块：消费方各处手抄一份拆分正则就是
    本模块 docstring 里写的那场「跨层 key 漂移」事故本身。

    为什么判"故障"只能看 stderr 段与退出码：
    - 拿正文里的字样当判据会把日志本身当成故障（日志里出现
      "No such file or directory" 恰恰说明它被读出来了）；
    - 每一次成功执行都无条件追加哨兵，所以「整串不含哨兵」这类判据对任何成功输出恒假
      （S6-07 就栽在那儿）；
    - **管道串起来的模板退出码属于最后一段**：`nl -ba f | sed -n …` 里 nl 打不开文件
      时 sed 仍然 0 退出，`ok`/`rc` 都是绿的，只有 stderr 段说真话。读类工具不查这一段
      就会把"读失败"报成"文件为空/没有匹配"，模型据此得出相反结论。

    退出码缺失（None）意味着通道根本没到远端（缺会话 / 桥不可达 / 超时），此时任何
    "没有结果"的读数都是编造的，不是观测结果。
    """
    m = _EXIT_SENTINEL_RE.search(raw)
    rc = int(m.group(1)) if m else None
    head = raw[: m.start()] if m else raw
    if STDERR_SENTINEL in head:
        body, err = head.split(STDERR_SENTINEL, 1)
        return body.strip(), " ".join(err.split()), rc
    return head.strip(), "", rc

