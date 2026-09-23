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

# 退出码可为负（信号终止），生产者写的是 str(int)，故 -?\d+
_EXIT_SENTINEL_RE = re.compile(re.escape(EXIT_CODE_PREFIX) + r"-?\d+\]")


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
